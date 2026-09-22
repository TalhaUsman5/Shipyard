"""
Git worktree isolation (worktrees.py) and per-project concurrency locking
(project_lock.py), wired into engine.py's _build_context/_execute_walk.

Every project becomes a real git repo the first time a run touches it —
whether brand new (make_repo's placeholder file) or already had real
files before Shipyard ever saw it (retrofit, no meaningful difference in
mechanics — same ensure_repo() call either way). A run's actual work
happens in its own linked worktree, merged back into the project's
canonical directory only once the session reaches "completed"; a second
session can never start against a project whose first session hasn't
reached that point yet.
"""
import os

import pytest

import engine
import project_lock
import sessions
import worktrees


def test_new_project_becomes_a_real_git_repo_with_a_completed_run_merged_in(fake_agents, calls, make_repo):
    make_repo("proj_wt_new")
    project_root = os.path.join(engine.WORKSPACE_DIR, "proj_wt_new")

    assert not os.path.isdir(os.path.join(project_root, ".git"))

    session = engine.run_pipeline("add greet helper", "proj_wt_new")

    assert session.data["status"] == "completed"
    assert os.path.isdir(os.path.join(project_root, ".git"))
    # The Builder's real output landed in the canonical directory itself,
    # not just in a worktree nobody merged back.
    assert os.path.isfile(os.path.join(project_root, "src", "greeting.js"))


def test_completed_runs_worktree_is_removed_after_merge(fake_agents, calls, make_repo):
    make_repo("proj_wt_cleanup")
    session = engine.run_pipeline("add greet helper", "proj_wt_cleanup")

    worktree_path = worktrees._worktree_path(engine.WORKSPACE_DIR, "proj_wt_cleanup", session.id)
    assert not os.path.isdir(worktree_path)


def test_a_second_run_is_blocked_while_the_first_is_still_awaiting_review(fake_agents_raw, calls, make_repo):
    make_repo("proj_wt_locked")

    first = engine.run_pipeline("add greet helper", "proj_wt_locked")
    assert first.data["status"] == "awaiting_review"

    second = engine.run_pipeline("add a second, unrelated feature", "proj_wt_locked")

    assert second.data["status"] == "failed"
    events = sessions.load_event_log(second.id)
    error_events = [e for e in events if e["type"] == "ERROR"]
    assert len(error_events) == 1
    assert "locked" in error_events[0]["data"]["error"]
    assert calls.plan == 1, "the second session must never even reach the Planner"


def test_a_second_run_succeeds_once_the_first_actually_completes(fake_agents, calls, make_repo):
    make_repo("proj_wt_sequential")

    first = engine.run_pipeline("add greet helper", "proj_wt_sequential")
    assert first.data["status"] == "completed"

    second = engine.run_pipeline("add a second, unrelated feature", "proj_wt_sequential")
    assert second.data["status"] == "completed"


def test_a_resumed_session_reuses_its_own_worktree_not_a_fresh_one(fake_agents, calls, knobs, make_repo):
    knobs.build_ok_from_call = 99  # exhausts build_retry, lands on failed_needs_human
    make_repo("proj_wt_resume")

    session = engine.run_pipeline("add greet helper", "proj_wt_resume")
    assert session.data["status"] == "failed_needs_human"

    worktree_path = worktrees._worktree_path(engine.WORKSPACE_DIR, "proj_wt_resume", session.id)
    assert os.path.isdir(worktree_path), "the worktree must survive a failed_needs_human stop, for a later resume"

    project_root = os.path.join(engine.WORKSPACE_DIR, "proj_wt_resume")
    lock_file = project_lock._lock_path(project_root)
    assert os.path.isfile(lock_file), "the lock must still be held — this session is not done yet"

    knobs.build_ok_from_call = 1  # the next real build attempt succeeds
    resumed = engine.resume_pipeline(
        session.id, grants={"build_retry": 2}, grant_reason="let it finish", granted_by="operator"
    )

    assert resumed.data["status"] == "completed"
    assert not os.path.isdir(worktree_path), "merged and cleaned up now that it's actually done"
    assert not os.path.isfile(lock_file), "released now that the session is completed"


def test_ensure_repo_retrofits_a_pre_existing_ungit_project(make_repo, isolated_dirs):
    project_root = make_repo("proj_wt_retrofit")
    assert not os.path.isdir(os.path.join(project_root, ".git"))

    worktrees.ensure_repo(project_root)

    assert os.path.isdir(os.path.join(project_root, ".git"))
    # The file make_repo seeded before Shipyard ever saw this project must
    # have made it into the baseline commit, not been silently skipped.
    log = worktrees._git(["log", "--name-only", "--format="], cwd=project_root).stdout
    assert "src/greeting.js" in log
