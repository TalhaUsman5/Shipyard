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

_client = None


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

    client = _get_client()
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    choice = response.choices[0]
    text = choice.message.content or ""
    usage = response.usage

    if not text and choice.finish_reason == "length":
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
