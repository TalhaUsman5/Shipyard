"""
Layer 5 (execution) of the harness. Every file write is checked against the
project root before it touches disk — no role's output can escape the
target project directory, even via a "../../" path. Tests are run for real
(`npm test`); there is no simulated pass/fail.
"""
import os
import platform
import re
import shutil
import signal
import subprocess


class ExecutionError(Exception):
    pass


def _safe_path(project_root: str, rel_path: str) -> str:
    root = os.path.realpath(project_root)
    target = os.path.realpath(os.path.join(root, rel_path))
    if target != root and not target.startswith(root + os.sep):
        raise ExecutionError(f"Path escapes project root: {rel_path!r}")
    return target


def write_file(project_root: str, rel_path: str, content: str) -> str:
    path = _safe_path(project_root, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def apply_files(project_root: str, file_edits) -> list:
    """file_edits: list of schemas.FileEdit. Returns the relative paths written."""
    written = []
    for edit in file_edits:
        write_file(project_root, edit.path, edit.content)
        written.append(edit.path)
    return written


def apply_files_replacing_directory(project_root: str, dir_prefix: str, file_edits) -> list:
    """Like apply_files, but treats dir_prefix as fully owned by this call:
    any file already on disk under dir_prefix that ISN'T in file_edits is
    deleted first. Use this for a role whose output is always the complete,
    authoritative state of its own directory (e.g. the Verifier and test/),
    not an incremental patch like the Builder's. Without this, a retry that
    picks a new filename instead of overwriting its own prior attempt
    leaves the old file behind — and something like `node --test`, which
    discovers every test-shaped file on disk rather than just the ones a
    caller intended, ends up running every abandoned generation at once
    (observed live: 4 accumulated test files, each spinning up its own
    mock HTTP server and spawning its own child process, blowing well past
    any reasonable timeout)."""
    root = os.path.realpath(project_root)
    prefix = dir_prefix.strip("/\\")
    new_paths = {edit.path.replace("\\", "/").strip("/") for edit in file_edits}

    scan_root = os.path.join(root, prefix)
    for dirpath, _dirnames, filenames in os.walk(scan_root):
        for fn in filenames:
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, root).replace("\\", "/")
            if rel not in new_paths:
                try:
                    os.remove(abs_path)
                except OSError:
                    pass

    return apply_files(project_root, file_edits)


def read_text_files(project_root: str, rel_paths, max_total_bytes: int = 150_000) -> list:
    """Reads current content for the given project-relative paths — used to
    give a role visibility into everything actually on disk right now, not
    just what an earlier LLM call happened to return (which only reflects
    that one call's own delta, not files an even earlier retry wrote and
    this one didn't happen to touch again). Skips anything unreadable as
    UTF-8 text; stops adding files once the total would exceed the size
    cap, rather than truncating a file mid-content."""
    files = []
    total = 0
    for rel_path in rel_paths:
        try:
            path = _safe_path(project_root, rel_path)
        except ExecutionError:
            continue
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        if total + len(content) > max_total_bytes:
            break
        total += len(content)
        # normalize to forward slashes: os.path.relpath (the usual source
        # of rel_paths) returns OS-native separators, and a role's own
        # output paths will consistently use "/" (the JS/npm convention
        # this harness targets) — keep what we show it consistent with that
        files.append({"path": rel_path.replace("\\", "/"), "content": content})
    return files


def _popen_kwargs(cwd: str) -> dict:
    kwargs = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # explicit UTF-8, not text mode's OS-default codepage: on Windows
        # that's cp1252, which crashes decoding npm/node's UTF-8 output
        # (observed: a 0x9d byte with no cp1252 mapping, which silently
        # left result.stdout as None).
        encoding="utf-8",
        errors="replace",
    )
    if platform.system() != "Windows":
        # Puts the child in its own process group so _kill_process_tree can
        # signal the whole tree, not just this one process — see there.
        kwargs["start_new_session"] = True
    return kwargs


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """subprocess.run's `timeout` only kills the single process it started
    — on Windows, `npm test` runs as npm.cmd, which launches node.exe as a
    grandchild that inherits the stdout/stderr pipe handles. Killing just
    npm.cmd leaves that grandchild alive and still holding those pipes
    open, so Popen.communicate() hangs forever waiting for EOF on them —
    the timeout never actually bounds the call. (Observed live: an
    orphaned node.exe survived a "timed out after 60s" run, and the
    harness stayed blocked on a single test_gate node for over an hour.)
    Kill the whole tree instead of just the one process."""
    if platform.system() == "Windows":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_npm(args: list, project_root: str, timeout: int):
    """Runs an npm subprocess with a timeout that actually bounds the call
    (tree-kill included, see _kill_process_tree — plain subprocess.run
    doesn't on Windows). Returns (returncode, stdout, stderr, timed_out);
    returncode is None when it timed out."""
    proc = subprocess.Popen(args, **_popen_kwargs(project_root))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        # Drain whatever's already buffered now that the tree is dead;
        # short grace period only — never wait on this indefinitely too.
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return None, stdout or "", stderr or "", True


def run_tests(project_root: str, timeout: int = 60) -> dict:
    # On Windows, `npm` is npm.cmd — subprocess.Popen(["npm", ...]) without
    # shell=True can't resolve it via CreateProcess (no PATHEXT lookup),
    # and fails with FileNotFoundError even though npm is genuinely on
    # PATH. shutil.which() does the same resolution a shell would, so this
    # works cross-platform without needing shell=True.
    npm_path = shutil.which("npm")
    if npm_path is None:
        return {
            "passed": False,
            "returncode": None,
            "stdout": "",
            "stderr": "npm not found on PATH — is Node.js installed?",
            "structured": {},
        }

    try:
        returncode, stdout, stderr, timed_out = _run_npm([npm_path, "test"], project_root, timeout)
    except FileNotFoundError:
        return {
            "passed": False,
            "returncode": None,
            "stdout": "",
            "stderr": "npm not found on PATH — is Node.js installed?",
            "structured": {},
        }

    if timed_out:
        return {
            "passed": False,
            "returncode": None,
            "stdout": stdout[-4000:],
            "stderr": (stderr[-4000:] + f"\nTest run timed out after {timeout}s").strip(),
            "structured": {},
        }

    outcome = {
        "passed": returncode == 0,
        "returncode": returncode,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
        "structured": {},
    }
    if not outcome["passed"]:
        outcome["structured"] = _try_structured_failure_detail(npm_path, project_root, timeout)
    return outcome


def _try_structured_failure_detail(npm_path: str, project_root: str, timeout: int) -> dict:
    """Best-effort only, and only called when the primary run above already
    failed: re-runs the suite once more with node's TAP reporter forwarded,
    purely to extract per-test failure detail for the Arbiter (does this
    test's failure look like a real assertion mismatch, or did it throw
    something else entirely before any assertion ran — often, but not
    always, a sign the defect is in the test's own setup/fixtures, not the
    implementation). This NEVER affects the authoritative pass/fail result
    from the primary run — if the project's test script isn't `node
    --test` under the hood, forwarding `--test-reporter=tap` may simply do
    nothing or error, and this just returns {}. The Arbiter falls back to
    reasoning from raw stdout/stderr alone, exactly as before this existed."""
    try:
        _returncode, stdout, _stderr, _timed_out = _run_npm(
            [npm_path, "test", "--", "--test-reporter=tap"], project_root, timeout
        )
        return _parse_tap(stdout)
    except Exception:  # noqa: BLE001 - purely best-effort diagnostic enrichment
        return {}


_TAP_TEST_LINE = re.compile(r"^(ok|not ok)\s+\d+\s+-\s+(.*)$")
_TAP_SUMMARY_LINE = re.compile(r"^#\s+(tests|pass|fail)\s+(\d+)$")
_TAP_CODE_LINE = re.compile(r"^\s*code:\s*'?([\w.]+)'?\s*$")
_TAP_ERROR_NAME_LINE = re.compile(r"^\s*name:\s*'?([\w.]+)'?\s*$")


def _parse_tap(text: str) -> dict:
    """Parses node --test's TAP output into a structured per-test
    breakdown. For each failed test, flags whether it looks like a genuine
    assertion failure (code: 'ERR_ASSERTION', or an AssertionError) versus
    some other exception thrown during the test. This is a heuristic for
    the Arbiter to weigh, not a verdict on its own — a non-assertion
    exception CAN still mean a real implementation bug (e.g. code that
    throws when it shouldn't) — but it's a strong, mechanical signal that
    a test never got far enough to check anything, which is exactly the
    shape of "the test's own setup is broken," not "the behavior is wrong."
    Returns {} if the text doesn't look like TAP output at all (e.g. the
    project's test script isn't `node --test`), so a caller can fall back
    to raw stdout/stderr alone."""
    if "TAP version" not in text:
        return {}

    lines = text.splitlines()
    summary = {"total": None, "passed": None, "failed": None}
    failures = []

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        summary_match = _TAP_SUMMARY_LINE.match(stripped)
        if summary_match:
            key = {"tests": "total", "pass": "passed", "fail": "failed"}[summary_match.group(1)]
            summary[key] = int(summary_match.group(2))
            i += 1
            continue

        test_match = _TAP_TEST_LINE.match(stripped)
        if test_match and test_match.group(1) == "not ok":
            name = test_match.group(2).strip()
            code = None
            error_name = None
            block_lines = []
            j = i + 1
            # Node's TAP diagnostic block is a YAML document: opens with
            # "---", closes with "..." (NOT a second "---" — verified
            # against real `node --test --test-reporter=tap` output).
            # Also bail on the next test/subtest line as a safety net, so
            # a malformed or unexpected block shape can never run away and
            # swallow the rest of the file into one giant "detail".
            in_block = False
            while j < len(lines):
                inner_stripped = lines[j].strip()
                if not in_block:
                    if inner_stripped == "---":
                        in_block = True
                        j += 1
                        continue
                    if _TAP_TEST_LINE.match(inner_stripped) or inner_stripped.startswith("#"):
                        break
                    j += 1
                    continue
                if inner_stripped == "...":
                    j += 1
                    break
                block_lines.append(lines[j])
                code_match = _TAP_CODE_LINE.match(lines[j])
                if code_match:
                    code = code_match.group(1)
                name_match = _TAP_ERROR_NAME_LINE.match(lines[j])
                if name_match:
                    error_name = name_match.group(1)
                j += 1

            failures.append({
                "name": name,
                "code": code,
                "error_name": error_name,
                "is_assertion_failure": code == "ERR_ASSERTION" or error_name == "AssertionError",
                "detail": "\n".join(block_lines).strip()[:800],
            })
            i = j
            continue

        i += 1

    return {
        "total": summary["total"],
        "passed": summary["passed"],
        "failed": summary["failed"],
        "failures": failures,
    }
