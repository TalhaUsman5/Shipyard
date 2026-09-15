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
import sessions
from memory import load_memory

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
    session = engine.run_pipeline(req.feature_request, req.project_path)
    return session.data


@app.post("/sessions/{session_id}/resume")
def resume_session(session_id: str, req: Optional[ResumeRequest] = None):
    try:
        session = engine.resume_pipeline(
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


@app.get("/sessions")
def get_sessions():
    return sessions.list_sessions()


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    data = sessions.load_session(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    return data


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
