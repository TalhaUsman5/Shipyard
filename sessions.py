"""
Layer 6 (verification/observability) and durable state.

Two different artifacts, two different jobs:

  sessions/<id>.json         - current-state snapshot (status, per-node
                                output, budgets). Rewritten in full on every
                                change. Convenient for the API; NOT the
                                evidence artifact, because a full rewrite
                                gives no guarantee earlier history wasn't
                                altered along with it.

  sessions/<id>.events.jsonl - the actual audit trail. One JSON object per
                                line, opened in append mode, never rewritten
                                or seeked into. This is what to point at as
                                proof of what a run actually did: the code
                                path that writes it (record_event, below)
                                never modifies a line once written.

A session tracks graph progress, not just a flat step list: per-node
status/attempts/output ("nodes"), and the event log. This is what makes a
run resumable — /sessions/{id}/resume reloads the snapshot, and the
engine's own "skip a work node if it's already succeeded" rule does the
rest (see graph.py's module docstring).
"""
import json
import os
import threading
import time
import uuid

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

RESUMABLE_STATUSES = {"failed", "failed_needs_human", "running", "cancelled"}


class Session:
    def __init__(self, feature_request: str, project_path: str):
        self.id = uuid.uuid4().hex[:12]
        # Guards record_event: two work nodes in a graph.py PARALLEL_GROUPS
        # fork (build/verify) run on separate threads against the SAME
        # Session, and record_event's seq assignment ("len(events)" then
        # append) is a read-then-write that isn't atomic on its own — two
        # threads computing the same seq would corrupt the append-only
        # trace's ordering guarantee, which is this project's evidence
        # artifact. resume_from/wrap set this too (they bypass __init__ via
        # __new__).
        self._lock = threading.Lock()
        self.data = {
            "id": self.id,
            "feature_request": feature_request,
            "project_path": project_path,
            "started_at": time.time(),
            "status": "running",
            "current_node": None,
            "budgets": {},
            "budget_grants": {},
            "nodes": {},
            "events": [],
        }
        self.save()

    @classmethod
    def resume_from(cls, data: dict) -> "Session":
        """Wrap an existing session's persisted data to continue it after a
        crash or an exhausted budget. Does NOT reset node status — nodes
        already "succeeded" stay that way so the engine skips redoing them.
        Logs "RUN_RESUMED" — see `wrap` for a continuation that ISN'T a
        crash-recovery event (e.g. a human's plan_review decision) and
        shouldn't be logged as one."""
        session = cls.wrap(data)
        session.data.setdefault("budget_grants", {})  # sessions from before this field existed
        session.save()
        session.record_event("RUN_RESUMED")
        return session

    @classmethod
    def wrap(cls, data: dict) -> "Session":
        """Wrap already-persisted session data to continue its walk, with
        no resume-specific side effects (no "RUN_RESUMED" event, no implied
        crash recovery) — for a routine continuation like a human's
        plan_review decision, which already gets its own more specific
        event (HUMAN_REVIEW_SUBMITTED) from the caller."""
        session = cls.__new__(cls)
        session.id = data["id"]
        session._lock = threading.Lock()
        session.data = data
        session.data["status"] = "running"
        return session

    # -- node lifecycle ----------------------------------------------------

    def node_started(self, node_id: str):
        node = self.data["nodes"].setdefault(node_id, {"status": "pending", "attempts": 0})
        node["status"] = "running"
        node["attempts"] += 1
        self.data["current_node"] = node_id
        self.record_event("NODE_STARTED", node_id)

    def node_succeeded(self, node_id: str, output, tokens=None, extra=None):
        node = self.data["nodes"].setdefault(node_id, {"status": "pending", "attempts": 0})
        node["status"] = "succeeded"
        node["output"] = _to_jsonable(output)
        node["tokens"] = tokens
        node["error"] = None
        self.record_event("NODE_SUCCEEDED", node_id, {"tokens": tokens, **(extra or {})})

    def node_failed(self, node_id: str, error: str, error_kind: str = None):
        """`error_kind` names a specific llm_client.InferenceError subclass
        (e.g. "ModelBlocked", "AllowanceExhausted", "CapacityInsufficient")
        when the failure happened at the inference layer itself, rather
        than in our own code — None for every other kind of failure,
        exactly as before this parameter existed."""
        node = self.data["nodes"].setdefault(node_id, {"status": "pending", "attempts": 0})
        node["status"] = "failed"
        node["error"] = error
        node["error_kind"] = error_kind
        self.record_event("NODE_FAILED", node_id, {"error": error, "error_kind": error_kind})

    def reset_nodes(self, node_ids):
        for node_id in node_ids:
            node = self.data["nodes"].get(node_id)
            if node:
                node["status"] = "pending"

    def node_status(self, node_id: str) -> str:
        node = self.data["nodes"].get(node_id)
        return node["status"] if node else "pending"

    def node_output(self, node_id: str):
        node = self.data["nodes"].get(node_id)
        return node["output"] if node else None

    # -- budgets -------------------------------------------------------------

    def use_budget(self, key: str) -> int:
        """Increments and returns the new usage count for a budget key."""
        used = self.data["budgets"].get(key, 0) + 1
        self.data["budgets"][key] = used
        return used

    def budget_used(self, key: str) -> int:
        return self.data["budgets"].get(key, 0)

    def grant_budget(self, key: str, amount: int, reason: str = None, granted_by: str = None) -> int:
        """Adds `amount` extra uses to a budget key, for THIS session only —
        never touches graph.py's declared max_uses (that stays the policy
        for every other session) and never touches the "used" count (that
        stays an honest record of what actually happened). The effective
        ceiling a route checks against is declared max_uses + this grant
        total; see engine.py's _take_route. Returns the new grant total for
        this key. Requires amount > 0 and a non-empty reason — an
        unexplained grant is exactly the kind of silent state change this
        project has otherwise gone out of its way to avoid."""
        if amount <= 0:
            raise ValueError("grant amount must be positive")
        if not reason or not str(reason).strip():
            raise ValueError("a grant requires a non-empty reason")
        self.data.setdefault("budget_grants", {})
        total = self.data["budget_grants"].get(key, 0) + amount
        self.data["budget_grants"][key] = total
        self.record_event(
            "BUDGET_GRANTED",
            data={"budget_key": key, "amount": amount, "new_grant_total": total, "reason": reason, "granted_by": granted_by},
        )
        return total

    def budget_granted(self, key: str) -> int:
        return self.data.get("budget_grants", {}).get(key, 0)

    # -- events / lifecycle --------------------------------------------------

    def record_event(self, type_: str, node_id: str = None, data: dict = None):
        # Locked: two work nodes in a parallel_groups fork (build/verify —
        # see graph.py) can call this concurrently on the same Session.
        # Without the lock, "seq = len(events)" then append is a
        # read-then-write race — two threads could compute the same seq,
        # corrupting the append-only trace's ordering guarantee.
        with self._lock:
            event = {
                "seq": len(self.data["events"]),
                "type": type_,
                "node_id": node_id,
                "timestamp": time.time(),
                "data": data or {},
            }
            self.data["events"].append(event)
            self._append_log(event)
            self.save()

    def _append_log(self, event: dict):
        path = os.path.join(SESSIONS_DIR, f"{self.id}.events.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def finish(self, status: str):
        self.data["status"] = status
        self.data["ended_at"] = time.time()
        self.record_event("RUN_FINISHED", data={"status": status})
        self.save()

    def save(self):
        # Write-then-rename instead of writing the real path directly: a
        # background-thread run now saves on every event while the API's
        # request thread (or the dashboard's poller) can read the same file
        # at any moment (see engine.py's start_pipeline/start_resume/
        # submit_review). A direct write truncates the file before writing
        # the new content, so a concurrent read can catch it empty
        # (observed live: json.JSONDecodeError "Expecting value" reading a
        # session mid-save from a test polling loop). os.replace is atomic
        # on both POSIX and Windows, so a reader only ever sees the
        # complete old version or the complete new one, never a partial
        # write.
        path = os.path.join(SESSIONS_DIR, f"{self.id}.json")
        tmp_path = path + f".tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, default=str)

        # os.replace() itself is atomic, but on Windows it can raise
        # PermissionError if a concurrent reader (e.g. the dashboard
        # polling this same session) has the destination path open at that
        # exact instant — POSIX has no such restriction. The reader's
        # handle is always short-lived (open, read, close), so a brief
        # retry clears this without weakening the "readers never see a
        # partial write" guarantee the temp-file swap itself provides.
        last_error = None
        for _ in range(10):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as e:
                last_error = e
                time.sleep(0.02)
        raise last_error


def _to_jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def load_session(session_id: str):
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        return None
    # Retries on PermissionError the same way Session.save() retries its
    # own os.replace(): on Windows, opening a file for read can transiently
    # fail while another thread's atomic replace of that same path is
    # mid-flight — more exercisable now that a parallel_groups fork
    # (build/verify) means two threads can be saving the same session
    # around the same moment. POSIX has no such restriction; this is a
    # no-op there in practice (open() would just succeed first try).
    last_error = None
    for _ in range(10):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except PermissionError as e:
            last_error = e
            time.sleep(0.02)
    raise last_error


def total_tokens(data: dict) -> dict:
    """Sums every node's recorded `tokens` — a run's cost is tracked
    per-node everywhere else in this file, but nothing ever added them up
    until now. Works mid-run (partial totals for whatever's finished so
    far) or on a terminal session alike; never mutates `data` itself, so
    callers get a fresh number computed from whatever's actually there,
    not a persisted value that could drift from it."""
    total_input = 0
    total_output = 0
    for node in data.get("nodes", {}).values():
        tokens = node.get("tokens")
        if tokens:
            total_input += tokens.get("input", 0) or 0
            total_output += tokens.get("output", 0) or 0
    return {"input": total_input, "output": total_output, "total": total_input + total_output}


def load_event_log(session_id: str):
    """The literal append-only trace — reads sessions/<id>.events.jsonl
    line by line. Returns None if the session (or its log) doesn't exist."""
    path = os.path.join(SESSIONS_DIR, f"{session_id}.events.jsonl")
    if not os.path.exists(path):
        return None
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


_TERMINAL_NODE_EVENTS = {"NODE_SUCCEEDED", "NODE_FAILED", "NODE_CANCELLED"}


def node_durations(session_id: str) -> dict:
    """Total wall-clock seconds spent in each node across every attempt
    within this one run — pairs each NODE_STARTED with the next terminal
    event (NODE_SUCCEEDED/NODE_FAILED/NODE_CANCELLED) for the same
    node_id, summed across retries so a node reset and rerun several times
    shows its true total cost, not just its last attempt. Returns {} if
    there's no event log. A NODE_STARTED with no matching terminal event
    (the run is still on that node, or crashed mid-node) contributes
    nothing — its duration isn't known yet, not zero and not omitted from
    a future computation once it does finish."""
    events = load_event_log(session_id)
    if events is None:
        return {}
    open_starts = {}
    totals = {}
    for event in events:
        node_id = event.get("node_id")
        if node_id is None:
            continue
        if event["type"] == "NODE_STARTED":
            open_starts[node_id] = event["timestamp"]
        elif event["type"] in _TERMINAL_NODE_EVENTS and node_id in open_starts:
            totals[node_id] = totals.get(node_id, 0.0) + (event["timestamp"] - open_starts.pop(node_id))
    return totals


def slowest_nodes_across_recent(limit: int = 20) -> list:
    """Aggregates node_durations() over the `limit` most-recently-started
    sessions — answers "which node is slowest across my last N runs"
    without hand-parsing JSONL across many files. Returns
    [{"node_id", "total_seconds", "run_count"}], slowest total first."""
    rows = sorted(list_sessions(), key=lambda r: r["started_at"], reverse=True)[:limit]

    totals = {}
    run_counts = {}
    for row in rows:
        for node_id, seconds in node_durations(row["id"]).items():
            totals[node_id] = totals.get(node_id, 0.0) + seconds
            run_counts[node_id] = run_counts.get(node_id, 0) + 1

    result = [
        {"node_id": node_id, "total_seconds": round(seconds, 2), "run_count": run_counts[node_id]}
        for node_id, seconds in totals.items()
    ]
    result.sort(key=lambda r: r["total_seconds"], reverse=True)
    return result


def list_sessions(status: str = None, project_path: str = None, since: float = None):
    """`status`/`project_path` filter on an exact match; `since` (a unix
    timestamp, matching `started_at`'s own format) keeps only sessions
    started at or after it — answers "every run that hit
    failed_needs_human this week" without hand-parsing JSONL, by combining
    status="failed_needs_human" with since=<7 days ago>."""
    out = []
    for fn in sorted(os.listdir(SESSIONS_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            if status is not None and d["status"] != status:
                continue
            if project_path is not None and d["project_path"] != project_path:
                continue
            if since is not None and d["started_at"] < since:
                continue
            out.append(
                {
                    "id": d["id"],
                    "status": d["status"],
                    "project_path": d["project_path"],
                    "feature_request": d["feature_request"],
                    "started_at": d["started_at"],
                    "current_node": d.get("current_node"),
                    "awaiting_node": d.get("awaiting_node"),
                    "tokens": total_tokens(d)["total"],
                    "resumable": d["status"] in RESUMABLE_STATUSES,
                }
            )
    return out


def build_report(data: dict) -> dict:
    """A reviewable summary of one run: the artifacts each node produced and
    the final verdict, without having to read the raw event log."""
    nodes = data.get("nodes", {})

    def output_of(node_id):
        node = nodes.get(node_id)
        return node["output"] if node else None

    contract = output_of("plan")
    build_output = output_of("build")
    test_output = output_of("verify")
    test_gate = output_of("test_gate")
    review = output_of("review")
    calibrator = output_of("calibrate")

    return {
        "id": data["id"],
        "status": data["status"],
        "feature_request": data["feature_request"],
        "project_path": data["project_path"],
        "contract": contract,
        "files_written": {
            "implementation": [f["path"] for f in build_output["files"]] if build_output else [],
            "tests": [f["path"] for f in test_output["files"]] if test_output else [],
        },
        "test_result": {"passed": test_gate.get("passed")} if test_gate else None,
        "review": review,
        "calibrated_patterns": calibrator.get("patterns", []) if calibrator else [],
        "node_summary": {
            node_id: {"status": n["status"], "attempts": n["attempts"]} for node_id, n in nodes.items()
        },
        "event_count": len(data.get("events", [])),
        "tokens": total_tokens(data),
    }
