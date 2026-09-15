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
import time
import uuid

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

RESUMABLE_STATUSES = {"failed", "failed_needs_human", "running"}


class Session:
    def __init__(self, feature_request: str, project_path: str):
        self.id = uuid.uuid4().hex[:12]
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
        """Wrap an existing session's persisted data to continue it. Does
        NOT reset node status — nodes already "succeeded" stay that way so
        the engine skips redoing them."""
        session = cls.__new__(cls)
        session.id = data["id"]
        session.data = data
        session.data["status"] = "running"
        session.data.setdefault("budget_grants", {})  # sessions from before this field existed
        session.save()
        session.record_event("RUN_RESUMED")
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

    def node_failed(self, node_id: str, error: str):
        node = self.data["nodes"].setdefault(node_id, {"status": "pending", "attempts": 0})
        node["status"] = "failed"
        node["error"] = error
        self.record_event("NODE_FAILED", node_id, {"error": error})

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
        path = os.path.join(SESSIONS_DIR, f"{self.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, default=str)


def _to_jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def load_session(session_id: str):
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def list_sessions():
    out = []
    for fn in sorted(os.listdir(SESSIONS_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            out.append(
                {
                    "id": d["id"],
                    "status": d["status"],
                    "project_path": d["project_path"],
                    "feature_request": d["feature_request"],
                    "started_at": d["started_at"],
                    "current_node": d.get("current_node"),
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
        "calibrated_pattern": calibrator.get("pattern") if calibrator else None,
        "node_summary": {
            node_id: {"status": n["status"], "attempts": n["attempts"]} for node_id, n in nodes.items()
        },
        "event_count": len(data.get("events", [])),
    }
