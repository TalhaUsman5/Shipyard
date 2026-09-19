"""
Unit tests for llm_client's named inference-failure hierarchy, and an
integration test proving engine.py actually records the classification on
a node failure instead of the previous bare exception string.
"""
import httpx
import openai
import pytest

import engine
import llm_client
import sessions


def _rate_limit_error(body):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(429, request=request, json=body)
    return openai.RateLimitError(message="rate limited", response=response, body=body)


def _permission_denied_error():
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(403, request=request, json={})
    return openai.PermissionDeniedError(message="forbidden", response=response, body={})


def _internal_server_error():
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(500, request=request, json={})
    return openai.InternalServerError(message="server error", response=response, body={})


def test_quota_exhaustion_classifies_as_allowance_exhausted():
    error = _rate_limit_error({"error": {"code": "insufficient_quota"}})
    classified = llm_client._classify(error)
    assert isinstance(classified, llm_client.AllowanceExhausted)


def test_generic_rate_limit_classifies_as_capacity_insufficient():
    error = _rate_limit_error({"error": {"code": "rate_limit_exceeded"}})
    classified = llm_client._classify(error)
    assert isinstance(classified, llm_client.CapacityInsufficient)


def test_permission_denied_classifies_as_model_blocked():
    classified = llm_client._classify(_permission_denied_error())
    assert isinstance(classified, llm_client.ModelBlocked)


def test_internal_server_error_classifies_as_capacity_insufficient():
    classified = llm_client._classify(_internal_server_error())
    assert isinstance(classified, llm_client.CapacityInsufficient)


def test_a_bug_in_our_own_code_is_left_unclassified():
    """A plain, non-openai exception (a bug in our own code, a malformed
    response our own parsing choked on) must pass through unchanged — only
    genuine provider-layer conditions get wrapped."""
    error = ValueError("not json")
    assert llm_client._classify(error) is error


def test_node_failed_records_error_kind_for_a_classified_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_DIR", str(tmp_path))
    s = sessions.Session("feat", "proj")
    engine._node_failed(s, "plan", llm_client.AllowanceExhausted("out of quota"))

    assert s.data["nodes"]["plan"]["error_kind"] == "AllowanceExhausted"
    events = [e for e in s.data["events"] if e["type"] == "NODE_FAILED"]
    assert events[-1]["data"]["error_kind"] == "AllowanceExhausted"


def test_node_failed_leaves_error_kind_none_for_an_ordinary_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_DIR", str(tmp_path))
    s = sessions.Session("feat", "proj")
    engine._node_failed(s, "plan", ValueError("the model returned malformed JSON"))

    assert s.data["nodes"]["plan"]["error_kind"] is None


def test_a_named_inference_error_from_a_role_ends_the_run_with_its_kind_recorded(
    fake_agents_raw, monkeypatch, make_repo
):
    """End-to-end through the real walker: a role raising a named
    inference error must surface as a NODE_FAILED with that error_kind,
    not a generic unclassified failure."""
    import agents

    make_repo("proj_inference_error")

    def blocked_planner(feature_request, memory, snapshot, pkg, refinement_feedback=None, snapshot_truncated=False):
        raise llm_client.ModelBlocked("content policy violation")

    monkeypatch.setattr(agents, "run_planner", blocked_planner)

    session = engine.run_pipeline("add greet helper", "proj_inference_error")

    assert session.data["status"] == "failed"
    assert session.data["nodes"]["plan"]["error_kind"] == "ModelBlocked"
