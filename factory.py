"""
Entry point. You run this manually:

    python factory.py

It starts a local API server on http://127.0.0.1:8000. Each feature request
is then submitted and reviewed separately, via Postman, against that
running server — the manual launch and the per-feature runs are two
different actions (see README.md).
"""
import os
from typing import Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv()

import engine  # noqa: E402  (import after load_dotenv so env vars are set)
import runtime
import sessions
from memory import load_memory, promotion_candidates

app = FastAPI(title="Software Factory", version="0.1.0")

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


class RunRequest(BaseModel):
    feature_request: str
    project_path: str


class ResumeRequest(BaseModel):
    # {budget_key: additional_uses} — an operator's explicit top-up for
    # THIS session's exhausted budgets. Optional: omit for an ordinary
    # crash-recovery resume where nothing needs granting.
    grants: Optional[Dict[str, int]] = None
    grant_reason: Optional[str] = None
    granted_by: Optional[str] = None


class ReviewRequest(BaseModel):
    node_id: Optional[str] = None
    approved: bool
    feedback: Optional[str] = None
    reviewed_by: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """A live view of the phase graph — same-origin, no CORS needed, reads
    dashboard.html fresh off disk on every request so it can be edited
    without restarting the server."""
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        return f.read()


@app.post("/run")
def run_feature(req: RunRequest):
    """Returns as soon as the session is created — the walk itself runs on
    a background thread, so this never blocks on the run finishing (or
    reaching plan_review) the way it used to."""
    session = engine.start_pipeline(req.feature_request, req.project_path)
    return session.data


@app.post("/sessions/{session_id}/resume")
def resume_session(session_id: str, req: Optional[ResumeRequest] = None):
    try:
        session = engine.start_resume(
            session_id,
            grants=req.grants if req else None,
            grant_reason=req.grant_reason if req else None,
            granted_by=req.granted_by if req else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session.data


@app.post("/sessions/{session_id}/review")
def review_session(session_id: str, req: ReviewRequest):
    """Submits a human decision at a paused human_gate node (currently only
    "plan_review") and resumes the walk in the background."""
    try:
        session = engine.submit_review(
            session_id,
            node_id=req.node_id,
            approved=req.approved,
            feedback=req.feedback,
            reviewed_by=req.reviewed_by,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session.data


@app.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: str):
    data = sessions.load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    signalled = engine.cancel_run(session_id)
    if not signalled:
        raise HTTPException(
            status_code=409,
            detail="session is not currently running in this server process (already finished, or the server restarted)",
        )
    return {"cancel_requested": True, "session": sessions.load_session(session_id)}


@app.get("/sessions/{session_id}/live/{node_id}")
def get_live_output(session_id: str, node_id: str):
    """The raw text streamed so far for a currently-generating node — see
    runtime.py. Returns "" for a node that hasn't produced any LLM output
    yet (not started, no LLM call, or the endpoint it's calling doesn't
    support streaming and fell back — see llm_client.py)."""
    data = sessions.load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    node = data.get("nodes", {}).get(node_id)
    running = bool(node) and node.get("status") == "running"
    return {"node_id": node_id, "text": runtime.get_buffer(session_id, node_id), "running": running}


@app.get("/sessions")
def get_sessions(status: Optional[str] = None, project_path: Optional[str] = None, since: Optional[float] = None):
    """`since` is a unix timestamp (matching `started_at`'s own format) —
    combine with status="failed_needs_human" to answer "every run that hit
    failed_needs_human this week" without hand-parsing JSONL."""
    return sessions.list_sessions(status=status, project_path=project_path, since=since)


@app.get("/sessions/slowest-nodes")
def get_slowest_nodes(limit: int = 20):
    """Which node consistently costs the most wall-clock time, aggregated
    across the `limit` most-recently-started sessions — see
    sessions.slowest_nodes_across_recent."""
    return {"nodes": sessions.slowest_nodes_across_recent(limit=limit)}


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    data = sessions.load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Derived, not persisted — computed fresh from whatever's actually in
    # `data` on every request (works mid-run too), so it can never drift
    # from the per-node token counts it's summing.
    return {**data, "tokens": sessions.total_tokens(data)}


@app.get("/sessions/{session_id}/report")
def get_session_report(session_id: str):
    data = sessions.load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    return sessions.build_report(data)


@app.get("/sessions/{session_id}/trace")
def get_session_trace(session_id: str):
    """The literal append-only log (sessions/<id>.events.jsonl) — the
    evidence artifact, not the rewritten-in-place session snapshot."""
    events = sessions.load_event_log(session_id)
    if events is None:
        raise HTTPException(status_code=404, detail="session or trace not found")
    return {"session_id": session_id, "events": events}


@app.get("/memory")
def get_memory():
    return {"agents_md": load_memory()}


@app.get("/memory/promotion-candidates")
def get_promotion_candidates(min_seen: int = 3):
    """Rolling patterns that have recurred at least `min_seen` times —
    worth an operator's consideration for promotion into AGENTS.md's
    Domain Rules (Permanent) section. Read-only: nothing here writes to
    Domain Rules — that stays a deliberate manual edit, same as retiring a
    Domain Rule that's since proven wrong always has been."""
    return {"candidates": promotion_candidates(min_seen=min_seen)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
