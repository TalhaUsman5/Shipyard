"""
Git worktree isolation for Shipyard-run projects.

Every project's canonical directory (workspace/<project>/) becomes a real
git repository the first time any run touches it — whether that project is
brand new (nothing there yet, gets the usual scaffold) or already existed
with real files but no git history (a plain retrofit init + baseline
commit). From then on, an individual RUN never operates directly in that
canonical directory: it gets its own linked git worktree, checked out onto
a session-specific branch, so its Build/Verify file writes are isolated
from whatever else might read or write the canonical directory. On a
successful completion, the worktree's branch is merged back into the
canonical directory's default branch and the worktree is removed; on any
other terminal or paused state (failed, failed_needs_human, cancelled,
awaiting human review) the worktree is left exactly as-is so a later
resume can keep working in it.

Pairs with project_lock.py, which is what actually guarantees only one
session's worktree is ever active per project at a time — that guarantee
is what makes "always merge cleanly on completion" safe to assume here
without any conflict-resolution logic.
"""
import os
import subprocess

DEFAULT_BRANCH = "main"


class WorktreeError(RuntimeError):
    """A git operation this module depends on failed unexpectedly (not the
    ordinary 'nothing to commit' case, which is handled quietly)."""


def _run(args: list, cwd: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result


def _git(args: list, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    result = _run(["git", *args], cwd=cwd)
    if check and result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed in {cwd!r} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def _worktree_path(workspace_dir: str, project_name: str, session_id: str) -> str:
    # Deliberately a TOP-LEVEL sibling directly under workspace_dir — the
    # same level as every real project directory — not nested one level
    # deeper (e.g. workspace/.worktrees/<name>). A project's own code can
    # legitimately reference another project as a sibling via a relative
    # path (release-manager-review-ui's server.js resolves its CLI via
    # path.resolve(__dirname, '../release-manager-v2')); nesting worktrees
    # one level deeper breaks that resolution during a build/test run even
    # though the same relative path works fine in the final deployed
    # product (which runs from the real cloned repo, never a worktree).
    # Found live: a feature request against release-manager-v2 planned a
    # test that spawns release-manager-review-ui's server, which failed
    # this exact way before ever reaching Build.
    return os.path.join(workspace_dir, f"{project_name}__{session_id}")


def _branch_name(session_id: str) -> str:
    return f"session-{session_id}"


def ensure_repo(project_root: str) -> None:
    """Makes project_root a git repository with at least one commit on
    DEFAULT_BRANCH, if it isn't already one. Safe to call every time a run
    starts — a no-op once the repo exists. Covers both cases the same way:
    a brand-new project (engine.py has already scaffolded package.json into
    an empty directory before this runs) and a pre-existing project
    directory that happens to have no git history yet — either way, "make
    a baseline commit of whatever's here right now" is the correct action."""
    if os.path.isdir(os.path.join(project_root, ".git")):
        return
    _git(["init", "--quiet", "-b", DEFAULT_BRANCH], cwd=project_root)
    _git(["add", "-A"], cwd=project_root)
    # Nothing to commit is possible (e.g. an existing project whose only
    # contents are already gitignored) — that's fine, init alone still
    # gives worktree operations a valid repo to branch from.
    status = _git(["status", "--porcelain"], cwd=project_root)
    if status.stdout.strip():
        _git(
            [
                "-c", "user.name=Shipyard",
                "-c", "user.email=shipyard@localhost",
                "commit", "--quiet", "-m", "Baseline commit before the first Shipyard run",
            ],
            cwd=project_root,
        )


def _worktree_already_registered(project_root: str, worktree_path: str) -> bool:
    # Deliberately not string-matching `git worktree list`'s own path
    # output against worktree_path: git normalizes to forward slashes
    # there even on Windows, while os.path.join here uses the platform
    # separator, so a naive comparison silently never matches on Windows
    # (found live — create_worktree kept trying to re-add an existing
    # worktree on every resume instead of reusing it). A linked worktree's
    # own .git is a FILE (pointing back at the main repo's
    # .git/worktrees/<name>), not a directory, which is a simpler and
    # separator-proof way to confirm this is a real, live worktree.
    git_marker = os.path.join(worktree_path, ".git")
    return os.path.isfile(git_marker)


def create_worktree(workspace_dir: str, project_root: str, project_name: str, session_id: str) -> str:
    """Returns the path to this session's worktree, creating it (and the
    repo, if needed) on first call. A second call for the SAME session_id
    (a resume) reuses the existing worktree rather than erroring — git
    worktree add would fail outright on an already-checked-out branch."""
    ensure_repo(project_root)
    worktree_path = _worktree_path(workspace_dir, project_name, session_id)
    if os.path.isdir(worktree_path) and _worktree_already_registered(project_root, worktree_path):
        return worktree_path

    branch = _branch_name(session_id)
    existing_branches = _git(["branch", "--list", branch], cwd=project_root).stdout
    if existing_branches.strip():
        # Branch survived a worktree directory that got removed out from
        # under git (e.g. manual cleanup) — reattach rather than fail.
        _git(["worktree", "add", worktree_path, branch], cwd=project_root)
    else:
        _git(["worktree", "add", "-b", branch, worktree_path, DEFAULT_BRANCH], cwd=project_root)
    return worktree_path


def _commit_worktree_changes(worktree_path: str, session_id: str) -> None:
    _git(["add", "-A"], cwd=worktree_path)
    status = _git(["status", "--porcelain"], cwd=worktree_path)
    if not status.stdout.strip():
        return
    _git(
        [
            "-c", "user.name=Shipyard",
            "-c", "user.email=shipyard@localhost",
            "commit", "--quiet", "-m", f"Session {session_id} results",
        ],
        cwd=worktree_path,
    )


def merge_worktree(workspace_dir: str, project_root: str, project_name: str, session_id: str) -> None:
    """Commits any uncommitted work in the session's worktree, merges its
    branch into DEFAULT_BRANCH from the canonical project directory, then
    removes the worktree. Only called on a session reaching "completed" —
    project_lock.py's guarantee that no other session's worktree exists for
    this project at the same time is what makes this safe as a plain merge
    with no conflict handling."""
    worktree_path = _worktree_path(workspace_dir, project_name, session_id)
    if not os.path.isdir(worktree_path):
        return  # Already merged/cleaned up — resuming a completed session, or a retry.

    _commit_worktree_changes(worktree_path, session_id)
    branch = _branch_name(session_id)
    _git(["checkout", "--quiet", DEFAULT_BRANCH], cwd=project_root)
    _git(["merge", "--quiet", "--no-ff", "-m", f"Merge session {session_id}", branch], cwd=project_root)
    _git(["worktree", "remove", "--force", worktree_path], cwd=project_root)
