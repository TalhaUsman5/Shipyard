"""
Unit tests for session state tracking (sessions.py), independent of the
engine walk — node lifecycle, budgets, the append-only trace log, and the
listing metadata used to pick a session to resume.
"""
import os

import pytest

import sessions


def test_record_event_appends_and_assigns_increasing_seq():
    s = sessions.Session("feat", "proj")
    s.record_event("A")
    s.record_event("B")
    seqs = [e["seq"] for e in s.data["events"]]
    assert seqs == sorted(seqs)
    assert seqs == list(range(len(seqs)))
    assert s.data["events"][-1]["type"] == "B"


def test_node_lifecycle():
    s = sessions.Session("feat", "proj")
    s.node_started("plan")
    assert s.node_status("plan") == "running"
    s.node_succeeded("plan", {"title": "x"}, tokens={"input": 1, "output": 1})
    assert s.node_status("plan") == "succeeded"
    assert s.node_output("plan") == {"title": "x"}


def test_node_failed_records_error():
    s = sessions.Session("feat", "proj")
    s.node_started("build")
    s.node_failed("build", "boom")
    assert s.node_status("build") == "failed"
    assert s.data["nodes"]["build"]["error"] == "boom"




def test_budgets_increment_and_read_back():
    s = sessions.Session("feat", "proj")
    assert s.budget_used("x") == 0
    assert s.use_budget("x") == 1
    assert s.use_budget("x") == 2
    assert s.budget_used("x") == 2


def test_grant_budget_is_additive_and_never_touches_used_count():
    s = sessions.Session("feat", "proj")
    s.use_budget("x")
    s.use_budget("x")
    assert s.grant_budget("x", 3, reason="give it more room") == 3
    assert s.grant_budget("x", 2, reason="a bit more still") == 5
    assert s.budget_granted("x") == 5
    assert s.budget_used("x") == 2, "granting never touches the used count — it only raises the ceiling"


def test_grant_budget_requires_a_positive_amount_and_a_reason():
    s = sessions.Session("feat", "proj")
    with pytest.raises(ValueError):
        s.grant_budget("x", 0, reason="not enough")
    with pytest.raises(ValueError):
        s.grant_budget("x", -1, reason="negative")
    with pytest.raises(ValueError):
        s.grant_budget("x", 1, reason="")
    with pytest.raises(ValueError):
        s.grant_budget("x", 1, reason=None)


def test_grant_budget_records_its_own_event():
    s = sessions.Session("feat", "proj")
    s.grant_budget("x", 4, reason="demo unblock", granted_by="alice")
    events = sessions.load_event_log(s.id)
    granted = [e for e in events if e["type"] == "BUDGET_GRANTED"]
    assert len(granted) == 1
    assert granted[0]["data"] == {
        "budget_key": "x",
        "amount": 4,
        "new_grant_total": 4,
        "reason": "demo unblock",
        "granted_by": "alice",
    }


def test_reset_nodes_reverts_status_to_pending():
    s = sessions.Session("feat", "proj")
    s.node_started("build")
    s.node_succeeded("build", {})
    s.reset_nodes(["build"])
    assert s.node_status("build") == "pending"


def test_events_log_file_is_readable_back():
    s = sessions.Session("feat", "proj")
    s.record_event("HELLO")
    events = sessions.load_event_log(s.id)
    assert events is not None
    assert events[-1]["type"] == "HELLO"


def test_events_jsonl_is_never_rewritten_only_appended_to():
    s = sessions.Session("feat", "proj")
    s.record_event("A")

    path = os.path.join(sessions.SESSIONS_DIR, f"{s.id}.events.jsonl")
    with open(path, "r", encoding="utf-8") as f:
        before = f.read()

    s.record_event("B")

    with open(path, "r", encoding="utf-8") as f:
        after = f.read()

    assert after.startswith(before), "earlier lines must never be rewritten, only appended to"
    assert after != before


def test_load_event_log_missing_session_returns_none():
    assert sessions.load_event_log("does-not-exist") is None


def test_list_sessions_reports_project_path_and_resumable():
    s = sessions.Session("feat", "myproj")
    s.finish("failed_needs_human")
    rows = sessions.list_sessions()
    row = next(r for r in rows if r["id"] == s.id)
    assert row["project_path"] == "myproj"
    assert row["resumable"] is True

    s2 = sessions.Session("feat2", "otherproj")
    s2.finish("completed")
    rows = sessions.list_sessions()
    row2 = next(r for r in rows if r["id"] == s2.id)
    assert row2["project_path"] == "otherproj"
    assert row2["resumable"] is False


def test_build_report_summarizes_a_completed_run():
    s = sessions.Session("feat", "proj")
    s.node_succeeded("plan", {"title": "x", "constraints": []})
    s.node_succeeded("build", {"files": [{"path": "src/a.js", "content": "x"}]})
    s.node_succeeded("verify", {"files": [{"path": "test/a.test.js", "content": "x"}]})
    s.node_succeeded("test_gate", {"passed": True})
    s.node_succeeded("review", {"approved": True, "issues": [], "summary": "ok"})
    s.node_succeeded("calibrate", {"pattern": "a pattern"})
    s.finish("completed")

    report = sessions.build_report(s.data)
    assert report["status"] == "completed"
    assert report["files_written"]["implementation"] == ["src/a.js"]
    assert report["files_written"]["tests"] == ["test/a.test.js"]
    assert report["test_result"] == {"passed": True}
    assert report["calibrated_pattern"] == "a pattern"
