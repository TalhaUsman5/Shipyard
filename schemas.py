"""
Structured payloads passed between roles, and the graph layer that
declares how nodes connect.

Every role is instructed to return ONLY a JSON object matching one of these
shapes. Pydantic gives us a hard validation gate right at the boundary where
model output re-enters the system — if a role's JSON doesn't fit the
contract, the pipeline fails loudly here instead of corrupting a later step.
"""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class Contract(BaseModel):
    title: str
    description: str
    requirements: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    acceptance_criteria: List[str] = Field(default_factory=list)
    out_of_scope: List[str] = Field(default_factory=list)
    target_files: List[str] = Field(default_factory=list)


class FileEdit(BaseModel):
    path: str
    content: str


class BuildOutput(BaseModel):
    files: List[FileEdit] = Field(default_factory=list)
    notes: str = ""


class TestOutput(BaseModel):
    files: List[FileEdit] = Field(default_factory=list)
    notes: str = ""


class ReviewOutput(BaseModel):
    approved: bool
    issues: List[str] = Field(default_factory=list)
    summary: str = ""


class ArbiterOutput(BaseModel):
    # one of: "bug" | "spec_gap" | "noise" | "ambiguity" | "test_gap"
    #
    # test_gap: the test run failed because of a defect IN THE TEST FILE
    # itself (a broken fixture, a mock that doesn't represent the real
    # scenario it claims to), not because the implementation is wrong —
    # route to Verifier, not Builder. Same concept as review_arbiter's
    # "test_gap", just reached via a test_gate failure instead of a
    # Reviewer rejection.
    classification: str
    explanation: str
    feedback_for_builder: Optional[str] = None
    feedback_for_planner: Optional[str] = None
    feedback_for_verifier: Optional[str] = None


class ReviewArbiterOutput(BaseModel):
    # one of: "implementation_gap" | "scope_creep" | "test_gap"
    #
    # implementation_gap: the code genuinely doesn't meet the contract —
    # route back to Build the same as always.
    # scope_creep: the CONTRACT is demanding something the original feature
    # request never actually asked for (the Planner over-elaborated it) —
    # route back to Plan to narrow the contract, not Build to chase an
    # unbounded target.
    # test_gap: the REJECTION is actually about the submitted automated
    # tests themselves being broken/incomplete/inconsistent, not the
    # implementation — route back to Verify, since no amount of Build
    # retries can fix a defect in a file Build never touches.
    classification: str
    explanation: str
    feedback_for_planner: Optional[str] = None
    feedback_for_verifier: Optional[str] = None


class CalibratorOutput(BaseModel):
    patterns: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph layer — the phase graph is data (see graph.py), not control flow.
# NodeSpec/ClassifierRoute describe the topology; NodeState/GraphEvent
# describe a run's progress through it. Node handlers (engine.py) are the
# only place that still has to be code.
# ---------------------------------------------------------------------------

NodeKind = Literal["work", "gate", "classifier", "human_gate"]
NodeStatus = Literal["pending", "running", "succeeded", "failed"]

# Sentinel route target meaning "stop the run, a human needs to look at this."
FAIL_SENTINEL = "__failed_needs_human__"


class ClassifierRoute(BaseModel):
    """One outcome a classifier node (e.g. the Arbiter) can route to.

    Also used as a gate node's `on_fail`, so a deterministic gate failure
    (e.g. Reviewer rejection) can carry the same bounded-retry machinery a
    classifier's routes do, not just an unconditional jump.

    `reset` names which already-succeeded work nodes must redo their work
    because this route invalidates their prior output (e.g. a "bug" route
    resets "build" but not "verify" — the tests don't need rewriting just
    because the implementation was wrong). `budget_key`/`max_uses` bound how
    many times this specific route may be taken across a run; a route with
    no budget_key is unbounded (only the engine's global iteration cap
    applies) — used for terminal routes like target=FAIL_SENTINEL.
    """

    target: str
    reset: List[str] = Field(default_factory=list)
    budget_key: Optional[str] = None
    max_uses: Optional[int] = None


class NodeSpec(BaseModel):
    """One node in the phase graph. Pure data — no callables — so the graph
    itself can be inspected, validated, and serialized independently of the
    Python functions (engine.py's NODE_HANDLERS) that execute each node."""

    id: str
    kind: NodeKind
    # work node: always taken. gate node: taken when the gate passes.
    next: Optional[str] = None
    # gate node only: taken when the gate fails. A ClassifierRoute (not a
    # bare node id) so a gate failure can carry a bounded retry budget too.
    on_fail: Optional[ClassifierRoute] = None
    # classifier node only: classification label -> route.
    routes: Dict[str, ClassifierRoute] = Field(default_factory=dict)
    # if this node raises and fatal_on_error is False, the run continues
    # past it (e.g. Calibrator: a failed pattern extraction shouldn't sink
    # an otherwise-successful run).
    fatal_on_error: bool = True


class NodeState(BaseModel):
    """Persisted progress for one node within a session."""

    status: NodeStatus = "pending"
    attempts: int = 0
    output: Optional[Any] = None
    tokens: Optional[Dict[str, int]] = None
    error: Optional[str] = None


class GraphEvent(BaseModel):
    """One entry in a session's append-only event log."""

    type: str
    node_id: Optional[str] = None
    timestamp: float
    data: Dict[str, Any] = Field(default_factory=dict)
