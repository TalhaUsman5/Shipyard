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
from dataclasses import dataclass, field
from typing import Any, Optional

import agents
import execution
from context import project_snapshot, read_package_json
from graph import ENTRY_NODE, MAX_TOTAL_ITERATIONS, NODE_OUTPUT_MODELS, PHASE_GRAPH
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
    pkg: Optional[dict]
    session: Session
    outputs: dict = field(default_factory=dict)
    scratch: dict = field(default_factory=dict)

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
        ctx.feature_request, ctx.memory, ctx.snapshot, ctx.pkg, refinement_feedback=feedback
    )
    return WorkResult(output=contract, tokens=tokens)


def _handle_scope_gate(ctx: RunContext) -> GateResult:
    contract = ctx.get("plan")
    assert contract is not None  # graph invariant: "scope_gate" only runs after "plan" has succeeded
    result, tokens = agents.run_scope_check(ctx.feature_request, contract, ctx.memory)
    if not result.in_scope:
        # Same feedback channel spec_gap/ambiguity/scope_creep already use
        # to hand the next "plan" run something to act on.
        issues = "; ".join(result.issues) if result.issues else result.summary
        ctx.scratch["planner_refinement_feedback"] = (
            f"The contract includes requirements with no basis in the original feature request: {issues}"
        )
    return GateResult(passed=result.in_scope, data={"issues": result.issues, "summary": result.summary}, tokens=tokens)


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
    listing = [p for p in project_snapshot(ctx.project_path) if not _is_test_path(p)]
    current_files = execution.read_text_files(ctx.project_path, listing)

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
    )
    written = execution.apply_files(ctx.project_path, build_output.files)
    return WorkResult(output=build_output, tokens=tokens, extra={"files_written": written})


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
    listing = [p for p in project_snapshot(ctx.project_path) if _is_test_path(p)]
    current_files = execution.read_text_files(ctx.project_path, listing)

    test_output, tokens = agents.run_verifier(
        contract, ctx.snapshot, ctx.pkg, ctx.memory, current_files=current_files, feedback=feedback
    )
    # test/ is the Verifier's own directory to fully own, not a patch onto
    # someone else's files — its response IS the complete new state of
    # test/, so anything it didn't include this time gets deleted, not
    # left behind as an abandoned prior generation.
    written = execution.apply_files_replacing_directory(ctx.project_path, "test", test_output.files)
    return WorkResult(output=test_output, tokens=tokens, extra={"files_written": written})


def _handle_test_gate(ctx: RunContext) -> GateResult:
    test_results = execution.run_tests(ctx.project_path)
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
    if calib.pattern:
        append_pattern(calib.pattern)
    return WorkResult(output=calib, tokens=tokens)


NODE_HANDLERS = {
    "plan": _handle_plan,
    "scope_gate": _handle_scope_gate,
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


def _run_work_or_classifier(ctx: RunContext, session: Session, node_id: str, spec):
    handler = NODE_HANDLERS[node_id]
    session.node_started(node_id)
    try:
        result = handler(ctx)
    except Exception as e:  # noqa: BLE001 - deliberately broad: any node failure must be caught and logged
        session.node_failed(node_id, str(e))
        if spec.fatal_on_error:
            session.finish("failed")
            raise _Abort()
        return None

    ctx.set(node_id, result.output)
    session.node_succeeded(node_id, result.output, result.tokens, getattr(result, "extra", None))
    return result


def _run_gate(ctx: RunContext, session: Session, node_id: str) -> GateResult:
    handler = NODE_HANDLERS[node_id]
    session.node_started(node_id)
    try:
        result = handler(ctx)
    except Exception as e:  # noqa: BLE001
        session.node_failed(node_id, str(e))
        session.finish("failed")
        raise _Abort()

    ctx.set(node_id, result.data)
    session.node_succeeded(node_id, result.data, result.tokens)
    return result


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

        iterations += 1
        if iterations > MAX_TOTAL_ITERATIONS:
            session.finish("failed_needs_human")
            return

        spec = PHASE_GRAPH[node_id]

        if spec.kind == "work":
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


def run_pipeline(feature_request: str, project_path: str) -> Session:
    session = Session(feature_request, project_path)
    _snapshot_graph_event(session)

    try:
        resolved_path = _resolve_project_path(project_path)
    except ValueError as e:
        session.record_event("ERROR", data={"stage": "init", "error": str(e)})
        session.finish("failed")
        return session

    ctx = RunContext(
        feature_request=feature_request,
        project_path=resolved_path,
        memory=load_memory(),
        snapshot=project_snapshot(resolved_path),
        pkg=read_package_json(resolved_path),
        session=session,
    )

    try:
        _walk(session, ctx, ENTRY_NODE)
    except _Abort:
        pass
    return session


def resume_pipeline(
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

    try:
        resolved_path = _resolve_project_path(session.data["project_path"])
    except ValueError as e:
        session.record_event("ERROR", data={"stage": "resume_init", "error": str(e)})
        session.finish("failed")
        return session

    ctx = RunContext(
        feature_request=session.data["feature_request"],
        project_path=resolved_path,
        memory=load_memory(),
        snapshot=project_snapshot(resolved_path),
        pkg=read_package_json(resolved_path),
        session=session,
    )
    for node_id, node in session.data["nodes"].items():
        if node["status"] == "succeeded" and node_id in NODE_OUTPUT_MODELS:
            ctx.set(node_id, _rehydrate(node_id, node["output"]))

    try:
        _walk(session, ctx, ENTRY_NODE)
    except _Abort:
        pass
    return session
