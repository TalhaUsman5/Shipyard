"""
Unit tests for session state tracking (sessions.py), independent of the
engine walk — node lifecycle, budgets, the append-only trace log, and the
listing metadata used to pick a session to resume.
"""
import os
import threading

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


def test_record_event_is_safe_under_concurrent_callers():
    """Regression test for the race a parallel_groups fork (build/verify —
    see graph.py) introduced: two threads calling record_event on the same
    Session near-simultaneously used to be able to read the same
    "len(events)" before either appended, producing duplicate seq values
    and corrupting the append-only trace's ordering guarantee. Session._lock
    fixes this — this hammers it with real concurrent threads rather than
    just asserting the lock exists."""
    s = sessions.Session("feat", "proj")
    n = 20
    threads = [threading.Thread(target=s.record_event, args=(f"EVENT_{i}",)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = sorted(e["seq"] for e in s.data["events"])
    assert seqs == list(range(n)), "concurrent record_event calls must never collide on seq"

    logged = sessions.load_event_log(s.id)
    assert sorted(e["seq"] for e in logged) == list(range(n)), "the on-disk trace must match too"


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


def test_list_sessions_filters_by_status_and_project_path():
    s1 = sessions.Session("feat", "proj-a")
    s1.finish("failed_needs_human")
    s2 = sessions.Session("feat", "proj-b")
    s2.finish("completed")

    only_stuck = sessions.list_sessions(status="failed_needs_human")
    assert {r["id"] for r in only_stuck} == {s1.id}

    only_proj_b = sessions.list_sessions(project_path="proj-b")
    assert {r["id"] for r in only_proj_b} == {s2.id}


def test_list_sessions_filters_by_since():
    s_old = sessions.Session("feat", "proj")
    s_old.data["started_at"] = 100.0
    s_old.save()
    s_new = sessions.Session("feat", "proj")
    s_new.data["started_at"] = 200.0
    s_new.save()

    rows = sessions.list_sessions(since=150.0)
    assert {r["id"] for r in rows} == {s_new.id}


def test_node_durations_sums_across_retries(monkeypatch):
    # consumed in order: Session.__init__'s started_at, then one pair per
    # record_event call below (start1, end1, start2, end2)
    times = iter([0.0, 100.0, 105.0, 110.0, 118.0])
    monkeypatch.setattr(sessions.time, "time", lambda: next(times))

    s = sessions.Session("feat", "proj")
    s.record_event("NODE_STARTED", "build")
    s.record_event("NODE_SUCCEEDED", "build")
    s.record_event("NODE_STARTED", "build")
    s.record_event("NODE_FAILED", "build")

    durations = sessions.node_durations(s.id)
    assert durations["build"] == pytest.approx(5.0 + 8.0)


def test_node_durations_ignores_a_node_still_in_progress(monkeypatch):
    times = iter([0.0, 100.0])
    monkeypatch.setattr(sessions.time, "time", lambda: next(times))

    s = sessions.Session("feat", "proj")
    s.record_event("NODE_STARTED", "build")  # never finishes

    assert sessions.node_durations(s.id) == {}


def test_node_durations_returns_empty_dict_for_a_session_with_no_log():
    assert sessions.node_durations("does-not-exist") == {}


def test_slowest_nodes_across_recent_aggregates_and_sorts(monkeypatch):
    times = iter([0.0, 100.0, 110.0, 0.0, 100.0, 101.0])
    monkeypatch.setattr(sessions.time, "time", lambda: next(times))

    s1 = sessions.Session("feat", "proj")
    s1.record_event("NODE_STARTED", "build")
    s1.record_event("NODE_SUCCEEDED", "build")  # 10s

    s2 = sessions.Session("feat", "proj")
    s2.record_event("NODE_STARTED", "verify")
    s2.record_event("NODE_SUCCEEDED", "verify")  # 1s

    result = sessions.slowest_nodes_across_recent(limit=20)
    assert result[0] == {"node_id": "build", "total_seconds": 10.0, "run_count": 1}
    assert result[1] == {"node_id": "verify", "total_seconds": 1.0, "run_count": 1}


def test_build_report_summarizes_a_completed_run():
    s = sessions.Session("feat", "proj")
    s.node_succeeded("plan", {"title": "x", "constraints": []})
    s.node_succeeded("build", {"files": [{"path": "src/a.js", "content": "x"}]})
    s.node_succeeded("verify", {"files": [{"path": "test/a.test.js", "content": "x"}]})
    s.node_succeeded("test_gate", {"passed": True})
    s.node_succeeded("review", {"approved": True, "issues": [], "summary": "ok"})
    s.node_succeeded("calibrate", {"patterns": ["a pattern"]})
    s.finish("completed")

    report = sessions.build_report(s.data)
    assert report["status"] == "completed"
    assert report["files_written"]["implementation"] == ["src/a.js"]
    assert report["files_written"]["tests"] == ["test/a.test.js"]
    assert report["test_result"] == {"passed": True}
    assert report["calibrated_patterns"] == ["a pattern"]


def test_total_tokens_sums_every_node_regardless_of_kind():
    s = sessions.Session("feat", "proj")
    s.node_succeeded("plan", {"title": "x"}, tokens={"input": 100, "output": 50})
    s.node_succeeded("build", {"files": []}, tokens={"input": 200, "output": 75})
    # a gate's tokens are usually None (no LLM call) — must not blow up
    s.node_succeeded("test_gate", {"passed": True}, tokens=None)

    totals = sessions.total_tokens(s.data)
    assert totals == {"input": 300, "output": 125, "total": 425}


def test_total_tokens_is_zero_for_a_session_with_no_nodes_yet():
    s = sessions.Session("feat", "proj")
    assert sessions.total_tokens(s.data) == {"input": 0, "output": 0, "total": 0}


def test_build_report_includes_the_token_rollup():
    s = sessions.Session("feat", "proj")
    s.node_succeeded("plan", {"title": "x"}, tokens={"input": 10, "output": 5})
    s.finish("failed")

    report = sessions.build_report(s.data)
    assert report["tokens"] == {"input": 10, "output": 5, "total": 15}


def test_list_sessions_surfaces_a_per_run_token_total():
    s = sessions.Session("feat", "proj")
    s.node_succeeded("plan", {"title": "x"}, tokens={"input": 10, "output": 5})
    s.finish("failed")

    row = next(r for r in sessions.list_sessions() if r["id"] == s.id)
    assert row["tokens"] == 15
