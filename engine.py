"""
Layer 2 (orchestration): the generic graph walker.

This file has no idea that "plan" means the Planner — it just walks
graph.PHASE_GRAPH (data) and calls whatever NODE_HANDLERS[node_id] says to
run. Node-specific behavior (which agent to call, which files to write)
lives in the handler functions below; the walking logic itself — gates,
bounded retries via budgets, cache-skip of already-succeeded work nodes —
is generic and doesn't change if the graph's shape does.

Retrying and resuming are the same mechanism: a retry is a classifier route
that resets some work nodes to "pending" and re-walks from its target;
resuming an interrupted run is re-walking from ENTRY_NODE with nothing
reset — already-"succeeded" work nodes just skip themselves. See graph.py's
module docstring for the full topology.
"""
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import agents
import execution
import llm_client
import runtime
from context import project_snapshot, read_package_json
from graph import ENTRY_NODE, MAX_TOTAL_ITERATIONS, NODE_OUTPUT_MODELS, PARALLEL_GROUPS, PHASE_GRAPH
from memory import append_pattern, load_memory
from schemas import FAIL_SENTINEL, ReviewOutput
from sessions import RESUMABLE_STATUSES, Session, load_session

WORKSPACE_DIR = os.path.join(os.path.dirname(__file__), "workspace")


class _Abort(Exception):
    pass


def _resolve_project_path(project_path: str) -> str:
    """Resolve project_path as a name relative to workspace/, creating the
    directory if this is a new repo. Raises ValueError if project_path is
    absolute or would escape workspace/."""
    if os.path.isabs(project_path):
        raise ValueError("project_path must be a name relative to workspace/, not an absolute path")

    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    target = os.path.realpath(os.path.join(WORKSPACE_DIR, project_path))
    workspace_real = os.path.realpath(WORKSPACE_DIR)
    if target != workspace_real and not target.startswith(workspace_real + os.sep):
        raise ValueError(f"project_path escapes workspace/: {project_path!r}")

    is_new = not os.path.isdir(target)
    os.makedirs(target, exist_ok=True)
    if is_new:
        _scaffold_new_repo(target, project_path)
    return target


def _scaffold_new_repo(target: str, project_path: str) -> None:
    """A brand-new workspace repo starts with nothing to run `npm test`
    against, so test_gate fails before the Builder ever gets a chance. Give
    it the minimal skeleton the Verifier already assumes (node's built-in
    test runner via `npm test`)."""
    package_json = {
        "name": os.path.basename(project_path.rstrip("/\\")) or "factory-project",
        "version": "0.0.0",
        "private": True,
        "scripts": {"test": "node --test"},
    }
    with open(os.path.join(target, "package.json"), "w", encoding="utf-8") as f:
        json.dump(package_json, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Run context — the typed-I/O bus. Every node's output is stored under its
# node id; downstream handlers pull prior outputs by id via ctx.get(...).
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    feature_request: str
    project_path: str
    memory: str
    snapshot: list
    snapshot_truncated: bool
    pkg: Optional[dict]
    session: Session
    outputs: dict = field(default_factory=dict)
    scratch: dict = field(default_factory=dict)
    # Set by _build_context to this session's runtime.py cancel Event —
    # checked at the top of the walker loop, inside llm_client's streaming
    # loop, and inside execution.run_tests's wait loop. None only in
    # contexts that never go through the background-thread entry points
    # below (there are none left in normal operation, but keep it optional
    # so a handler never has to assume it's set).
    cancel_event: Optional[threading.Event] = None

    def get(self, node_id: str):
        return self.outputs.get(node_id)

    def set(self, node_id: str, value):
        self.outputs[node_id] = value


@dataclass
class WorkResult:
    output: Any
    tokens: Optional[dict] = None
    extra: Optional[dict] = None


@dataclass
class GateResult:
    passed: bool
    data: Any
    # Most gates (test_gate/review_gate) are cheap and deterministic — no
    # LLM call, so nothing to track. "scope_gate" is the exception: its
    # handler makes a real LLM judgment call, so its cost needs the same
    # observability every work/classifier node already gets.
    tokens: Optional[dict] = None


@dataclass
class ClassifierResult:
    label: str
    output: Any
    tokens: Optional[dict] = None


def _rehydrate(node_id: str, raw):
    if raw is None:
        return None
    model = NODE_OUTPUT_MODELS.get(node_id)
    return model(**raw) if model else raw


# ---------------------------------------------------------------------------
# Node handlers — the only place that still knows "plan means the Planner".
# ---------------------------------------------------------------------------


def _handle_plan(ctx: RunContext) -> WorkResult:
    feedback = ctx.scratch.pop("planner_refinement_feedback", None)
    contract, tokens = agents.run_planner(
        ctx.feature_request,
        ctx.memory,
        ctx.snapshot,
        ctx.pkg,
        refinement_feedback=feedback,
        snapshot_truncated=ctx.snapshot_truncated,
    )
    return WorkResult(output=contract, tokens=tokens)


def _is_test_path(rel_path: str) -> bool:
    """True if rel_path is under test/ — the Verifier's own convention (see
    VERIFIER_SYSTEM) — so the Builder never gets to see test code, even
    when we hand it the current on-disk file contents below."""
    return rel_path.replace("\\", "/").split("/")[0] == "test"


def _handle_build(ctx: RunContext) -> WorkResult:
    contract = ctx.get("plan")
    assert contract is not None  # graph invariant: "build" only runs after "plan" has succeeded

    # The real, current state of the project — not just what the Builder's
    # own last attempt happened to return. apply_files only writes what a
    # call includes, so a narrower "last delta" view can't see files an
    # EARLIER retry wrote that this one doesn't happen to touch again.
    # priority_paths=contract.target_files: if the project is big enough to
    # hit project_snapshot's cap, the files the contract actually names
    # must survive truncation ahead of whatever directory-walk order
    # happens to turn up first.
    all_files, _listing_truncated = project_snapshot(ctx.project_path, priority_paths=contract.target_files)
    listing = [p for p in all_files if not _is_test_path(p)]
    current_files, omitted_current_files = execution.read_text_files(ctx.project_path, listing)

    # Feedback comes straight from arbiter/review's own persisted output —
    # both already durable (survive a crash/resume) and the single source
    # of truth, so there's no need to also duplicate it into an
    # ever-growing contract.constraints list that every role's prompt has
    # to carry forever after.
    arbiter_raw = ctx.session.data["nodes"].get("arbiter", {}).get("output")
    arbiter_feedback = None
    if arbiter_raw and arbiter_raw.get("classification") == "bug":
        arbiter_feedback = arbiter_raw.get("feedback_for_builder")

    review_raw = ctx.session.data["nodes"].get("review", {}).get("output")
    previous_review = ReviewOutput(**review_raw) if review_raw else None

    build_output, tokens = agents.run_builder(
        contract,
        ctx.snapshot,
        ctx.pkg,
        ctx.memory,
        current_files=current_files,
        arbiter_feedback=arbiter_feedback,
        previous_review=previous_review,
        snapshot_truncated=ctx.snapshot_truncated,
        omitted_current_files=omitted_current_files,
    )

    # Structural enforcement of BUILDER_SYSTEM's own rule ("you do not see
    # any test code") — nothing before this stopped the Builder from
    # choosing to WRITE one anyway. Observed live: a real run had the
    # Builder return its own test/rate-limiter.test.js alongside its
    # implementation. Verify's directory-replace happened to overwrite it
    # by filename coincidence that time, but that's luck, not a guarantee
    # — a Builder-authored test that survives means the Builder graded its
    # own work, defeating independent verification entirely. Drop it here,
    # structurally, the same way execution._safe_path structurally blocks
    # a path escape rather than trusting a role not to attempt one — and
    # mutate build_output itself (not just what gets written to disk) so
    # the persisted record, and what Arbiter/Reviewer see later via
    # build_output.model_dump_json(), never claims a file exists that
    # isn't really there.
    dropped_test_paths = [f.path for f in build_output.files if _is_test_path(f.path)]
    if dropped_test_paths:
        build_output.files = [f for f in build_output.files if not _is_test_path(f.path)]

    written = execution.apply_files(ctx.project_path, build_output.files)
    extra = {"files_written": written}
    if dropped_test_paths:
        extra["dropped_test_paths"] = dropped_test_paths
    return WorkResult(output=build_output, tokens=tokens, extra=extra)


def _handle_verify(ctx: RunContext) -> WorkResult:
    contract = ctx.get("plan")
    assert contract is not None  # graph invariant: "verify" only runs after "plan" has succeeded
    # Only set by review_arbiter's test_gap route — a Reviewer finding that
    # the submitted tests themselves are broken/incomplete, threaded here
    # the same way arbiter/spec_gap feeds the Planner's next attempt.
    feedback = ctx.scratch.pop("verifier_feedback", None)

    # Same reason _handle_build reads real on-disk state before every
    # retry: without this, the Verifier has no idea a prior attempt's test
    # file(s) already exist, so a retry tends to invent a NEW filename
    # instead of fixing the old one — leaving the broken version behind
    # for `node --test` (which discovers every test-shaped file on disk)
    # to keep running forever alongside the new one.
    all_files, _listing_truncated = project_snapshot(ctx.project_path)
    listing = [p for p in all_files if _is_test_path(p)]
    current_files, omitted_current_files = execution.read_text_files(ctx.project_path, listing)

    test_output, tokens = agents.run_verifier(
        contract,
        ctx.snapshot,
        ctx.pkg,
        ctx.memory,
        current_files=current_files,
        feedback=feedback,
        snapshot_truncated=ctx.snapshot_truncated,
        omitted_current_files=omitted_current_files,
    )
    # test/ is the Verifier's own directory to fully own, not a patch onto
    # someone else's files — its response IS the complete new state of
    # test/, so anything it didn't include this time gets deleted, not
    # left behind as an abandoned prior generation.
    written = execution.apply_files_replacing_directory(ctx.project_path, "test", test_output.files)
    return WorkResult(output=test_output, tokens=tokens, extra={"files_written": written})


def _handle_test_gate(ctx: RunContext) -> GateResult:
    test_results = execution.run_tests(ctx.project_path, cancel_event=ctx.cancel_event)
    return GateResult(passed=test_results["passed"], data=test_results)


def _handle_arbiter(ctx: RunContext) -> ClassifierResult:
    contract = ctx.get("plan")
    build_output = ctx.get("build")
    test_output = ctx.get("verify")
    test_results = ctx.get("test_gate")
    # graph invariant: "arbiter" only runs on test_gate's on_fail, after plan/build/verify/test_gate succeeded
    assert contract is not None and build_output is not None and test_output is not None and test_results is not None
    arb, tokens = agents.run_arbiter(contract, build_output, test_output, test_results, ctx.memory)

    # spec_gap/ambiguity/test_gap all need a side channel: their target
    # node gets fully redone next, so feedback has to be handed off
    # somewhere that next run will read it (ctx.scratch). "bug" doesn't
    # need this — "plan" isn't being redone, and _handle_build reads the
    # Arbiter's own persisted output directly.
    if arb.classification in ("spec_gap", "ambiguity"):
        ctx.scratch["planner_refinement_feedback"] = arb.feedback_for_planner
    elif arb.classification == "test_gap":
        ctx.scratch["verifier_feedback"] = arb.feedback_for_verifier

    return ClassifierResult(label=arb.classification, output=arb, tokens=tokens)


def _handle_review(ctx: RunContext) -> WorkResult:
    contract = ctx.get("plan")
    build_output = ctx.get("build")
    test_output = ctx.get("verify")
    test_results = ctx.get("test_gate")
    # graph invariant: "review" only runs after plan/build/verify/test_gate succeeded
    assert contract is not None
    assert build_output is not None
    assert test_output is not None
    assert test_results is not None

    # Same idea as _handle_build's previous_build: review_gate's on_fail
    # resets "review"'s status but its last persisted output survives, so
    # a re-review can check what it already found instead of re-auditing
    # from scratch and risking a false re-flag of something now fixed.
    previous_raw = ctx.session.data["nodes"].get("review", {}).get("output")
    previous_review = ReviewOutput(**previous_raw) if previous_raw else None

    review, tokens = agents.run_reviewer(
        contract, build_output, test_output, test_results, ctx.memory, previous_review=previous_review
    )
    return WorkResult(output=review, tokens=tokens)


def _handle_review_gate(ctx: RunContext) -> GateResult:
    review = ctx.get("review")
    assert review is not None  # graph invariant: "review_gate" only runs after "review" has succeeded
    # No contract mutation needed on rejection: review's own persisted
    # output already carries the issues/summary, and _handle_build reads
    # it directly as "previous_review" when it reruns.
    return GateResult(passed=review.approved, data={"issues": review.issues, "summary": review.summary})


def _handle_review_arbiter(ctx: RunContext) -> ClassifierResult:
    contract = ctx.get("plan")
    review = ctx.get("review")
    # graph invariant: "review_arbiter" only runs on review_gate's on_fail, after plan/review succeeded
    assert contract is not None and review is not None
    result, tokens = agents.run_review_arbiter(ctx.feature_request, contract, review, ctx.memory)

    # Mirrors _handle_arbiter's spec_gap/ambiguity branch: the target node
    # gets fully redone next, so feedback has to be handed off via
    # ctx.scratch for that next run to read. "implementation_gap" needs no
    # side channel — _handle_build reads review's own persisted output
    # directly as "previous_review".
    if result.classification == "scope_creep":
        ctx.scratch["planner_refinement_feedback"] = result.feedback_for_planner
    elif result.classification == "test_gap":
        ctx.scratch["verifier_feedback"] = result.feedback_for_verifier

    return ClassifierResult(label=result.classification, output=result, tokens=tokens)


def _handle_calibrate(ctx: RunContext) -> WorkResult:
    calib, tokens = agents.run_calibrator(ctx.session.data, ctx.memory)
    source = ctx.session.data.get("project_path")
    for pattern in calib.patterns:
        append_pattern(pattern, source=source)
    return WorkResult(output=calib, tokens=tokens)


NODE_HANDLERS = {
    "plan": _handle_plan,
    "build": _handle_build,
    "verify": _handle_verify,
    "test_gate": _handle_test_gate,
    "arbiter": _handle_arbiter,
    "review": _handle_review,
    "review_gate": _handle_review_gate,
    "review_arbiter": _handle_review_arbiter,
    "calibrate": _handle_calibrate,
}


# ---------------------------------------------------------------------------
# The walker
# ---------------------------------------------------------------------------


def _drain_redactions(session: Session, node_id: str) -> None:
    """Logs a durable SECRET_REDACTED event for whatever llm_client.call()
    scrubbed for this node — see runtime.py's PENDING_REDACTIONS. Called
    from the same `finally` that tears down the sink, so this fires
    regardless of whether the node ultimately succeeded, failed, or was
    cancelled: a credential was still sent to (then scrubbed before
    leaving toward) the provider either way, and that's worth a permanent
    record independent of the node's own outcome."""
    findings = runtime.pop_redactions(session.id, node_id)
    if findings:
        session.record_event(
            "SECRET_REDACTED",
            node_id,
            {"patterns": [{"pattern": f.pattern, "count": f.count} for f in findings]},
        )


def _node_failed(session: Session, node_id: str, error: Exception) -> None:
    """Records a node failure, classifying it as a named inference-layer
    condition (see llm_client.InferenceError and its subclasses) when the
    exception actually is one — so a trace reader can tell "the provider
    wouldn't serve this" from "the model produced something wrong" without
    reading the raw exception text. Any other exception (a real bug, a
    malformed response our own code choked on) is recorded exactly as
    before: no error_kind, just the message."""
    error_kind = type(error).__name__ if isinstance(error, llm_client.InferenceError) else None
    session.node_failed(node_id, str(error), error_kind=error_kind)


def _run_work_or_classifier(ctx: RunContext, session: Session, node_id: str, spec):
    handler = NODE_HANDLERS[node_id]
    session.node_started(node_id)
    sink_token = runtime.set_sink(session.id, node_id, ctx.cancel_event)
    try:
        result = handler(ctx)
    except runtime.Cancelled as e:
        session.record_event("NODE_CANCELLED", node_id, {"error": str(e)})
        session.finish("cancelled")
        raise _Abort()
    except Exception as e:  # noqa: BLE001 - deliberately broad: any node failure must be caught and logged
        _node_failed(session, node_id, e)
        if spec.fatal_on_error:
            session.finish("failed")
            raise _Abort()
        return None
    finally:
        runtime.clear_sink(sink_token)
        _drain_redactions(session, node_id)

    ctx.set(node_id, result.output)
    session.node_succeeded(node_id, result.output, result.tokens, getattr(result, "extra", None))
    return result


def _run_gate(ctx: RunContext, session: Session, node_id: str) -> GateResult:
    handler = NODE_HANDLERS[node_id]
    session.node_started(node_id)
    sink_token = runtime.set_sink(session.id, node_id, ctx.cancel_event)
    try:
        result = handler(ctx)
    except runtime.Cancelled as e:
        session.record_event("NODE_CANCELLED", node_id, {"error": str(e)})
        session.finish("cancelled")
        raise _Abort()
    except Exception as e:  # noqa: BLE001
        _node_failed(session, node_id, e)
        session.finish("failed")
        raise _Abort()
    finally:
        runtime.clear_sink(sink_token)
        _drain_redactions(session, node_id)

    ctx.set(node_id, result.data)
    session.node_succeeded(node_id, result.data, result.tokens)
    return result


def _run_parallel_group(ctx: RunContext, session: Session, group: frozenset) -> str:
    """Runs every not-yet-succeeded member of a graph.PARALLEL_GROUPS group
    concurrently, each on its own thread, and returns the group's shared
    `next` once all of them have finished. A route that reset only ONE
    member (e.g. arbiter's "bug" resets just "build") still only reruns
    that one — the others are already "succeeded" and get skipped/
    rehydrated exactly like the single-node work path does, just per
    member instead of for one node.

    Thread-safety: each member writes only its OWN ctx.outputs key and its
    OWN session.data["nodes"][...] entry (disjoint dict keys, safe under
    the GIL); Session.record_event has its own lock for the one genuinely
    shared, order-sensitive piece of state (the event log's seq counter).
    """
    members = sorted(group)
    pending = [nid for nid in members if session.node_status(nid) != "succeeded"]
    for nid in members:
        if nid not in pending and nid not in ctx.outputs:
            ctx.set(nid, _rehydrate(nid, session.node_output(nid)))

    shared_next = PHASE_GRAPH[members[0]].next
    if not pending:
        return shared_next

    errors = {}

    def _run_one(nid):
        try:
            _run_work_or_classifier(ctx, session, nid, PHASE_GRAPH[nid])
        except _Abort as e:
            errors[nid] = e

    threads = [threading.Thread(target=_run_one, args=(nid,)) for nid in pending]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        # Whichever member(s) failed already called session.finish(...) and
        # logged its own NODE_FAILED/NODE_CANCELLED — re-raising once here
        # stops the outer walk exactly like a single failing node does.
        raise _Abort()

    return shared_next


def _take_route(session: Session, ctx: RunContext, route) -> Optional[str]:
    """Applies a ClassifierRoute (used both for a classifier's own routes
    and a gate's on_fail): enforces its retry budget, resets whichever work
    nodes it invalidates, and returns the node id to continue at. Returns
    None if the run was terminated here (no route, or budget exhausted).

    Every outcome — a route taken, or a budget exhausted — is logged as its
    own event, so a retry is explicit in the trace rather than only
    inferable from repeated NODE_STARTED lines for the same node."""
    if route is None:
        session.record_event("ROUTE_MISSING")
        session.finish("failed_needs_human")
        return None
    if route.budget_key:
        # The effective ceiling is the graph's declared max_uses PLUS
        # whatever this specific session has been granted — see
        # Session.grant_budget. Grants are per-session, additive, and
        # logged; they never touch graph.py (which stays the policy for
        # every other session) or the "used" count (which stays an honest
        # record of what actually happened).
        granted = session.budget_granted(route.budget_key)
        effective_max = (route.max_uses or 0) + granted
        used_so_far = session.budget_used(route.budget_key)
        if used_so_far >= effective_max:
            session.record_event(
                "ROUTE_BUDGET_EXHAUSTED",
                data={
                    "budget_key": route.budget_key,
                    "max_uses": route.max_uses,
                    "granted": granted,
                    "effective_max": effective_max,
                    "target": route.target,
                },
            )
            session.finish("failed_needs_human")
            return None
        used = session.use_budget(route.budget_key)
    else:
        used = None
    session.reset_nodes(route.reset)
    for reset_id in route.reset:
        ctx.outputs.pop(reset_id, None)
    session.record_event(
        "ROUTE_TAKEN",
        data={
            "target": route.target,
            "reset": route.reset,
            "budget_key": route.budget_key,
            "budget_used": used,
            "budget_max": route.max_uses,
        },
    )
    return route.target


def _walk(session: Session, ctx: RunContext, start_node: str):
    node_id = start_node
    iterations = 0

    while True:
        if node_id is None:
            session.finish("completed")
            return
        if node_id == FAIL_SENTINEL:
            session.finish("failed_needs_human")
            return
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            session.record_event("NODE_CANCELLED", node_id, {"note": "cancelled before node started"})
            session.finish("cancelled")
            return

        iterations += 1
        if iterations > MAX_TOTAL_ITERATIONS:
            session.finish("failed_needs_human")
            return

        spec = PHASE_GRAPH[node_id]

        if spec.kind == "work":
            group = PARALLEL_GROUPS.get(node_id)
            if group:
                node_id = _run_parallel_group(ctx, session, group)
                continue
            if session.node_status(node_id) == "succeeded":
                if node_id not in ctx.outputs:
                    ctx.set(node_id, _rehydrate(node_id, session.node_output(node_id)))
                node_id = spec.next
                continue
            _run_work_or_classifier(ctx, session, node_id, spec)
            node_id = spec.next
            continue

        if spec.kind == "gate":
            result = _run_gate(ctx, session, node_id)
            if result.passed:
                node_id = spec.next
                continue
            target = _take_route(session, ctx, spec.on_fail)
            if target is None:
                return
            node_id = target
            continue

        if spec.kind == "human_gate":
            # Unlike an ordinary gate, an already-*approved* decision IS
            # trusted across a resume — a human signed off on this exact
            # "plan" output already, and asking them to re-approve the
            # identical contract after an unrelated crash mid-build would
            # be a UX regression, not more truthful. A REJECTED decision
            # never lingers here to be (mis-)trusted this way: rejecting
            # immediately routes back to "plan" in the same walk (see
            # below), and "plan" reruns before this node is ever reached
            # again, so the only "succeeded" state this branch can ever
            # see on a later resume is a genuine approval.
            if session.node_status(node_id) == "succeeded":
                cached = session.node_output(node_id) or {}
                if cached.get("approved"):
                    ctx.set(node_id, cached)
                    node_id = spec.next
                    continue

            # Only skip node_started() on the round-trip back into the SAME
            # pause (status still "running" from when this node first
            # paused the walk) so "attempts" counts review rounds, not
            # every walk() call that happens to reach it.
            if session.node_status(node_id) != "running":
                session.node_started(node_id)
            pending = session.data.get("pending_reviews", {}).pop(node_id, None)
            if pending is None:
                session.data["status"] = "awaiting_review"
                session.data["awaiting_node"] = node_id
                session.data["current_node"] = node_id
                session.record_event("AWAITING_HUMAN_REVIEW", node_id)
                return
            decision = {
                "approved": bool(pending.get("approved")),
                "feedback": pending.get("feedback"),
                "reviewed_by": pending.get("reviewed_by"),
            }
            ctx.set(node_id, decision)
            session.node_succeeded(node_id, decision)
            if decision["approved"]:
                node_id = spec.next
                continue
            # Same feedback channel spec_gap/ambiguity/scope_creep already
            # use to hand the next "plan" run something to act on.
            ctx.scratch["planner_refinement_feedback"] = decision["feedback"]
            target = _take_route(session, ctx, spec.on_fail)
            if target is None:
                return
            node_id = target
            continue

        # classifier
        result = _run_work_or_classifier(ctx, session, node_id, spec)
        # every classifier node in PHASE_GRAPH has fatal_on_error=True (the
        # default), so a failure here raises _Abort instead of returning None
        assert result is not None
        route = spec.routes.get(result.label)
        target = _take_route(session, ctx, route)
        if target is None:
            return
        node_id = target


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _snapshot_graph_event(session: Session):
    """Records the topology governing this run (or this resumed segment of
    it) as the first line of the append-only trace — so the log is
    self-contained proof of what gates/routes/budgets were in effect, even
    if graph.py is edited later. Emitted at both /run and /resume: if the
    graph hasn't changed between a crash and its resume, this is just a
    harmless duplicate; if it has, that's exactly the discrepancy the log
    should surface."""
    session.record_event(
        "GRAPH_SNAPSHOT",
        data={
            "entry_node": ENTRY_NODE,
            "max_total_iterations": MAX_TOTAL_ITERATIONS,
            "nodes": {node_id: spec.model_dump() for node_id, spec in PHASE_GRAPH.items()},
        },
    )


def _build_context(session: Session) -> Optional[RunContext]:
    """Builds the RunContext a walk needs and registers this session's
    cancel Event with runtime.py. Returns None (after recording an ERROR
    event and finishing the session as "failed") if project_path can't be
    resolved — the one init step that can fail before a walk ever starts."""
    try:
        resolved_path = _resolve_project_path(session.data["project_path"])
    except ValueError as e:
        session.record_event("ERROR", data={"stage": "init", "error": str(e)})
        session.finish("failed")
        return None

    snapshot, snapshot_truncated = project_snapshot(resolved_path)
    ctx = RunContext(
        feature_request=session.data["feature_request"],
        project_path=resolved_path,
        memory=load_memory(),
        snapshot=snapshot,
        snapshot_truncated=snapshot_truncated,
        pkg=read_package_json(resolved_path),
        session=session,
        cancel_event=runtime.register_cancel_event(session.id),
    )
    for node_id, node in session.data["nodes"].items():
        if node["status"] == "succeeded" and node_id in NODE_OUTPUT_MODELS:
            ctx.set(node_id, _rehydrate(node_id, node["output"]))
    return ctx


def _execute_walk(session: Session):
    """The actual (potentially long-running) graph walk. Called directly
    (blocking) by run_pipeline/resume_pipeline/apply_review, or scheduled
    onto a background thread by their start_*/submit_review counterparts
    below — same walk either way, just a different calling convention for
    a synchronous caller (e.g. the test suite) versus factory.py's request
    handlers, which must never block on it."""
    try:
        ctx = _build_context(session)
        if ctx is None:
            return
        try:
            _walk(session, ctx, ENTRY_NODE)
        except _Abort:
            pass
    finally:
        runtime.unregister_cancel_event(session.id)
        # Self-cleanup so _THREADS never grows unbounded over a long-lived
        # server process. Only remove OUR OWN entry: a same-session resume
        # started while we were still finishing up (shouldn't happen in
        # practice — see the "one active writer" note elsewhere — but cheap
        # to guard) would already have overwritten this with its own
        # thread, which must not be popped out from under it.
        if _THREADS.get(session.id) is threading.current_thread():
            _THREADS.pop(session.id, None)



# Tracks the background Thread each session is currently running on, so a
# caller that genuinely needs to know the walk has FULLY finished (not just
# that its status field flipped to a terminal value, which a poll loop can
# observe a moment before the thread's own last few statements actually
# run) can join it deterministically. Production code never needs this —
# it's here for the test suite, which monkeypatches WORKSPACE_DIR/
# SESSIONS_DIR per-test: a background thread from start_pipeline/
# start_resume/submit_review that outlives its test function can straggle
# past that test's fixture teardown and write into whatever real path the
# monkeypatch reverted to (observed live: a stray session file landed in
# the actual sessions/ directory this way). See join_background below.
_THREADS: dict = {}


def _spawn(session: Session):
    thread = threading.Thread(target=_execute_walk, args=(session,), daemon=True)
    _THREADS[session.id] = thread
    thread.start()


def join_background(session_id: str, timeout: float = 5.0) -> bool:
    """Test-only helper: blocks until the background thread started for
    this session_id has fully exited. Returns False on timeout (the thread
    is still running) so a caller can fail loudly instead of silently
    racing on. A session never resumed in the background (or already
    joined) has nothing to wait for and returns True immediately."""
    thread = _THREADS.pop(session_id, None)
    if thread is None:
        return True
    thread.join(timeout=timeout)
    return not thread.is_alive()


def _prepare_resume(
    session_id: str,
    grants: Optional[dict] = None,
    grant_reason: Optional[str] = None,
    granted_by: Optional[str] = None,
) -> Optional[Session]:
    """Continue a session that ended in a resumable status. Already-
    succeeded nodes are trusted and skipped — see graph.py's module
    docstring for why that's also how retries work.

    `grants` is an optional {budget_key: additional_uses} map — an
    operator's explicit, one-time top-up for THIS session's budgets,
    applied before the walk resumes. This is the intended way to get past
    an exhausted budget: no graph.py edit, no restart, no engineer needed
    in the loop. Every grant requires `grant_reason` and is logged as its
    own BUDGET_GRANTED event (see Session.grant_budget) — it adds to the
    ceiling a route checks against, it never touches graph.py's declared
    policy or the "used" count, so it never looks like something that
    silently didn't happen."""
    data = load_session(session_id)
    if data is None:
        return None
    if data["status"] not in RESUMABLE_STATUSES:
        raise ValueError(
            f"session {session_id!r} is {data['status']!r} — only "
            f"{sorted(RESUMABLE_STATUSES)} runs can be resumed"
        )

    session = Session.resume_from(data)
    if grants:
        for key, amount in grants.items():
            session.grant_budget(key, amount, reason=grant_reason, granted_by=granted_by)
    _snapshot_graph_event(session)
    return session


def _prepare_review(
    session_id: str,
    node_id: Optional[str] = None,
    approved: bool = False,
    feedback: Optional[str] = None,
    reviewed_by: Optional[str] = None,
) -> Optional[Session]:
    """Records a human's decision at a paused "human_gate" node, ready to
    resume the walk — the counterpart to _prepare_resume, but for a run
    paused waiting on a person rather than one that crashed or exhausted a
    budget. Requires non-empty `feedback` on a rejection, same "no silent/
    unexplained state change" rule Session.grant_budget already enforces
    for budget top-ups."""
    data = load_session(session_id)
    if data is None:
        return None
    if data.get("status") != "awaiting_review":
        raise ValueError(f"session {session_id!r} is {data.get('status')!r}, not awaiting review")
    awaiting = data.get("awaiting_node")
    if node_id and node_id != awaiting:
        raise ValueError(f"session {session_id!r} is awaiting review at {awaiting!r}, not {node_id!r}")
    if not approved and not (feedback and str(feedback).strip()):
        raise ValueError("feedback is required when requesting changes (approved=False)")

    # Session.wrap, not resume_from: this isn't a crash/budget recovery
    # (no "RUN_RESUMED" event, no extra GRAPH_SNAPSHOT) — it's the walk
    # continuing after a routine human decision, which gets its own more
    # specific HUMAN_REVIEW_SUBMITTED event below instead.
    session = Session.wrap(data)
    session.data.pop("awaiting_node", None)
    session.data.setdefault("pending_reviews", {})[awaiting] = {
        "approved": bool(approved),
        "feedback": feedback,
        "reviewed_by": reviewed_by,
    }
    session.record_event(
        "HUMAN_REVIEW_SUBMITTED",
        awaiting,
        {"approved": bool(approved), "reviewed_by": reviewed_by},
    )
    return session


def run_pipeline(feature_request: str, project_path: str) -> Session:
    """Blocking: creates the session and walks it to completion (or a
    pause/stop) on the calling thread. Use start_pipeline instead from a
    request handler that must not block on the run itself."""
    session = Session(feature_request, project_path)
    _snapshot_graph_event(session)
    _execute_walk(session)
    return session


def start_pipeline(feature_request: str, project_path: str) -> Session:
    """Creates the session (persisted immediately, so its id/status are
    available right away) and starts the walk on a background thread —
    the caller (factory.py's /run) never blocks on the run itself."""
    session = Session(feature_request, project_path)
    _snapshot_graph_event(session)
    _spawn(session)
    return session


def resume_pipeline(
    session_id: str,
    grants: Optional[dict] = None,
    grant_reason: Optional[str] = None,
    granted_by: Optional[str] = None,
) -> Optional[Session]:
    """Blocking counterpart to start_resume — see _prepare_resume."""
    session = _prepare_resume(session_id, grants, grant_reason, granted_by)
    if session is None:
        return None
    _execute_walk(session)
    return session


def start_resume(
    session_id: str,
    grants: Optional[dict] = None,
    grant_reason: Optional[str] = None,
    granted_by: Optional[str] = None,
) -> Optional[Session]:
    """Continue a session that ended in a resumable status, on a background
    thread — see _prepare_resume for the actual validation/setup."""
    session = _prepare_resume(session_id, grants, grant_reason, granted_by)
    if session is None:
        return None
    _spawn(session)
    return session


def apply_review(
    session_id: str,
    node_id: Optional[str] = None,
    approved: bool = False,
    feedback: Optional[str] = None,
    reviewed_by: Optional[str] = None,
) -> Optional[Session]:
    """Blocking counterpart to submit_review — see _prepare_review."""
    session = _prepare_review(session_id, node_id, approved, feedback, reviewed_by)
    if session is None:
        return None
    _execute_walk(session)
    return session


def submit_review(
    session_id: str,
    node_id: Optional[str] = None,
    approved: bool = False,
    feedback: Optional[str] = None,
    reviewed_by: Optional[str] = None,
) -> Optional[Session]:
    """Records a human's decision at a paused "human_gate" node and resumes
    the walk on a background thread — see _prepare_review for the actual
    validation/setup."""
    session = _prepare_review(session_id, node_id, approved, feedback, reviewed_by)
    if session is None:
        return None
    _spawn(session)
    return session


def cancel_run(session_id: str) -> bool:
    """Signals this session's cancel Event if it's actually running in this
    process. Returns False (a no-op) if it isn't — already finished, or the
    server restarted since it started — rather than fabricating success.
    Deliberately does NOT write to the session file itself: the running
    walk is the sole writer of its own session (see sessions.py's "one
    session, one active writer" assumption everywhere else in this
    codebase), and it will record its own NODE_CANCELLED/RUN_FINISHED
    events within moments of noticing the cancellation — see runtime.py's
    module docstring for where that's checked."""
    return runtime.request_cancel(session_id)
