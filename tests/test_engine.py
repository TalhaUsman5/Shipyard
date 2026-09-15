"""
Core engine behavior: gates, bounded retries, resume, and new-repo
scaffolding. Everything here runs against monkeypatched agents/execution
(see conftest.py's fake_agents) — no real LLM call, no real npm/Node
required, and every workspace/session directory is a pytest tmp_path.
"""
import json
import os

import pytest

import engine
import sessions


def test_bug_retry_recovers_without_rerunning_verify(fake_agents, calls, knobs, make_repo):
    knobs.build_ok_from_call = 2  # first build is wrong, second is correct
    make_repo("proj1")

    session = engine.run_pipeline("add greet helper", "proj1")

    assert session.data["status"] == "completed"
    assert calls.build == 2
    assert calls.verify == 1, "verify must not rerun on a bug retry — the tests weren't wrong"
    assert calls.arbiter == 1


def test_bug_retry_threads_current_files_and_arbiter_feedback(fake_agents, calls, knobs, make_repo):
    knobs.build_ok_from_call = 2
    make_repo("proj1b")

    engine.run_pipeline("add greet helper", "proj1b")

    current_files = fake_agents["current_files"]
    arbiter_feedback = fake_agents["build_arbiter_feedback"]
    assert len(current_files) == 2

    assert arbiter_feedback[0] is None, "no arbiter has run before the first build attempt"
    assert arbiter_feedback[1] == "fix the greeting string", "a bug retry must see the Arbiter's diagnosis"

    # the retry sees what's REALLY on disk (the first attempt's actual
    # wrong output), not a stale/reconstructed copy
    content_by_path = {f["path"]: f["content"] for f in current_files[1]}
    assert content_by_path["src/greeting.js"] == "// wrong"

    # and never the test file the Verifier already wrote by this point —
    # the Builder must not see test code, even via this full-directory read
    assert "test/greeting.test.js" not in content_by_path

    assert calls.review == 1
    assert calls.calibrate == 1


def test_bug_budget_exhaustion_terminates_as_failed_needs_human(fake_agents, calls, knobs, make_repo):
    knobs.build_ok_from_call = 99  # never gets fixed
    make_repo("proj2")

    session = engine.run_pipeline("add greet helper", "proj2")

    assert session.data["status"] == "failed_needs_human"
    assert calls.build == 5  # 1 initial + 4 retries (build_retry budget = 4)
    assert session.data["budgets"]["build_retry"] == 4
    assert calls.review == 0
    assert calls.calibrate == 0


def test_grant_lets_an_exhausted_budget_recover_without_editing_graph_py(fake_agents, calls, knobs, make_repo):
    """The whole point of grants: an operator unblocks a stuck session by
    calling resume with a grant, not by editing graph.py and asking an
    engineer to redeploy."""
    knobs.build_ok_from_call = 99  # never fixed — exhausts build_retry (max 4)
    make_repo("proj_grant")

    session = engine.run_pipeline("add greet helper", "proj_grant")
    assert session.data["status"] == "failed_needs_human"
    assert session.data["budgets"]["build_retry"] == 4

    knobs.build_ok_from_call = 1  # the next real build attempt will succeed
    resumed = engine.resume_pipeline(
        session.id, grants={"build_retry": 2}, grant_reason="unblock the demo", granted_by="operator"
    )

    assert resumed is not None
    assert resumed.data["status"] == "completed"
    assert resumed.data["budget_grants"]["build_retry"] == 2
    assert resumed.data["budgets"]["build_retry"] == 5  # 4 old uses + 1 more, now under the raised ceiling

    events = sessions.load_event_log(session.id)
    granted = [e for e in events if e["type"] == "BUDGET_GRANTED"]
    assert len(granted) == 1
    assert granted[0]["data"] == {
        "budget_key": "build_retry",
        "amount": 2,
        "new_grant_total": 2,
        "reason": "unblock the demo",
        "granted_by": "operator",
    }


def test_grant_without_a_reason_is_rejected(fake_agents, calls, knobs, make_repo):
    knobs.build_ok_from_call = 99
    make_repo("proj_grant_no_reason")
    session = engine.run_pipeline("add greet helper", "proj_grant_no_reason")

    with pytest.raises(ValueError):
        engine.resume_pipeline(session.id, grants={"build_retry": 2}, grant_reason=None)


def test_ambiguity_retry_recovers_by_replanning_rebuilding_reverifying(fake_agents, calls, knobs, make_repo):
    knobs.arbiter_classification = "ambiguity"
    knobs.build_ok_from_call = 2  # fixed once plan/build/verify are redone
    make_repo("proj_ambig_ok")

    session = engine.run_pipeline("add greet helper", "proj_ambig_ok")

    assert session.data["status"] == "completed"
    assert calls.plan == 2, "ambiguity must reset + rerun plan, not just build"
    assert calls.build == 2
    assert calls.verify == 2, "ambiguity resets verify too — the tests get re-derived, not just the contract"
    assert calls.arbiter == 1
    assert session.data["budgets"]["ambiguity_retry"] == 1


def test_ambiguity_budget_exhaustion_terminates_as_failed_needs_human(fake_agents, calls, knobs, make_repo):
    knobs.arbiter_classification = "ambiguity"
    knobs.build_ok_from_call = 99  # never gets fixed
    make_repo("proj_ambig_exhausted")

    session = engine.run_pipeline("add greet helper", "proj_ambig_exhausted")

    assert session.data["status"] == "failed_needs_human"
    assert calls.arbiter == 2  # 1 initial + 1 retry (ambiguity_retry budget = 1)
    assert session.data["budgets"]["ambiguity_retry"] == 1
    assert calls.review == 0
    assert calls.calibrate == 0


def test_arbiter_test_gap_reruns_verify_never_touches_build(fake_agents, calls, knobs, make_repo):
    """A broken test fixture (found live once — see graph.py's test_gap
    docstring) must route to Verifier, never Builder — Builder never sees
    test code and could not fix this no matter how many attempts it got."""
    knobs.arbiter_classification = "test_gap"
    knobs.build_ok_from_call = 2  # would fix it on a rebuild — but test_gap never reruns build
    make_repo("proj_arbiter_test_gap")

    session = engine.run_pipeline("add greet helper", "proj_arbiter_test_gap")

    # test_gate's pass/fail depends only on build's (unchanged) content in
    # this fake harness, so it fails again after the rewrite and the
    # single shared test_retry use gets exhausted — the real assertion
    # here is WHAT got retried, not whether the run converged.
    assert session.data["status"] == "failed_needs_human"
    assert calls.build == 1, "test_gap must never touch build"
    assert calls.verify == 2, "test_gap must reset + rerun verify"
    assert calls.arbiter == 2  # 1 initial + 1 retry (test_retry budget = 1)
    assert session.data["budgets"]["test_retry"] == 1
    assert calls.review == 0

    verifier_feedback = fake_agents["verifier_feedback"]
    assert verifier_feedback[0] is None
    assert verifier_feedback[1] == "fix the broken test fixture"


def test_verify_retry_sees_its_own_prior_test_files(fake_agents, calls, knobs, make_repo):
    """Without this, a Verifier retry has no idea a prior attempt's test
    file(s) already exist on disk, so it tends to invent a new filename
    instead of fixing the old one — leaving the stale file behind for
    `node --test` to keep discovering and running forever."""
    knobs.arbiter_classification = "test_gap"
    knobs.build_ok_from_call = 2  # forces the first test_gate to fail, so test_gap fires and verify retries
    make_repo("proj_verify_sees_current_files")

    engine.run_pipeline("add greet helper", "proj_verify_sees_current_files")

    current_files = fake_agents["verifier_current_files"]
    assert current_files[0] is None or current_files[0] == [], "no test file exists before the first verify"
    assert current_files[1], "a retry must see the test file the first verify already wrote"
    assert current_files[1][0]["path"] == "test/greeting.test.js"


def test_resume_skips_succeeded_work_nodes_but_reruns_gates(fake_agents, calls, knobs, make_repo):
    make_repo("proj3")
    session = engine.run_pipeline("add greet helper", "proj3")
    assert session.data["status"] == "completed"

    # simulate a crash right before calibrate ran
    session.data["nodes"].pop("calibrate", None)
    session.data["status"] = "failed"
    session.save()

    for k in calls:
        calls[k] = 0
    resumed = engine.resume_pipeline(session.id)

    assert resumed is not None
    assert resumed.data["status"] == "completed"
    assert calls.plan == 0 and calls.build == 0 and calls.verify == 0 and calls.review == 0
    assert calls.test_run == 1, "gates always re-execute for real, even on resume"
    assert calls.calibrate == 1


def test_resume_rejects_a_completed_session(fake_agents, calls, knobs, make_repo):
    make_repo("proj_completed")
    session = engine.run_pipeline("add greet helper", "proj_completed")
    assert session.data["status"] == "completed"

    with pytest.raises(ValueError):
        engine.resume_pipeline(session.id)


def test_resume_missing_session_returns_none():
    assert engine.resume_pipeline("does-not-exist") is None


def test_review_rejection_bounded_retry_threads_feedback_to_builder(fake_agents, calls, knobs, make_repo):
    knobs.review_approved_from_call = 99  # never approves
    make_repo("proj4")

    session = engine.run_pipeline("add greet helper", "proj4")

    assert session.data["status"] == "failed_needs_human"
    assert calls.build == 7  # 1 initial + 6 review_retry uses
    assert calls.review == 7
    assert calls.verify == 1, "a review rejection doesn't invalidate the already-written tests"
    assert session.data["budgets"]["review_retry"] == 6

    # feedback now flows through previous_review/arbiter_feedback (durable,
    # persisted node output) instead of an ever-growing contract.constraints
    # list — the contract itself must stay exactly as the Planner wrote it
    contract = session.data["nodes"]["plan"]["output"]
    assert contract["constraints"] == [], "rejection feedback must not accumulate onto the contract anymore"

    # every retry after the first rejection must see that rejection
    build_previous_review = fake_agents["build_previous_review"]
    assert build_previous_review[0] is None, "no review has happened before the first build attempt"
    assert all(r is not None and r.approved is False for r in build_previous_review[1:])


def test_review_rejection_then_approval_completes(fake_agents, calls, knobs, make_repo):
    knobs.review_approved_from_call = 2
    make_repo("proj5")

    session = engine.run_pipeline("add greet helper", "proj5")

    assert session.data["status"] == "completed"
    assert calls.review == 2


def test_review_rejection_threads_previous_review_into_the_retry(fake_agents, calls, knobs, make_repo):
    knobs.review_approved_from_call = 2
    make_repo("proj5b")

    engine.run_pipeline("add greet helper", "proj5b")

    previous_reviews = fake_agents["previous_reviews"]
    assert len(previous_reviews) == 2
    assert previous_reviews[0] is None, "the first review has no prior verdict to check against"


def test_scope_creep_recovers_by_replanning_rebuilding_reverifying(fake_agents, calls, knobs, make_repo):
    knobs.review_arbiter_classification = "scope_creep"
    knobs.review_approved_from_call = 2  # approved once the narrowed contract's second build lands
    make_repo("proj_scope_ok")

    session = engine.run_pipeline("add greet helper", "proj_scope_ok")

    assert session.data["status"] == "completed"
    assert calls.plan == 2, "scope_creep must reset + rerun plan, not just build"
    assert calls.build == 2
    assert calls.verify == 2, "scope_creep resets verify too — tests get re-derived against the narrowed contract"
    assert calls.review_arbiter == 1
    assert session.data["budgets"]["scope_retry"] == 1


def test_scope_creep_budget_exhaustion_terminates_as_failed_needs_human(fake_agents, calls, knobs, make_repo):
    knobs.review_arbiter_classification = "scope_creep"
    knobs.review_approved_from_call = 99  # never approves, even after the narrowed contract
    make_repo("proj_scope_exhausted")

    session = engine.run_pipeline("add greet helper", "proj_scope_exhausted")

    assert session.data["status"] == "failed_needs_human"
    assert calls.review_arbiter == 3  # 1 initial + 2 retries (scope_retry budget = 2)
    assert session.data["budgets"]["scope_retry"] == 2
    assert calls.calibrate == 0


def test_test_gap_recovers_by_reverifying_not_rebuilding(fake_agents, calls, knobs, make_repo):
    knobs.review_arbiter_classification = "test_gap"
    knobs.review_approved_from_call = 2  # rejected once, approved on the re-review
    make_repo("proj_test_gap_ok")

    session = engine.run_pipeline("add greet helper", "proj_test_gap_ok")

    assert session.data["status"] == "completed"
    assert calls.build == 1, "test_gap must not touch build — the implementation was never the problem"
    assert calls.verify == 2, "test_gap must reset + rerun verify, that's the whole point of the route"
    assert calls.review == 2
    assert calls.review_arbiter == 1
    assert session.data["budgets"]["test_retry"] == 1

    verifier_feedback = fake_agents["verifier_feedback"]
    assert verifier_feedback[0] is None, "the first verify has no prior rejection to respond to"
    assert verifier_feedback[1] == "fix the broken test double"


def test_test_gap_budget_exhaustion_terminates_as_failed_needs_human(fake_agents, calls, knobs, make_repo):
    knobs.review_arbiter_classification = "test_gap"
    knobs.review_approved_from_call = 99  # never approves
    make_repo("proj_test_gap_exhausted")

    session = engine.run_pipeline("add greet helper", "proj_test_gap_exhausted")

    assert session.data["status"] == "failed_needs_human"
    assert calls.build == 1, "test_gap must never touch build, even exhausted"
    assert calls.review_arbiter == 2  # 1 initial + 1 retry (test_retry budget = 1)
    assert session.data["budgets"]["test_retry"] == 1
    assert calls.calibrate == 0


def test_new_repo_is_scaffolded_existing_repo_is_untouched(isolated_dirs):
    resolved = engine._resolve_project_path("brand_new")
    pkg_path = os.path.join(resolved, "package.json")
    assert os.path.isfile(pkg_path)
    with open(pkg_path, encoding="utf-8") as f:
        pkg = json.load(f)
    assert pkg["scripts"]["test"] == "node --test"
    assert pkg["name"] == "brand_new"

    # resolving it again (now that it exists) must not re-scaffold
    with open(pkg_path, "w", encoding="utf-8") as f:
        json.dump({"name": "custom", "scripts": {"test": "custom-cmd"}}, f)
    engine._resolve_project_path("brand_new")
    with open(pkg_path, encoding="utf-8") as f:
        pkg2 = json.load(f)
    assert pkg2["scripts"]["test"] == "custom-cmd"


def test_resolve_project_path_rejects_absolute_and_escaping_paths(isolated_dirs):
    with pytest.raises(ValueError):
        engine._resolve_project_path(os.path.join(isolated_dirs["workspace"], "abs"))
    with pytest.raises(ValueError):
        engine._resolve_project_path("../outside_workspace")


def test_trace_log_records_graph_snapshot_and_route_events(fake_agents, calls, knobs, make_repo):
    knobs.build_ok_from_call = 2
    make_repo("proj6")

    session = engine.run_pipeline("add greet helper", "proj6")

    events = sessions.load_event_log(session.id)
    assert events[0]["type"] == "GRAPH_SNAPSHOT"
    assert events[0]["data"]["entry_node"] == "plan"
    route_events = [e for e in events if e["type"] == "ROUTE_TAKEN"]
    assert any(
        e["data"]["target"] == "build" and e["data"]["budget_key"] == "build_retry" for e in route_events
    )


def test_trace_log_records_budget_exhaustion(fake_agents, calls, knobs, make_repo):
    knobs.build_ok_from_call = 99
    make_repo("proj7")

    session = engine.run_pipeline("add greet helper", "proj7")

    events = sessions.load_event_log(session.id)
    exhausted = [e for e in events if e["type"] == "ROUTE_BUDGET_EXHAUSTED"]
    assert len(exhausted) == 1
    assert exhausted[0]["data"]["budget_key"] == "build_retry"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["data"]["status"] == "failed_needs_human"


def test_resume_emits_a_second_graph_snapshot(fake_agents, calls, knobs, make_repo):
    make_repo("proj8")
    session = engine.run_pipeline("add greet helper", "proj8")
    session.data["nodes"].pop("calibrate", None)
    session.data["status"] = "failed"
    session.save()

    engine.resume_pipeline(session.id)

    events = sessions.load_event_log(session.id)
    snapshots = [e for e in events if e["type"] == "GRAPH_SNAPSHOT"]
    assert len(snapshots) == 2
    assert any(e["type"] == "RUN_RESUMED" for e in events)


def test_scope_gate_in_scope_proceeds_straight_to_build(fake_agents, calls, knobs, make_repo):
    make_repo("proj_scope_ok")

    session = engine.run_pipeline("add greet helper", "proj_scope_ok")

    assert session.data["status"] == "completed"
    assert calls.scope_check == 1
    assert calls.plan == 1, "an in-scope contract must not trigger a replan"
    assert session.data["budgets"].get("scope_retry", 0) == 0


def test_scope_gate_failure_triggers_replan_and_consumes_scope_retry(fake_agents, calls, knobs, make_repo):
    knobs.scope_check_in_scope = False
    make_repo("proj_scope_bad")

    session = engine.run_pipeline("add greet helper", "proj_scope_bad")

    assert session.data["status"] == "failed_needs_human"
    assert calls.scope_check == 3, "1 initial + 2 retries (scope_retry budget = 2), always failing here"
    assert calls.plan == 3, "scope_gate's on_fail resets plan"
    assert calls.build == 0, "scope_gate must catch an over-scoped contract before build ever runs"
    assert session.data["budgets"]["scope_retry"] == 2


def test_scope_gate_and_review_arbiters_scope_creep_share_one_budget_counter(
    fake_agents, calls, knobs, make_repo
):
    # scope_gate always passes here, so the run's own scope_retry uses are
    # driven entirely by review_arbiter's "scope_creep" route — this just
    # confirms the two routes read/write the SAME budget key ("scope_retry"
    # in graph.py for both), and that scope_gate re-runs on every "plan"
    # attempt the shared counter causes, including ones triggered by
    # scope_creep rather than by scope_gate itself.
    knobs.scope_check_in_scope = True
    knobs.review_arbiter_classification = "scope_creep"
    knobs.review_approved_from_call = 99
    make_repo("proj_shared_budget")

    session = engine.run_pipeline("add greet helper", "proj_shared_budget")

    assert session.data["status"] == "failed_needs_human"
    assert session.data["budgets"]["scope_retry"] == 2
    assert calls.scope_check == 3, "scope_gate re-runs on every plan retry, including scope_creep's"
