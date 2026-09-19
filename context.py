"""
Layer 2 (context delivery) of the harness — assembles what each role sees
about the target project. Deliberately shallow for v1: a file listing and
package.json, not full file contents (keeps prompts small and cheap).
"""
import json
import os

IGNORE_DIRS = {"node_modules", ".git", "dist", "build", ".claude"}

# Hard ceiling on how many files a single walk will ever collect before
# giving up, independent of max_files below — bounds the cost of walking a
# pathologically large tree even when priority_paths means we can't just
# stop at the first `max_files` entries we see (see project_snapshot).
# Comfortably above any real max_files this harness uses; only matters on
# a genuinely huge, badly-.gitignored directory.
_WALK_CEILING = 5000


def project_snapshot(project_root: str, max_files: int = 200, priority_paths=None):
    """Returns (files, truncated). `truncated` is True when more files
    exist on disk than made it into `files` — the caller (and, via
    agents.py, the model) can then say so explicitly instead of silently
    presenting a partial listing as if it were complete.

    `priority_paths` (typically contract.target_files, once a contract
    exists), when given, are placed first — so if the cap has to drop
    something, it drops directory-walk stragglers before it ever drops a
    file the contract actually names. Without this, truncation follows
    pure os.walk order, which has no relationship to relevance."""
    all_files = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), project_root).replace("\\", "/")
            all_files.append(rel)
            if len(all_files) >= _WALK_CEILING:
                break
        else:
            continue
        break

    if priority_paths:
        priority_set = set(priority_paths)
        ordered = [p for p in all_files if p in priority_set] + [p for p in all_files if p not in priority_set]
    else:
        ordered = all_files

    truncated = len(ordered) > max_files
    return ordered[:max_files], truncated


def describe_snapshot(files: list, truncated: bool, shown_limit: int = 100) -> str:
    """Renders a project file listing for a prompt, honestly labeled.
    Two independent things can cut what a role sees: project_snapshot's
    own max_files cap during the walk (signaled by `truncated`), and this
    function's own `shown_limit` slice of whatever the walk did return —
    both are named here, not silently applied, so a role never mistakes a
    partial listing for a complete one."""
    shown = files[:shown_limit]
    notes = []
    omitted_from_slice = len(files) - len(shown)
    if omitted_from_slice > 0:
        notes.append(f"{omitted_from_slice} more collected file path(s) not shown here")
    if truncated:
        notes.append(f"the scan itself stopped at {len(files)} files — more may exist on disk beyond that")
    label = "Project file listing" + (f" ({'; '.join(notes)})" if notes else " (complete)")
    return f"{label}:\n{json.dumps(shown, indent=2)}"


def read_package_json(project_root: str):
    path = os.path.join(project_root, "package.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return None
    return None
