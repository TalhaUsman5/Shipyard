"""Auto-launches a completed project's own server so the real, live result
of a run is immediately reachable at its own URL — not just described in
the report. One live preview per project: starting a new one for a
project that already has one running stops the old process first, so
ports and child processes never accumulate across repeated runs against
the same project.

Deliberately a no-op (returns "unavailable", spawns nothing) for any
project without an npm "start" script — this keeps the whole feature
inert for the test suite's fixture-made repos, which have no
package.json at all, without a special test-only flag.
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

READY_TIMEOUT_SECONDS = 15
POLL_INTERVAL_SECONDS = 0.5

_lock = threading.Lock()
_running: dict = {}  # project_name -> {"proc": Popen, "port": int}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_script(project_root: str) -> Optional[str]:
    pkg_path = os.path.join(project_root, "package.json")
    if not os.path.isfile(pkg_path):
        return None
    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return pkg.get("scripts", {}).get("start") or None


def stop_preview(project_name: str):
    """Kills this project's currently-running preview server, if any.
    Safe to call when none is running."""
    with _lock:
        entry = _running.pop(project_name, None)
    if entry is not None:
        try:
            entry["proc"].kill()
        except OSError:
            pass


def _wait_until_reachable(port: int, deadline: float) -> bool:
    url = f"http://127.0.0.1:{port}/"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            # Any HTTP status — even an error page — still means the
            # server itself is up and answering requests.
            return True
        except Exception:
            time.sleep(POLL_INTERVAL_SECONDS)
    return False


def start_preview(
    project_root: str, project_name: str, on_update: Optional[Callable[[dict], None]] = None
) -> Optional[dict]:
    """Starts (or restarts) `npm start` for this project on a fresh
    ephemeral port. `on_update`, when given, is the SOLE channel every
    status this call will ever produce is reported through — including
    the initial "starting"/"unavailable"/"failed" — called synchronously,
    in order, before this function returns anything but the very last one
    (which is also its return value). This avoids a real race: a caller
    that instead read the return value AND separately received
    "live"/"failed" through on_update later could have the background
    thread's update arrive and then get clobbered by the caller's own
    post-call handling of the earlier "starting" return value, since the
    two are otherwise unordered with respect to each other. Readiness is
    checked on its own background thread and reported through `on_update`
    once known, never blocking this call."""
    if _start_script(project_root) is None:
        info = {"status": "unavailable", "reason": "no start script in package.json"}
        if on_update:
            on_update(info)
        return info

    stop_preview(project_name)

    port = _free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    try:
        # Invoked through cmd.exe, not bare npm, per this project's
        # established Windows convention — spawning npm directly can fail
        # with EINVAL in some runners.
        proc = subprocess.Popen(
            ["cmd.exe", "/d", "/s", "/c", "npm start"],
            cwd=project_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        info = {"status": "failed", "reason": str(e)}
        if on_update:
            on_update(info)
        return info

    with _lock:
        _running[project_name] = {"proc": proc, "port": port}

    url = f"http://127.0.0.1:{port}/"

    def _watch():
        ready = _wait_until_reachable(port, time.time() + READY_TIMEOUT_SECONDS)
        if ready:
            final = {"status": "live", "port": port, "url": url}
        elif proc.poll() is not None:
            final = {"status": "failed", "reason": "server process exited before it became reachable"}
        else:
            final = {"status": "failed", "reason": "server did not respond within timeout"}
        if on_update:
            on_update(final)

    starting_info = {"status": "starting", "port": port, "url": url}
    if on_update:
        on_update(starting_info)
    threading.Thread(target=_watch, daemon=True).start()
    return starting_info
