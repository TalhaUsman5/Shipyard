"""
Per-project concurrency lock: guarantees at most one Shipyard session
actively owns a given project at a time.

Holding this lock is what makes worktrees.py's merge-on-completion safe to
do as a plain fast-forward-or-merge with no conflict handling — with the
lock in place, no second session's worktree can ever exist for the same
project while the first is still unresolved.

The lock is a small JSON file inside the project's own canonical
directory (not inside any worktree, since it must be visible regardless of
which session currently owns the project) plus an in-memory per-project
threading.Lock guarding the read-check-write sequence against a genuine
same-process race between two near-simultaneous requests for the same
project. The file is what makes the lock durable across a server restart;
the in-memory lock is only about atomicity within one process's lifetime.

A lock is released only when the owning session reaches "completed" —
every other status (failed, failed_needs_human, cancelled, or still
running/awaiting review) is resumable, so the same session might still
come back for its worktree, and the lock must keep blocking anyone else
until that's genuinely settled. A lock referencing a session that turns
out to already be "completed" (its own release step must have crashed
before it got here) is treated as stale and silently reclaimed — this is
the same class of self-healing this project has applied elsewhere rather
than requiring manual cleanup.
"""
import json
import os
import threading
from typing import Optional

_FILE_LOCKS: dict = {}
_FILE_LOCKS_GUARD = threading.Lock()


class ProjectLocked(RuntimeError):
    """Raised when a different, still-active session already owns this
    project."""


def _lock_path(project_root: str) -> str:
    return os.path.join(project_root, ".shipyard-lock.json")


def _in_memory_lock(project_root: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(project_root)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[project_root] = lock
        return lock


def _read_lock(project_root: str) -> Optional[dict]:
    path = _lock_path(project_root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def acquire(project_root: str, session_id: str, load_session_status) -> None:
    """load_session_status(session_id) -> Optional[str] lets this module
    check another lock-holder's real current status without importing
    sessions.py directly (keeps this module dependency-free and easy to
    unit test with a fake). Raises ProjectLocked if a different, still-
    resumable session already holds the lock."""
    with _in_memory_lock(project_root):
        current = _read_lock(project_root)
        if current is not None and current.get("session_id") != session_id:
            other_status = load_session_status(current["session_id"])
            if other_status is not None and other_status != "completed":
                raise ProjectLocked(
                    f"project is locked by session {current['session_id']!r} (status={other_status!r})"
                )
            # Stale (owner completed, or its record vanished) — reclaim below.

        os.makedirs(project_root, exist_ok=True)
        with open(_lock_path(project_root), "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id}, f)


def release(project_root: str, session_id: str) -> None:
    """Only removes the lock if it's still this session's — never clears a
    lock a different (later) session has since legitimately acquired."""
    with _in_memory_lock(project_root):
        current = _read_lock(project_root)
        if current is not None and current.get("session_id") == session_id:
            try:
                os.remove(_lock_path(project_root))
            except FileNotFoundError:
                pass

