"""
The "plan_review" human_gate: pauses after "plan", before "build", for a
live approve/reject decision — replacing an earlier automated "scope_gate"
(see graph.py's module docstring). These tests use `fake_agents_raw` (NOT
`fake_agents`), since the whole point is to observe the pause/decision
mechanics that `fake_agents`'s auto-approval otherwise hides.

Also covers the background-thread entry points (start_pipeline/
start_resume/submit_review) and cancellation, added alongside plan_review
so the dashboard's "submit a run" / "cancel a run" / "review the plan"
controls all have somewhere they don't have to block on a real LLM call.
"""
import time

import pytest

import agents
import engine
import runtime
import sessions


def _status(session_id):
    return sessions.load_session(session_id)["status"]


def _poll(predicate, timeout=2.0, interval=0.01):
    """Polls a zero-arg predicate until it's truthy or `timeout` elapses.
    Only used against the fake agents (near-instant, no real LLM/network
    calls), so a couple of seconds is generous headroom, not a real wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_plan_review_pauses_after_plan_before_build(fake_agents_raw, calls, make_repo):
    make_repo("proj_pr_pause")

    session = engine.run_pipeline("add greet helper", "proj_pr_pause")

    assert session.data["status"] == "awaiting_review"
    assert session.data["awaiting_node"] == "plan_review"
    assert calls.plan == 1
    assert calls.build == 0, "build must not run before plan_review is approved"
    assert session.data["nodes"]["plan"]["output"]["title"] == "Add greet helper"


def test_plan_review_approval_proceeds_to_build_and_completes(fake_agents_raw, calls, make_repo):
    make_repo("proj_pr_approve")
    session = engine.run_pipeline("add greet helper", "proj_pr_approve")

    resumed = engine.apply_review(session.id, approved=True, reviewed_by="alice")

    assert resumed.data["status"] == "completed"
    assert calls.plan == 1, "an approval must not trigger a replan"
    assert calls.build == 1
    decision = resumed.data["nodes"]["plan_review"]["output"]
    assert decision == {"approved": True, "feedback": None, "reviewed_by": "alice"}


def test_plan_review_rejection_reruns_plan_with_feedback(fake_agents_raw, calls, make_repo):
    make_repo("proj_pr_reject")
    session = engine.run_pipeline("add greet helper", "proj_pr_reject")

    resumed = engine.apply_review(
        session.id, approved=False, feedback="drop the CLI requirement, only a library function was asked for"
    )

    assert resumed.data["status"] == "awaiting_review", "a rejection must pause again for the NEW contract"
    assert calls.plan == 2, "rejection must reset + rerun plan"
    assert calls.build == 0, "build must still not run after a rejection"

    planner_feedback = fake_agents_raw["planner_refinement_feedback"]
    assert planner_feedback[0] is None, "no feedback exists before the first plan attempt"
    assert planner_feedback[1] == "drop the CLI requirement, only a library function was asked for"


def test_plan_review_has_no_retry_budget(fake_agents_raw, calls, make_repo):
    """A human paces how many refinement rounds happen — there's no
    budget_key to exhaust, unlike every LLM-driven retry route."""
    make_repo("proj_pr_unbounded")
    session = engine.run_pipeline("add greet helper", "proj_pr_unbounded")

    for _ in range(5):
        session = engine.apply_review(session.id, approved=False, feedback="still not narrow enough")
        assert session.data["status"] == "awaiting_review"

    assert calls.plan == 6
    assert session.data["budgets"].get("scope_retry", 0) == 0


def test_apply_review_requires_feedback_on_rejection(fake_agents_raw, make_repo):
    make_repo("proj_pr_no_feedback")
    session = engine.run_pipeline("add greet helper", "proj_pr_no_feedback")

    with pytest.raises(ValueError):
        engine.apply_review(session.id, approved=False, feedback="")
    with pytest.raises(ValueError):
        engine.apply_review(session.id, approved=False, feedback=None)


def test_apply_review_rejects_a_node_id_mismatch(fake_agents_raw, make_repo):
    make_repo("proj_pr_mismatch")
    session = engine.run_pipeline("add greet helper", "proj_pr_mismatch")

    with pytest.raises(ValueError):
        engine.apply_review(session.id, node_id="build", approved=True)


def test_apply_review_rejects_a_session_not_awaiting_review(fake_agents, make_repo):
    """Uses the auto-approving `fake_agents`, so this session runs to
    completion with no pending review left to act on."""
    make_repo("proj_pr_not_awaiting")
    session = engine.run_pipeline("add greet helper", "proj_pr_not_awaiting")
    assert session.data["status"] == "completed"

    with pytest.raises(ValueError):
        engine.apply_review(session.id, approved=True)


def test_apply_review_missing_session_returns_none():
    assert engine.apply_review("does-not-exist", approved=True) is None


def test_an_already_approved_plan_review_is_not_re_asked_on_resume(fake_agents_raw, calls, make_repo):
    """A crash later in the pipeline (simulated here, same as the existing
    resume tests) must not re-pause for a decision on a contract a human
    already approved — see graph.py's docstring on why plan_review's
    approved status IS trusted across a resume, unlike an ordinary gate."""
    make_repo("proj_pr_resume")
    session = engine.run_pipeline("add greet helper", "proj_pr_resume")
    session = engine.apply_review(session.id, approved=True)
    assert session.data["status"] == "completed"

    session.data["nodes"].pop("calibrate", None)
    session.data["status"] = "failed"
    session.save()
    for k in calls:
        calls[k] = 0

    resumed = engine.resume_pipeline(session.id)

    assert resumed.data["status"] == "completed"
    assert calls.plan == 0, "plan must stay skipped on resume"
    assert calls.build == 0, "build must stay skipped on resume"
    assert resumed.data["nodes"]["plan_review"]["attempts"] == 1, "no fresh review round was started"


def test_start_pipeline_returns_immediately_then_pauses_for_review(fake_agents_raw, calls, make_repo):
    make_repo("proj_pr_start")

    session = engine.start_pipeline("add greet helper", "proj_pr_start")
    # start_pipeline hands back the session before the background thread
    # necessarily finishes even the first node — this only proves the call
    # didn't block on the walk itself.
    assert session.data["status"] == "running"

    assert _poll(lambda: _status(session.id) == "awaiting_review")
    # join_background, not just the status poll above: a poll only proves
    # the thread's session.finish()-equivalent write already landed, not
    # that the thread has fully exited — see engine.join_background's
    # docstring for why that gap matters (it let a stray session leak into
    # the real sessions/ directory once, when this test's own tmp-dir
    # monkeypatch reverted a moment before the thread's last statements ran).
    assert engine.join_background(session.id)
    assert calls.plan == 1


def test_submit_review_runs_the_continuation_in_the_background(fake_agents_raw, calls, make_repo):
    make_repo("proj_pr_submit")
    session = engine.run_pipeline("add greet helper", "proj_pr_submit")

    engine.submit_review(session.id, approved=True)

    assert _poll(lambda: _status(session.id) == "completed")
    assert engine.join_background(session.id)
    assert calls.build == 1


def test_cancel_run_stops_a_running_walk_and_leaves_it_resumable(fake_agents_raw, calls, monkeypatch, make_repo):
    make_repo("proj_cancel")

    def slow_cancelable_builder(contract, snapshot, pkg, memory, current_files=None, arbiter_feedback=None, previous_review=None, snapshot_truncated=False, omitted_current_files=0):
        calls["build"] += 1
        sink = runtime.get_sink()
        for _ in range(500):
            if sink and sink.cancel_event is not None and sink.cancel_event.is_set():
                raise runtime.Cancelled("cancelled in fake builder")
            time.sleep(0.01)
        raise AssertionError("cancel_run never took effect")

    monkeypatch.setattr(agents, "run_builder", slow_cancelable_builder)

    session = engine.run_pipeline("add greet helper", "proj_cancel")
    engine.submit_review(session.id, approved=True)
    assert _poll(lambda: calls.build >= 1)

    signalled = engine.cancel_run(session.id)
    assert signalled is True

    assert _poll(lambda: _status(session.id) == "cancelled", timeout=5.0)
    assert engine.join_background(session.id)
    assert "cancelled" in sessions.RESUMABLE_STATUSES
