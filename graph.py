"""
The phase graph itself: the whole pipeline's topology in one place, as
data (schemas.NodeSpec / ClassifierRoute) — not as Python control flow.
Read this file to see the whole shape of the pipeline; read engine.py to
see what each node actually does when it runs.

                          ,-> build  -,
    plan -> plan_review -+           +-> test_gate --pass--> review -> review_gate --pass--> calibrate -> (done)
                |         `-> verify -'         |                              |
             reject                          fail                           fail
                v                              v                              v
              plan                          arbiter                  review_arbiter
   (reset: plan, human-paced) (bug/noise/spec_gap/ambiguity/test_gap) (implementation_gap/scope_creep/test_gap)
                                     bug      -> build (reset: build)                 budget: build_retry x4
                                     noise    -> test_gate (reset: none — rerun as-is) budget: build_retry x4
                                     spec_gap -> plan (reset: plan, build, verify)     budget: plan_retry x1
                                     ambiguity-> plan (reset: plan, build, verify)     budget: ambiguity_retry x1
                                     test_gap -> verify (reset: verify)                budget: test_retry x1 (shared)

"build" and "verify" run CONCURRENTLY, not sequentially — see
PARALLEL_GROUPS below and engine.py's _run_parallel_group. Neither depends
on the other's output (the Verifier is deliberately never given
build_output — see agents.py), and test_gate is the actual synchronization
point, so there's no reason to make one wait on the other. A route that
resets only one of them (e.g. "bug" resets just "build", "test_gap" resets
just "verify") still only reruns that one — the other stays cached, same
as before this existed.

                  implementation_gap -> build (reset: build, review)  budget: review_retry x6
                  scope_creep        -> plan (reset: plan, build, verify, review) budget: scope_retry x2
                  test_gap           -> verify (reset: verify, review) budget: test_retry x1 (shared)

A run only ever reaches a true dead end (failed_needs_human with no
further route) by exhausting one of these budgets, or via an
unrecognized classification label — every declared classification has
somewhere bounded to go first.

"plan_review" runs right after "plan", before any build/verify/review work
happens: a human reads the Planner's contract and either approves it
(-> build) or rejects it with feedback (-> plan, reset, re-derive). This
replaced an earlier automated "scope_gate" (an LLM judging the same
"did the contract overshoot the request" question scope_creep below
catches reactively) — a human call on the contract is strictly more
trustworthy than an LLM checking another LLM's output, and it's the one
place in the pipeline a human is guaranteed to look before real
implementation effort is spent. It has no retry budget: a human decides
how many refinement passes are worth doing, not a fixed counter.

A Reviewer rejection doesn't route straight to "build" — it goes through
"review_arbiter" first, the same judgment-then-route pattern test_gate
uses via "arbiter". Most rejections are genuine implementation gaps and
route to build exactly as before (build's own forward edge already runs
verify(cached)->test_gate(always fresh)->review(reset, so it re-judges the
new code)->review_gate, so one retry re-earns the test gate too, not just
a fresh Review opinion on unchanged code). But a Reviewer can be faithfully
enforcing a contract that itself demands more than the ORIGINAL feature
request ever asked for — the Planner over-elaborated it — and Build can't
fix a contract that overshot the ask. review_arbiter sees the original
feature request specifically to catch that case and route back to "plan"
to narrow the contract instead, bounded by its own small "scope_retry"
budget so a genuine scope correction happens once, not indefinitely. This
budget is no longer shared with anything else — an earlier "scope_gate"
node used the same key proactively, right after "plan"; it's since been
replaced by the "plan_review" human gate above, which has no budget at
all (a human, not a counter, decides how many refinement passes to allow).

"ambiguity" routes exactly like "spec_gap" (reset plan/build/verify, feed
the Arbiter's feedback_for_planner into the Planner's next attempt) under
its own budget — the two failure reasons are mechanically identical
(the contract needs a clarifying pass) but kept distinguishable in the
trace. Resetting "verify" too means the Verifier also rewrites its tests
against the clarified contract, so this is a joint re-derivation of both
sides, not "narrow the contract until the existing test passes." The same
is true of "scope_creep" relative to "implementation_gap".

"test_gap" exists on BOTH classifiers, because the same problem can surface
two different ways: arbiter's "test_gap" catches it when a broken test
fixture makes test_gate fail outright (found live: a FakeGitHub mock
substituting a default value that masked the exact scenario a test claimed
to cover — the Arbiter's own explanation named the fixture defect
precisely, but with no route for it the only available label was "bug",
which sent it to Build and burned a real retry on something Build could
never fix). review_arbiter's "test_gap" catches it when the Reviewer
notices the submitted tests are inconsistent or incomplete even though
they technically pass. Neither of the other routes on either classifier
can fix a defect IN the tests: "bug"/"implementation_gap" reset "build"
(and "review", for review_arbiter) but never "verify", and Build never
sees test code anyway (BUILDER_SYSTEM's own rule) — so the same test
defect would recur every single cycle no matter how many bug or
implementation_gap retries ran, since nothing ever gave the Verifier a
chance to rewrite the file. Both routes share the "test_retry" budget
(same resource, two triggers) and MUST declare the same max_uses for that
to mean anything consistent. review_arbiter's version also resets "review"
(it already ran and rejected once, so without resetting it review_gate
would just re-check that same stale rejection instead of judging the
rewritten tests) — arbiter's version doesn't, because at that point in the
graph "review" hasn't run yet.

Gate and classifier nodes always execute when the walker reaches them —
never skipped, never cached — because they must reflect current truth.
"test_gate"/"review_gate" happen to be cheap and deterministic (a real
`npm test` run; `review.approved`), so re-running them fresh every time
costs nothing. "plan_review" is the one exception: its verdict is a human
decision, not a re-derivable fact, so an already-*approved* verdict IS
trusted across a resume (see engine.py's _walk) — re-asking a human to
approve the exact same contract again after an unrelated crash elsewhere
in the pipeline would be a regression, not more truthful. A rejection
never has this problem: it always routes straight back to "plan" in the
same walk, so the contract has already changed by the time "plan_review"
is reached again. Work nodes (plan/build/verify/review/calibrate) are the
opposite — skipped and
their prior output reused whenever they're already marked "succeeded" and
weren't named in a route's `reset` list — this is what makes retries *and*
resuming an interrupted run the same mechanism: both are just "walk the
graph again and let already-succeeded work nodes skip themselves."
"""
from schemas import (
    ArbiterOutput,
    BuildOutput,
    CalibratorOutput,
    ClassifierRoute,
    Contract,
    NodeSpec,
    ReviewArbiterOutput,
    ReviewOutput,
    TestOutput,
)

ENTRY_NODE = "plan"

# Global safety net: caps total node executions in a single walk, independent
# of any per-route budget below. Protects against a misconfigured graph
# (e.g. a route cycle with no budget_key) looping forever.
#
# Must comfortably exceed the worst case every per-route budget allows,
# summed — otherwise this cap fires first and silently eats a legitimate
# retry before its own budget check ever gets to run. Worst case here:
# base path (9 nodes: plan/plan_review/build/verify/test_gate/arbiter/
# review/review_gate/review_arbiter/calibrate — plan_review itself is
# unbounded since a human paces it, not counted against this LLM-retry cap)
# + build_retry's 4 uses x <=6 nodes = 24
# + plan_retry's 1 use x <=5 nodes = 5
# + ambiguity_retry's 1 use x <=5 nodes = 5
# + review_retry's 6 uses x <=6 nodes (build/verify/test_gate/review/
#   review_gate/review_arbiter) = 36
# + scope_retry's 2 uses x <=8 nodes = 16 (review_arbiter's scope_creep only
#   now — no longer shared with a proactive scope_gate, see graph docstring)
# + test_retry's 1 use x <=6 nodes = 6 (shared between arbiter's and
#   review_arbiter's identically-named test_gap routes)
# = 10 + 24 + 5 + 5 + 36 + 16 + 6 = 102. Rounded up with real headroom (not
# just to the exact worst case) for future routes. "build"+"verify" running
# concurrently as one PARALLEL_GROUPS step (see below) only ever REDUCES
# how many outer-loop iterations a pass through the graph costs relative to
# this count (each "<=N nodes" above still counts build and verify as two),
# so this cap keeps even more headroom than the arithmetic above assumes —
# never recalculated down, only ever conservative in the safer direction.
MAX_TOTAL_ITERATIONS = 140

PHASE_GRAPH: dict[str, NodeSpec] = {
    "plan": NodeSpec(id="plan", kind="work", next="plan_review"),
    "plan_review": NodeSpec(
        id="plan_review",
        kind="human_gate",
        next="build",
        # No budget: a human decides how many refinement passes are worth
        # doing, not a fixed counter — see the "plan_review" section of
        # this file's module docstring.
        on_fail=ClassifierRoute(target="plan", reset=["plan"]),
    ),
    # "build" and "verify" run concurrently — see PARALLEL_GROUPS below and
    # the module docstring. Both declare the SAME `next`: neither one's
    # "next" is actually followed on its own by the walker once it's part
    # of a group (_run_parallel_group returns the group's shared next
    # directly), but keeping them equal here is the invariant
    # tests/test_graph.py checks, and is what makes "the group's next" a
    # well-defined thing to read off of either member.
    "build": NodeSpec(id="build", kind="work", next="test_gate"),
    "verify": NodeSpec(id="verify", kind="work", next="test_gate"),
    "test_gate": NodeSpec(id="test_gate", kind="gate", next="review", on_fail=ClassifierRoute(target="arbiter")),
    "arbiter": NodeSpec(
        id="arbiter",
        kind="classifier",
        routes={
            "bug": ClassifierRoute(
                target="build", reset=["build"], budget_key="build_retry", max_uses=4
            ),
            "noise": ClassifierRoute(
                target="test_gate", reset=[], budget_key="build_retry", max_uses=4
            ),
            "spec_gap": ClassifierRoute(
                target="plan",
                reset=["plan", "build", "verify"],
                budget_key="plan_retry",
                max_uses=1,
            ),
            # Same shape as spec_gap — reset plan/build/verify so the
            # Planner clarifies the contract AND the Verifier rewrites its
            # tests against that clarification (this isn't "narrow the
            # contract until the existing rigid test passes"; both sides
            # re-derive from the disambiguated spec). A separate budget key
            # from plan_retry keeps the two failure reasons distinguishable
            # in the trace even though the route mechanics are identical.
            "ambiguity": ClassifierRoute(
                target="plan",
                reset=["plan", "build", "verify"],
                budget_key="ambiguity_retry",
                max_uses=1,
            ),
            # The test FILE is defective, not the implementation — route to
            # Verifier, not Builder (which never sees test code and could
            # never fix this no matter how many "bug" retries it got).
            # "review" isn't reset: at this point in the graph it hasn't
            # run yet, same as spec_gap/ambiguity above. Shares the
            # "test_retry" budget with review_arbiter's identically-named
            # route below — same underlying resource ("how many times will
            # we let Verifier redo its tests"), reached from two different
            # triggers (a test_gate failure here; a Reviewer rejection
            # there). Both routes MUST declare the same max_uses, since
            # whichever one fires reads its own max_uses against a shared
            # "used" counter — a mismatch would make the effective ceiling
            # depend on which one happened to trigger first.
            "test_gap": ClassifierRoute(
                target="verify",
                reset=["verify"],
                budget_key="test_retry",
                max_uses=1,
            ),
        },
    ),
    "review": NodeSpec(id="review", kind="work", next="review_gate"),
    "review_gate": NodeSpec(
        id="review_gate",
        kind="gate",
        next="calibrate",
        on_fail=ClassifierRoute(target="review_arbiter"),
    ),
    "review_arbiter": NodeSpec(
        id="review_arbiter",
        kind="classifier",
        routes={
            "implementation_gap": ClassifierRoute(
                target="build", reset=["build", "review"], budget_key="review_retry", max_uses=6
            ),
            # The contract itself overshot the original ask — narrow it
            # back rather than have Build keep chasing an unbounded
            # target. Same shape as arbiter's spec_gap/ambiguity: reset
            # plan/build/verify so the Verifier also re-derives its tests
            # against the narrowed contract, under its own small budget so
            # a genuine correction happens once, not indefinitely. Unlike
            # spec_gap/ambiguity (triggered before "review" ever runs),
            # "review" must ALSO be reset here — it already ran and
            # rejected once, so without resetting it review_gate would
            # just re-check that same stale rejection against the
            # narrowed contract instead of getting a fresh verdict.
            "scope_creep": ClassifierRoute(
                target="plan",
                reset=["plan", "build", "verify", "review"],
                budget_key="scope_retry",
                max_uses=2,
            ),
            # The rejection is about the SUBMITTED TESTS themselves, not
            # the implementation — route straight to "verify", not "build".
            # No amount of Build retries can fix a defect in a file Build
            # never sees; only resetting "verify" gives the Verifier a
            # chance to rewrite it. "review" is also reset (same reason as
            # scope_creep above: it already ran and rejected once) so the
            # rewritten tests get judged fresh, not against a stale verdict.
            "test_gap": ClassifierRoute(
                target="verify",
                reset=["verify", "review"],
                budget_key="test_retry",
                max_uses=1,
            ),
        },
    ),
    "calibrate": NodeSpec(id="calibrate", kind="work", next=None, fatal_on_error=False),
}

# Which pydantic model a node's persisted `output` deserializes back into on
# resume. Gate nodes aren't listed — their output is a plain dict (test
# results / review-gate summary), not a role schema.
NODE_OUTPUT_MODELS = {
    "plan": Contract,
    "build": BuildOutput,
    "verify": TestOutput,
    "arbiter": ArbiterOutput,
    "review": ReviewOutput,
    "review_arbiter": ReviewArbiterOutput,
    "calibrate": CalibratorOutput,
}

# Work-node ids that run CONCURRENTLY as a single step of the walk, keyed
# by every id that's a member — so the walker recognizes the group whether
# it arrives via the forward chain (plan_review -> "build") or via a solo
# retry route that targets just one member directly (e.g. arbiter's
# "test_gap" -> "verify"). See this file's module docstring for why build
# and verify specifically are safe to run this way, and
# engine._run_parallel_group for how a route that resets only one member
# still reruns only that one. Every member of a group MUST declare the
# same `next` — tests/test_graph.py enforces this.
PARALLEL_GROUPS: dict[str, frozenset] = {
    "build": frozenset({"build", "verify"}),
    "verify": frozenset({"build", "verify"}),
}
