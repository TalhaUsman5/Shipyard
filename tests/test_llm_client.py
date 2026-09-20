"""
Unit tests for llm_client's per-role model-tier resolution. ROLE_TIERS
says WHICH tier a role needs (a code decision); FACTORY_MODEL_<TIER> says
what model id that tier currently means (an environment decision) — these
tests pin down both the resolution logic and the fallback-to-FACTORY_MODEL
behavior for anyone with only that single variable configured.
"""
import pytest

import llm_client


@pytest.fixture(autouse=True)
def _clear_model_env(monkeypatch):
    for key in ("FACTORY_MODEL", "FACTORY_MODEL_SOL", "FACTORY_MODEL_TERRA", "FACTORY_MODEL_LUNA"):
        monkeypatch.delenv(key, raising=False)


def test_every_declared_role_has_a_tier():
    expected_roles = {"planner", "builder", "verifier", "reviewer", "arbiter", "review_arbiter", "calibrator"}
    assert expected_roles == set(llm_client.ROLE_TIERS)


def test_model_for_role_uses_the_tier_specific_override(monkeypatch):
    monkeypatch.setenv("FACTORY_MODEL", "fallback-model")
    monkeypatch.setenv("FACTORY_MODEL_SOL", "flagship-model")
    assert llm_client._model_for_role("planner") == "flagship-model"  # planner is tier "sol"


def test_model_for_role_falls_back_to_factory_model_when_tier_var_unset(monkeypatch):
    monkeypatch.setenv("FACTORY_MODEL", "fallback-model")
    assert llm_client._model_for_role("calibrator") == "fallback-model"  # no FACTORY_MODEL_LUNA set


def test_model_for_role_falls_back_to_factory_model_for_an_unrecognized_role(monkeypatch):
    monkeypatch.setenv("FACTORY_MODEL", "fallback-model")
    monkeypatch.setenv("FACTORY_MODEL_SOL", "flagship-model")
    assert llm_client._model_for_role("some_future_role") == "fallback-model"


def test_model_for_role_raises_when_nothing_is_configured_at_all(monkeypatch):
    with pytest.raises(RuntimeError):
        llm_client._model_for_role("planner")


def test_different_tiers_resolve_independently(monkeypatch):
    monkeypatch.setenv("FACTORY_MODEL", "fallback-model")
    monkeypatch.setenv("FACTORY_MODEL_SOL", "sol-model")
    monkeypatch.setenv("FACTORY_MODEL_TERRA", "terra-model")
    monkeypatch.setenv("FACTORY_MODEL_LUNA", "luna-model")

    assert llm_client._model_for_role("builder") == "sol-model"
    assert llm_client._model_for_role("arbiter") == "terra-model"
    assert llm_client._model_for_role("calibrator") == "luna-model"


def test_call_passes_the_resolved_model_to_the_client(monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "x")
    monkeypatch.setenv("FACTORY_MODEL", "fallback-model")
    monkeypatch.setenv("FACTORY_MODEL_LUNA", "luna-model")

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
        def create(self, model, **kwargs):
            captured["model"] = model
            return FakeStream()

    class FakeClient:
        chat = type("C", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(llm_client, "_get_client", lambda: FakeClient())

    llm_client.call("calibrator", "system", "user prompt")
    assert captured["model"] == "luna-model"
