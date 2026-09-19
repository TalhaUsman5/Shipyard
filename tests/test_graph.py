"""
Sanity checks on the declared topology itself, independent of the engine
that walks it. These catch a typo'd node id in `next`/`on_fail`/a route
target before it would only ever surface as a confusing runtime KeyError
mid-run.
"""
from graph import ENTRY_NODE, NODE_OUTPUT_MODELS, PARALLEL_GROUPS, PHASE_GRAPH
from schemas import FAIL_SENTINEL


def _all_referenced_targets():
    targets = set()
    for spec in PHASE_GRAPH.values():
        if spec.next:
            targets.add(spec.next)
        if spec.on_fail:
            targets.add(spec.on_fail.target)
        for route in spec.routes.values():
            targets.add(route.target)
    return targets


def test_entry_node_exists():
    assert ENTRY_NODE in PHASE_GRAPH


def test_every_route_target_is_a_known_node_or_the_fail_sentinel():
    for target in _all_referenced_targets():
        assert target == FAIL_SENTINEL or target in PHASE_GRAPH, f"unknown node id referenced: {target!r}"


def test_gate_nodes_declare_on_fail():
    for node_id, spec in PHASE_GRAPH.items():
        if spec.kind == "gate":
            assert spec.on_fail is not None, f"gate {node_id!r} has no on_fail route"


def test_classifier_nodes_declare_at_least_one_route():
    for node_id, spec in PHASE_GRAPH.items():
        if spec.kind == "classifier":
            assert spec.routes, f"classifier {node_id!r} has no routes"


def test_only_calibrate_is_a_terminal_work_node():
    terminal = [n for n, spec in PHASE_GRAPH.items() if spec.kind == "work" and spec.next is None]
    assert terminal == ["calibrate"]


def test_node_output_models_only_reference_real_nodes():
    for node_id in NODE_OUTPUT_MODELS:
        assert node_id in PHASE_GRAPH


def test_budgeted_routes_declare_a_positive_max_uses():
    for spec in PHASE_GRAPH.values():
        routes = list(spec.routes.values())
        if spec.on_fail:
            routes.append(spec.on_fail)
        for route in routes:
            if route.budget_key:
                assert route.max_uses and route.max_uses > 0, (
                    f"route with budget_key={route.budget_key!r} has no positive max_uses"
                )


def test_routes_sharing_a_budget_key_declare_the_same_max_uses():
    """A budget's ceiling is read from whichever specific route happens to
    fire (route.max_uses), not from the budget_key alone — so two routes
    sharing a key (e.g. arbiter's and review_arbiter's "test_gap", both
    keyed "test_retry") MUST declare identical max_uses, or the effective
    ceiling would silently depend on which one triggered first."""
    max_uses_by_key = {}
    for spec in PHASE_GRAPH.values():
        routes = list(spec.routes.values())
        if spec.on_fail:
            routes.append(spec.on_fail)
        for route in routes:
            if not route.budget_key:
                continue
            seen = max_uses_by_key.setdefault(route.budget_key, route.max_uses)
            assert seen == route.max_uses, (
                f"budget_key={route.budget_key!r} has inconsistent max_uses across routes: "
                f"{seen!r} vs {route.max_uses!r}"
            )


def test_parallel_group_members_share_one_next():
    """engine._run_parallel_group reads the group's shared `next` off of
    just one member (sorted(group)[0]) — every member MUST actually agree,
    or whichever one happens to be picked would silently decide where the
    whole group routes to next for every member, not just itself."""
    for group in PARALLEL_GROUPS.values():
        nexts = {PHASE_GRAPH[nid].next for nid in group}
        assert len(nexts) == 1, f"parallel group {sorted(group)!r} members disagree on `next`: {nexts!r}"


def test_parallel_group_keys_cover_every_member():
    """PARALLEL_GROUPS is keyed by every member id, not just one entry
    point — so the walker recognizes the group whether it's reached via the
    forward chain or via a solo retry route that targets one member
    directly (e.g. arbiter's test_gap -> "verify")."""
    for key, group in PARALLEL_GROUPS.items():
        assert key in group
        for member in group:
            assert PARALLEL_GROUPS.get(member) == group, (
                f"{member!r} is in group {sorted(group)!r} but PARALLEL_GROUPS[{member!r}] doesn't match"
            )
