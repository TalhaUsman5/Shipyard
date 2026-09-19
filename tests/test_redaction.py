"""
Unit tests for redaction.py's pattern matching, and integration tests
proving llm_client.call() actually scrubs credential-shaped content
before it would reach the provider — not just that the scanner itself
works in isolation.
"""
import pytest

import llm_client
import redaction
import runtime


def test_redact_scrubs_a_github_token_and_reports_the_pattern():
    text = "use ghp_abcdefghijklmnopqrstuvwxyz0123456789AB to auth"
    cleaned, findings = redaction.redact(text)
    assert "ghp_" not in cleaned
    assert "[REDACTED:github_token]" in cleaned
    assert findings == [redaction.Finding("github_token", 1)]


def test_redact_scrubs_an_aws_access_key():
    cleaned, findings = redaction.redact("AKIAABCDEFGHIJKLMNOP is the key")
    assert "AKIA" not in cleaned
    assert findings[0].pattern == "aws_access_key_id"


def test_redact_scrubs_a_private_key_block():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
    cleaned, findings = redaction.redact(text)
    assert "BEGIN RSA PRIVATE KEY" not in cleaned
    assert findings[0].pattern == "private_key_block"


def test_redact_scrubs_credentials_embedded_in_a_url():
    cleaned, findings = redaction.redact("postgres://admin:hunter2@db.internal:5432/prod")
    assert "hunter2" not in cleaned
    assert findings[0].pattern == "credential_in_url"


def test_redact_scrubs_a_jwt():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    cleaned, findings = redaction.redact(f"Authorization header: {token}")
    assert token not in cleaned
    assert findings[0].pattern == "jwt"


def test_redact_leaves_ordinary_text_untouched():
    text = "Add a greet(name) function that returns 'Hello, ' + name"
    cleaned, findings = redaction.redact(text)
    assert cleaned == text
    assert findings == []


def test_redact_counts_multiple_occurrences_of_the_same_pattern():
    text = "AKIAAAAAAAAAAAAAAAAA and also AKIABBBBBBBBBBBBBBBB"
    _cleaned, findings = redaction.redact(text)
    assert findings == [redaction.Finding("aws_access_key_id", 2)]


def test_policy_defaults_to_redact_and_reads_the_env_override(monkeypatch):
    monkeypatch.delenv("FACTORY_SECRET_POLICY", raising=False)
    assert redaction.policy() == "redact"
    monkeypatch.setenv("FACTORY_SECRET_POLICY", "block")
    assert redaction.policy() == "block"
    monkeypatch.setenv("FACTORY_SECRET_POLICY", "something-invalid")
    assert redaction.policy() == "redact"


def test_call_redacts_the_user_prompt_before_it_reaches_the_client(monkeypatch):
    """Integration point: llm_client.call() must scrub `user` before
    building the messages list sent to the SDK client — this is the
    structural enforcement point, not redaction.py in isolation."""
    monkeypatch.setenv("FACTORY_API_KEY", "x")
    monkeypatch.setenv("FACTORY_MODEL", "x")
    monkeypatch.delenv("FACTORY_SECRET_POLICY", raising=False)

    captured = {}

    class FakeUsage:
        prompt_tokens = 1
        completion_tokens = 1
        completion_tokens_details = None

    class FakeChoice:
        delta = type("D", (), {"content": "ok"})()
        finish_reason = "stop"

    class FakeChunk:
        choices = [FakeChoice()]
        usage = FakeUsage()

    class FakeStream:
        def __iter__(self):
            return iter([FakeChunk()])

        def close(self):
            pass

    class FakeCompletions:
        def create(self, model, max_tokens, messages, **kwargs):
            captured["messages"] = messages
            return FakeStream()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(llm_client, "_get_client", lambda: FakeClient())

    text, _tokens = llm_client.call(
        "builder", "system prompt", "the token is ghp_abcdefghijklmnopqrstuvwxyz0123456789AB, use it"
    )

    sent_user = captured["messages"][1]["content"]
    assert "ghp_" not in sent_user
    assert "[REDACTED:github_token]" in sent_user
    assert text == "ok"


def test_call_raises_secret_detected_under_block_policy(monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "x")
    monkeypatch.setenv("FACTORY_MODEL", "x")
    monkeypatch.setenv("FACTORY_SECRET_POLICY", "block")

    with pytest.raises(redaction.SecretDetected):
        llm_client.call("builder", "system", "token: AKIAABCDEFGHIJKLMNOP")


def test_redaction_findings_are_surfaced_via_the_runtime_sink(monkeypatch):
    """The bridge engine.py relies on to log a durable SECRET_REDACTED
    event: llm_client.call() must publish findings into runtime.py's
    PENDING_REDACTIONS for whatever sink is currently set, since it has no
    Session of its own to log onto directly."""
    monkeypatch.setenv("FACTORY_API_KEY", "x")
    monkeypatch.setenv("FACTORY_MODEL", "x")
    monkeypatch.delenv("FACTORY_SECRET_POLICY", raising=False)

    class FakeUsage:
        prompt_tokens = 1
        completion_tokens = 1
        completion_tokens_details = None

    class FakeChoice:
        delta = type("D", (), {"content": "ok"})()
        finish_reason = "stop"

    class FakeChunk:
        choices = [FakeChoice()]
        usage = FakeUsage()

    class FakeStream:
        def __iter__(self):
            return iter([FakeChunk()])

        def close(self):
            pass

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeStream()

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(llm_client, "_get_client", lambda: FakeClient())

    token = runtime.set_sink("sess1", "build", None)
    try:
        llm_client.call("builder", "system", "AKIAABCDEFGHIJKLMNOP")
    finally:
        runtime.clear_sink(token)

    findings = runtime.pop_redactions("sess1", "build")
    assert findings == [redaction.Finding("aws_access_key_id", 1)]
