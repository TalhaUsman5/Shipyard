"""
Layer 2.5: in-process, NOT persisted live-run state, keyed by session id.

Deliberately separate from sessions.py's durable snapshot/event-log (see
that file's docstring on "current state" vs "evidence artifact") — nothing
here is ever the source of truth for what a run did. It resets on server
restart, same as the "single manually-launched dev server" scope the README
already assumes: a session's persisted output/trace never depends on this
module still holding anything.

Four things live here:
  - CANCEL_EVENTS: one threading.Event per in-flight run, checked
    cooperatively at every point a run could reasonably stop quickly
    (the walker's node boundary, the LLM streaming loop, the test-runner's
    wait loop) rather than only between whole nodes.
  - LIVE_BUFFERS: the raw text streamed so far for whichever node is
    currently generating, so the dashboard can show a node "generating" in
    real time instead of only after it's fully persisted.
  - A contextvar-based "sink" telling llm_client.call() where (if anywhere)
    to publish chunks and which cancel event to watch, set by engine.py
    around each node handler call. This keeps every agents.py role function
    and its call sites unchanged — none of them need to know this exists.
  - PENDING_REDACTIONS: what redaction.py found and scrubbed for the node
    currently running, bridged the same way as LIVE_BUFFERS — llm_client.py
    has no Session to log a durable SECRET_REDACTED event onto directly (by
    design: it's "the only place that talks to the model," not to
    sessions.py), so it drops findings here and engine.py picks them up
    right after the handler returns, in the same place it already tears
    down the sink.
"""
import contextvars
import threading
from dataclasses import dataclass
from typing import Optional


class Cancelled(Exception):
    """Raised cooperatively wherever a cancel request is noticed."""


@dataclass
class Sink:
    session_id: str
    node_id: str
    cancel_event: Optional[threading.Event]


_current_sink: contextvars.ContextVar[Optional[Sink]] = contextvars.ContextVar(
    "current_sink", default=None
)

_lock = threading.Lock()
CANCEL_EVENTS: dict = {}
LIVE_BUFFERS: dict = {}
PENDING_REDACTIONS: dict = {}


def set_sink(session_id: str, node_id: str, cancel_event: Optional[threading.Event]):
    return _current_sink.set(Sink(session_id, node_id, cancel_event))


def clear_sink(token):
    _current_sink.reset(token)


def get_sink() -> Optional[Sink]:
    return _current_sink.get()


def register_cancel_event(session_id: str) -> threading.Event:
    event = threading.Event()
    with _lock:
        CANCEL_EVENTS[session_id] = event
    return event


def unregister_cancel_event(session_id: str):
    with _lock:
        CANCEL_EVENTS.pop(session_id, None)


def get_cancel_event(session_id: str) -> Optional[threading.Event]:
    with _lock:
        return CANCEL_EVENTS.get(session_id)


def request_cancel(session_id: str) -> bool:
    """Returns True if a live run was actually signalled — False if this
    session isn't running in this process (already finished, or the server
    restarted since it started)."""
    event = get_cancel_event(session_id)
    if event is None:
        return False
    event.set()
    return True


def start_node_buffer(session_id: str, node_id: str):
    with _lock:
        LIVE_BUFFERS[(session_id, node_id)] = ""


def append_chunk(session_id: str, node_id: str, chunk: str):
    with _lock:
        key = (session_id, node_id)
        LIVE_BUFFERS[key] = LIVE_BUFFERS.get(key, "") + chunk


def get_buffer(session_id: str, node_id: str) -> str:
    with _lock:
        return LIVE_BUFFERS.get((session_id, node_id), "")


def note_redaction(session_id: str, node_id: str, findings: list) -> None:
    """Records that llm_client.call() redacted something for this node.
    Additive across multiple calls within the same node (e.g. a retry
    doesn't lose an earlier attempt's findings before engine.py drains
    them) — see pop_redactions."""
    if not findings:
        return
    with _lock:
        key = (session_id, node_id)
        PENDING_REDACTIONS.setdefault(key, []).extend(findings)


def pop_redactions(session_id: str, node_id: str) -> list:
    """Drains and returns whatever's accumulated for this node — called by
    engine.py right after a handler returns, so a durable SECRET_REDACTED
    event can be logged on the session without llm_client.py ever needing
    a Session reference of its own."""
    with _lock:
        return PENDING_REDACTIONS.pop((session_id, node_id), [])
