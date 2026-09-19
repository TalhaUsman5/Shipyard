"""
The only place that talks to the model. Every role calls `call()` here.

Uses an OpenAI-compatible client (the OpenAI SDK pointed at a custom
base_url) rather than the Anthropic SDK, so this harness can target any
OpenAI-compatible endpoint. Configure via .env:
  FACTORY_API_KEY  - the API key
  FACTORY_BASE_URL - the endpoint base URL (omit to use OpenAI's default)
  FACTORY_MODEL    - the model name to use for every role
"""
import os

import openai

import redaction
import runtime

_client = None


class InferenceError(Exception):
    """Base for a failure that happened at the inference layer itself, as
    opposed to a bug in our own prompt/schema (a plain OpenAIError like
    BadRequestError or NotFoundError is left unwrapped — those point at
    something wrong on OUR side of the request, not the provider's
    capacity/policy). Distinguishing these lets a trace reader (or the
    dashboard) tell "the provider couldn't serve this" from "the model
    wrote broken code" without reading a raw exception string by hand."""


class ModelBlocked(InferenceError):
    """The provider refused to run this request at all — bad/rejected
    credentials, or a content-policy block. No amount of retrying the same
    request will fix this; it needs operator intervention (fix the key,
    change the content) before this role can run again."""


class AllowanceExhausted(InferenceError):
    """A quota/billing ceiling has been hit (not a transient rate limit) —
    the account itself is out of allowance. Retrying immediately won't
    help; this needs a human to raise the ceiling or wait for a reset."""


class CapacityInsufficient(InferenceError):
    """The provider is transiently unable to serve the request — rate
    limited, overloaded, or briefly unreachable. Unlike the two above,
    this one plausibly resolves on its own; a later retry (or a resume)
    has a real chance of succeeding without anyone changing anything."""


def _classify(error: Exception) -> Exception:
    """Maps an openai-SDK exception onto the hierarchy above. Returns the
    ORIGINAL error unchanged for anything that isn't a provider-layer
    condition (a BadRequestError from a malformed request is our bug, not
    the provider's), so callers still see exactly what they see today for
    everything except these three specific, actionable conditions."""
    if not isinstance(error, openai.OpenAIError):
        return error

    if isinstance(error, openai.RateLimitError):
        body = getattr(error, "body", None) or {}
        code = ""
        if isinstance(body, dict):
            code = str(body.get("error", {}).get("code") or body.get("code") or "").lower()
        if "quota" in code or "billing" in code:
            return AllowanceExhausted(str(error))
        return CapacityInsufficient(str(error))
    if isinstance(error, (openai.PermissionDeniedError, openai.AuthenticationError)):
        return ModelBlocked(str(error))
    if hasattr(openai, "ContentFilterFinishReasonError") and isinstance(
        error, openai.ContentFilterFinishReasonError
    ):
        return ModelBlocked(str(error))
    if isinstance(error, (openai.InternalServerError, openai.APIConnectionError, openai.APITimeoutError)):
        return CapacityInsufficient(str(error))
    return error


# Per-request ceiling in seconds. Every real call observed so far — plan,
# build, verify, arbiter — has finished well under 2 minutes; this leaves
# generous headroom above that (the biggest single-context call, review,
# reasons over the whole implementation + test file at once) while still
# failing predictably instead of riding out the SDK's own default (~10 min
# per attempt, times its own retries) the way a real hung call once did —
# observed live: a request that never returned and never errored either,
# past 13 minutes with no timeout in sight.
REQUEST_TIMEOUT_SECONDS = 300.0


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI

        api_key = os.getenv("FACTORY_API_KEY")
        if not api_key:
            raise RuntimeError("FACTORY_API_KEY is not set. Add it to .env.")
        base_url = os.getenv("FACTORY_BASE_URL") or None
        _client = OpenAI(api_key=api_key, base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS)
    return _client


def call(role: str, system: str, user: str, max_tokens: int = 16000):
    """Returns (text, {"input": n, "output": n}). `role` is passed through
    for logging only — every role uses the same FACTORY_MODEL.

    max_tokens defaults high (16000) because on a reasoning model, reasoning
    tokens are drawn from the same budget as the visible completion — a
    complex contract can burn the entire budget on reasoning and return
    empty content with finish_reason="length" before ever writing the JSON
    the caller actually wants. 4096 was observed exhausting on an
    18-requirement contract with zero completion tokens produced."""
    model = os.getenv("FACTORY_MODEL")
    if not model:
        raise RuntimeError("FACTORY_MODEL is not set. Add it to .env.")

    # The secrets boundary: `user` is where every role interpolates
    # externally-derived content (the raw feature request, target-project
    # file contents, a contract built from either) — `system` is always
    # our own static prompt text, never attacker/user-controlled, so it's
    # not scanned. This is the one chokepoint every role's call passes
    # through, so nothing downstream needs its own redaction step.
    sink = runtime.get_sink()
    cleaned_user, findings = redaction.redact(user)
    if findings:
        if redaction.policy() == "block":
            raise redaction.SecretDetected(findings)
        if sink:
            runtime.note_redaction(sink.session_id, sink.node_id, findings)

    client = _get_client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": cleaned_user},
    ]

    try:
        try:
            text, finish_reason, usage = _call_streaming(client, model, messages, max_tokens, sink)
        except runtime.Cancelled:
            raise
        except Exception:
            # Best-effort fallback for an OpenAI-compatible endpoint that
            # doesn't support stream=True / stream_options.include_usage —
            # keeps every role working exactly as before streaming existed,
            # just without a live buffer for this call.
            text, finish_reason, usage = _call_nonstreaming(client, model, messages, max_tokens)
    except runtime.Cancelled:
        raise
    except Exception as e:
        raise _classify(e) from e

    if not text and finish_reason == "length":
        reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
        raise RuntimeError(
            f"Model returned empty content for role={role!r}: hit max_tokens={max_tokens} "
            f"before producing any output (reasoning_tokens={reasoning}). Raise max_tokens."
        )

    tokens = {
        "input": usage.prompt_tokens if usage else 0,
        "output": usage.completion_tokens if usage else 0,
    }
    return text, tokens


def _call_streaming(client, model, messages, max_tokens, sink):
    stream = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
    )
    parts = []
    finish_reason = None
    usage = None
    if sink:
        runtime.start_node_buffer(sink.session_id, sink.node_id)
    try:
        for chunk in stream:
            if sink and sink.cancel_event is not None and sink.cancel_event.is_set():
                stream.close()
                raise runtime.Cancelled(f"cancelled during streaming call (session={sink.session_id!r})")
            if chunk.usage is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = getattr(choice.delta, "content", None)
            if delta:
                parts.append(delta)
                if sink:
                    runtime.append_chunk(sink.session_id, sink.node_id, delta)
    finally:
        stream.close()
    return "".join(parts), finish_reason, usage


def _call_nonstreaming(client, model, messages, max_tokens):
    response = client.chat.completions.create(model=model, max_tokens=max_tokens, messages=messages)
    choice = response.choices[0]
    return choice.message.content or "", choice.finish_reason, response.usage
