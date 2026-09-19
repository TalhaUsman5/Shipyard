"""
Regression coverage for graph.py's PARALLEL_GROUPS: "build" and "verify"
must actually run concurrently, not just both eventually get called in
either order — and a solo retry that resets only one of them must still
run only that one, never both.
"""
import threading
import time

import agents
import engine
import schemas


def test_build_and_verify_actually_run_concurrently(fake_agents, calls, monkeypatch, make_repo):
    """A threading.Barrier is a much stronger proof than comparing sleep
    intervals: if build/verify ran sequentially (the regression this test
    guards against), the SECOND call would be the only one to ever reach
    the barrier, time out waiting for a partner that already finished, and
    raise BrokenBarrierError — which surfaces as this run ending "failed"
    instead of "completed", not a flaky timing comparison."""
    make_repo("proj_parallel")
    barrier = threading.Barrier(2, timeout=2.0)

    def concurrent_builder(contract, snapshot, pkg, memory, current_files=None, arbiter_feedback=None, previous_review=None, snapshot_truncated=False, omitted_current_files=0):
        calls["build"] += 1
        barrier.wait()
        return schemas.BuildOutput(files=[{"path": "src/greeting.js", "content": "// correct"}]), {"input": 1, "output": 1}

    def concurrent_verifier(contract, snapshot, pkg, memory, current_files=None, feedback=None, snapshot_truncated=False, omitted_current_files=0):
        calls["verify"] += 1
        barrier.wait()
        return schemas.TestOutput(files=[{"path": "test/greeting.test.js", "content": "// test"}]), {"input": 1, "output": 1}

    monkeypatch.setattr(agents, "run_builder", concurrent_builder)
    monkeypatch.setattr(agents, "run_verifier", concurrent_verifier)

    session = engine.run_pipeline("add greet helper", "proj_parallel")

    assert session.data["status"] == "completed"
    assert calls.build == 1
    assert calls.verify == 1


def test_bug_retry_reruns_only_build_not_verify(fake_agents, calls, knobs, make_repo):
    """A solo retry that resets only "build" (arbiter's "bug" route) must
    still only rerun build — verify stays cached, even though both are
    members of the same PARALLEL_GROUPS entry."""
    knobs.build_ok_from_call = 2  # first build attempt is wrong, second is correct
    make_repo("proj_parallel_bug_retry")

    session = engine.run_pipeline("add greet helper", "proj_parallel_bug_retry")

    assert session.data["status"] == "completed"
    assert calls.build == 2
    assert calls.verify == 1, "verify must not rerun just because it shares a parallel group with build"


def test_test_gap_retry_reruns_only_verify_not_build(fake_agents, calls, knobs, make_repo):
    """The mirror image: arbiter's "test_gap" resets only "verify" — build
    must stay cached."""
    knobs.arbiter_classification = "test_gap"
    knobs.build_ok_from_call = 2  # would fix it on a rebuild — but test_gap never reruns build
    make_repo("proj_parallel_test_gap_retry")

    session = engine.run_pipeline("add greet helper", "proj_parallel_test_gap_retry")

    assert calls.build == 1, "test_gap must never touch build, even though they share a parallel group"
    assert calls.verify == 2
