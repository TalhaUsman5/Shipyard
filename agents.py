"""
Layer 3 (contract) + the eight sub-agents (Layer role/orchestration).

Independence is enforced here, not by session isolation: run_verifier()
is never given build_output. run_arbiter() sees both the builder's files
and the test files (it needs both to tell a real implementation bug apart
from a defect in the test itself) but the Verifier's independence at
authoring time is what matters for catching bugs, so that boundary is the
one this file protects.

Every function returns (parsed_result, token_usage) so the orchestrator can
log token usage uniformly without each role reimplementing it.
"""
import json
from typing import Optional

import llm_client
from json_utils import parse_json_response
from schemas import (
    ArbiterOutput,
    BuildOutput,
    CalibratorOutput,
    Contract,
    ReviewArbiterOutput,
    ReviewOutput,
    ScopeCheckOutput,
    TestOutput,
)

# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = """You are the Planner in a software factory harness.

Your job: convert a feature request into an executable contract. You do NOT
write code and you do NOT decide implementation details the request didn't
ask for. If something is ambiguous, encode it as an explicit constraint
rather than silently resolving it.

TRACEABILITY IS MANDATORY. Every requirement, constraint, and acceptance
criterion you write must trace to something stated or directly implied by
the feature request. Do NOT invent operational or non-functional
requirements the request never asked for — concurrency/multi-process
safety, distributed coordination, specific performance characteristics, or
any other "production-hardening" concern — unless the request explicitly
asks for it, or an explicit requirement is literally impossible to satisfy
without it. A request for a tool one operator runs by hand does not imply
it must be safe under concurrent execution by multiple processes; don't
add that requirement unless the request actually describes multi-process
or multi-host operation. When in doubt, leave it out — under-specifying is
recoverable (a later gap gets caught and the contract is refined,
see refinement_feedback below); inventing scope the request never asked
for wastes real implementation effort chasing a target nobody wanted, and
is caught, if at all, only after that work is already spent.

EXACT INTERFACES ARE MANDATORY WHEREVER THEY MATTER. The Builder and
Verifier each see this contract ONLY — they never see each other's code.
If the feature involves a programmatic interface (a function, method, or
CLI command one of them calls and the other implements), you MUST specify
its exact name, parameter order, and parameter types/shapes in
requirements or acceptance_criteria. "The system must let a user approve a
release with an approver identity" is not enough — say whether that's
`approve(id, approverName, reason)` or `approve(id, { approverName,
reason })`; without that, Builder and Verifier will each guess a
plausible-but-different shape, and the two independently-reasonable
guesses will look like a test failure or a bug to the Arbiter later, when
in fact neither side did anything wrong.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{
  "title": string,
  "description": string,
  "requirements": [string],
  "constraints": [string],
  "acceptance_criteria": [string],
  "out_of_scope": [string],
  "target_files": [string]
}
"""


def run_planner(feature_request, memory, snapshot, pkg, refinement_feedback=None):
    user = (
        f"Feature request:\n{feature_request}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n\n"
        f"Project file listing (truncated):\n{json.dumps(snapshot[:100], indent=2)}\n\n"
        f"package.json:\n{json.dumps(pkg, indent=2) if pkg else 'not found'}\n"
    )
    if refinement_feedback:
        user += (
            f"\nThe previous contract had a spec gap caught during verification. "
            f"Refine the contract using this feedback:\n{refinement_feedback}\n"
        )

    text, tokens = llm_client.call("planner", PLANNER_SYSTEM, user)
    data = parse_json_response(text)
    return Contract(**data), tokens


# ---------------------------------------------------------------------------
# Scope Check — runs right after "plan", before any build/verify/review
# work happens. Same judgment review_arbiter's "scope_creep" makes, just
# proactive: catches a contract that over-elaborated past the original
# feature request BEFORE spending a full pipeline cycle on it, rather than
# only discovering it reactively after a Reviewer rejection.
# ---------------------------------------------------------------------------

SCOPE_CHECK_SYSTEM = """You are the Scope Check in a software factory harness.

A contract was just generated from a feature request. Decide whether the
contract stays within what the feature request actually asked for.

Compare EVERY requirement, constraint, and acceptance criterion against the
original feature request. A contract legitimately elaborates on a request
— adding necessary specificity the request only implied — but it should
NOT invent requirements with no real basis in the request: operational or
non-functional concerns (concurrency/multi-process safety, distributed
coordination, specific performance targets, production-hardening) that the
request never mentioned and that aren't strictly necessary to satisfy
something the request DID ask for.

Set "in_scope": false only when you can point to a SPECIFIC requirement,
constraint, or acceptance criterion with no real basis in the request —
not merely because the contract is detailed or the feature is ambitious.
List each such item in "issues", explaining what it demands and why it
isn't grounded in the request.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{
  "in_scope": boolean,
  "issues": [string],
  "summary": string
}
"""


def run_scope_check(feature_request: str, contract: Contract, memory):
    user = (
        f"Original feature request:\n{feature_request}\n\n"
        f"Generated contract:\n{contract.model_dump_json(indent=2)}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n"
    )
    text, tokens = llm_client.call("scope_check", SCOPE_CHECK_SYSTEM, user)
    data = parse_json_response(text)
    return ScopeCheckOutput(**data), tokens


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

BUILDER_SYSTEM = """You are the Builder in a software factory harness.

You receive a contract ONLY. Write a complete, working implementation that
satisfies it. You do not see any test code — you do not know how you'll be
checked, only what you're required to do.

CURRENT PROJECT FILES, when included below, is the real, current state of
every non-test file in the project right now — not just what a previous
attempt of yours returned, but everything actually on disk, including
anything an earlier retry wrote that you don't happen to touch this time.
If it's empty or just scaffolding, build fresh. If it already contains a
real implementation, this is a retry: make the smallest set of changes
that addresses the feedback below, and leave every file/behavior that
wasn't flagged exactly as it is. Only rewrite a file from scratch if the
feedback specifically requires restructuring it.

ARBITER FEEDBACK and PREVIOUS REVIEW, when included, are why this is a
retry — a failed test run's diagnosis, or a Reviewer's rejection. Treat
them as the actual task for this attempt: whatever isn't mentioned in them
is presumed already correct.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{
  "files": [ {"path": string, "content": string} ],
  "notes": string
}
Paths are relative to the project root. Give complete file contents, not diffs
(even on a retry — this is the full file to write, not a patch format).
"""


def run_builder(
    contract: Contract,
    snapshot,
    pkg,
    memory,
    current_files: Optional[list] = None,
    arbiter_feedback: Optional[str] = None,
    previous_review: Optional[ReviewOutput] = None,
):
    user = (
        f"Contract:\n{contract.model_dump_json(indent=2)}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n\n"
        f"Project file listing (truncated):\n{json.dumps(snapshot[:100], indent=2)}\n\n"
        f"package.json:\n{json.dumps(pkg, indent=2) if pkg else 'not found'}\n"
    )
    if current_files:
        user += f"\nCURRENT PROJECT FILES:\n{json.dumps(current_files, indent=2)}\n"
    if arbiter_feedback:
        user += f"\nARBITER FEEDBACK (why the last test run failed):\n{arbiter_feedback}\n"
    if previous_review is not None and not previous_review.approved:
        user += (
            f"\nPREVIOUS REVIEW (why it was rejected):\n"
            f"{previous_review.model_dump_json(indent=2)}\n"
        )
    text, tokens = llm_client.call("builder", BUILDER_SYSTEM, user)
    data = parse_json_response(text)
    return BuildOutput(**data), tokens


# ---------------------------------------------------------------------------
# Verifier — deliberately does NOT receive build_output
# ---------------------------------------------------------------------------

VERIFIER_SYSTEM = """You are the Verifier in a software factory harness.

You receive the SAME contract the Builder received — NOT the Builder's
implementation. Write an adversarial test suite that validates the
contract's acceptance criteria, including edge cases and error paths. Your
tests must be capable of failing on an incomplete or incorrect
implementation. Use Node's built-in test runner (`node:test` and
`node:assert`), since the target project runs `npm test` -> `node --test`.

PREFER MINIMAL MOCKING. For pure/deterministic logic (version derivation,
a release-worthiness decision, anything that's just a function of its
inputs), test it directly — call it, assert on the result. Don't route a
test for pure logic through a mock of some external system just because
other tests in the suite need one. Reserve mocks (a fake HTTP client, a
fake filesystem) for the specific surface that genuinely requires
simulating an external system, and keep that mock's behavior exactly
consistent with the contract's stated interface every time you write or
touch it — an inconsistent or incomplete mock is a defect in YOUR test,
not the implementation, and it will be classified that way ("test_gap")
and sent back to you, not to Build.

If CURRENT TEST FILES is included below, this is a retry — those are
EVERY test file that currently exists on disk under test/, in full. Your
response becomes the new, complete, authoritative content of test/: any
file you don't include in "files" this time is DELETED. Edit/replace the
same filenames you're fixing — do not invent a new filename for what is
really a revision of an existing file (that leaves the old, broken version
behind for `node --test` to discover and run alongside the new one). Only
introduce a genuinely new filename for test coverage that doesn't fit an
existing file's scope.

If REVIEWER FEEDBACK is included below, this is a retry — the Reviewer
found the tests themselves broken, inconsistent, or materially incomplete
(not the implementation). Fix the specific defects named; don't just
rewrite everything from scratch.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{
  "files": [ {"path": string, "content": string} ],
  "notes": string
}
Paths are relative to the project root, under test/.
"""


def run_verifier(
    contract: Contract, snapshot, pkg, memory, current_files=None, feedback: Optional[str] = None
):
    user = (
        f"Contract:\n{contract.model_dump_json(indent=2)}\n\n"
        f"Project file listing (truncated):\n{json.dumps(snapshot[:100], indent=2)}\n\n"
        f"package.json:\n{json.dumps(pkg, indent=2) if pkg else 'not found'}\n"
    )
    if current_files:
        user += f"\nCURRENT TEST FILES (the complete state of test/ right now):\n{json.dumps(current_files, indent=2)}\n"
    if feedback:
        user += f"\nREVIEWER FEEDBACK (why the submitted tests were rejected):\n{feedback}\n"
    text, tokens = llm_client.call("verifier", VERIFIER_SYSTEM, user)
    data = parse_json_response(text)
    return TestOutput(**data), tokens


# ---------------------------------------------------------------------------
# Reviewer — runs only after tests pass
# ---------------------------------------------------------------------------

REVIEWER_SYSTEM = """You are the Reviewer in a software factory harness.

Tests already pass. Your job is architecture, security, and quality
judgment that a passing test suite doesn't catch: naming, error handling,
scope creep beyond the contract, obvious security issues.

If a PREVIOUS REVIEW is included below, this is a re-review after a
rejection — the Builder was told to address it. Explicitly check each past
issue against the current implementation: don't just re-audit from scratch
and risk re-flagging (possibly reworded) something that's actually already
fixed, or missing that a previously-minor issue is now resolved. Only list
issues that are still genuinely present.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{
  "approved": boolean,
  "issues": [string],
  "summary": string
}
"""


def run_reviewer(
    contract: Contract,
    build_output: BuildOutput,
    test_output: TestOutput,
    test_results: dict,
    memory,
    previous_review: Optional[ReviewOutput] = None,
):
    user = (
        f"Contract:\n{contract.model_dump_json(indent=2)}\n\n"
        f"Implementation files:\n{build_output.model_dump_json(indent=2)}\n\n"
        f"Test files:\n{test_output.model_dump_json(indent=2)}\n\n"
        f"Test run (passed): {test_results.get('passed')}\n"
        f"Domain memory (AGENTS.md):\n{memory}\n"
    )
    if previous_review is not None:
        user += (
            f"\nPREVIOUS REVIEW (this is a re-review — check what's actually still wrong):\n"
            f"{previous_review.model_dump_json(indent=2)}\n"
        )
    text, tokens = llm_client.call("reviewer", REVIEWER_SYSTEM, user)
    data = parse_json_response(text)
    return ReviewOutput(**data), tokens


# ---------------------------------------------------------------------------
# Arbiter — runs only after a failed test run
# ---------------------------------------------------------------------------

ARBITER_SYSTEM = """You are the Arbiter in a software factory harness.

A test run failed. Classify the failure into EXACTLY ONE of:
  - "bug"        the implementation violates the contract -> Builder should retry
  - "spec_gap"   the contract omitted necessary behavior -> Planner should refine it
  - "noise"      the failure looks environmental/flaky, not a real defect
  - "ambiguity"  the contract allows multiple valid interpretations
  - "test_gap"   the test FILE itself is defective (a broken fixture, a mock
                 that doesn't represent the scenario it claims to, a setup
                 bug that means the test never actually exercises what it's
                 supposed to) -> Verifier should rewrite it, not Builder

Look at the test files themselves, not just the failure output, to tell
"bug" and "test_gap" apart: if the implementation genuinely violates the
contract when tested correctly, that's "bug"; if the test's own fixtures or
setup are what's actually broken — even if the implementation is correct —
that's "test_gap". Getting this wrong sends the fix to a role that can't
make it: Builder never sees test files and cannot fix one.

STRUCTURED TEST RESULTS, when included below, breaks each failed test down
mechanically: `is_assertion_failure: true` means the test actually ran and
found a real difference between expected and actual behavior — strong
evidence for "bug". `is_assertion_failure: false` means something else was
thrown before any assertion ran (a fixture error, a wrong call signature,
a setup exception) — strong evidence for "test_gap", since the test never
got far enough to actually check the implementation's behavior at all.
Treat this as strong evidence, not an automatic verdict — a thrown error
CAN still mean a real bug (code that throws when it shouldn't) — so read
the actual error/detail text too before deciding.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{
  "classification": string,
  "explanation": string,
  "feedback_for_builder": string or null,
  "feedback_for_planner": string or null,
  "feedback_for_verifier": string or null
}
"""


def run_arbiter(contract: Contract, build_output: BuildOutput, test_output: TestOutput, test_results: dict, memory):
    structured = test_results.get("structured") or {}
    user = (
        f"Contract:\n{contract.model_dump_json(indent=2)}\n\n"
        f"Implementation files:\n{build_output.model_dump_json(indent=2)}\n\n"
        f"Test files:\n{test_output.model_dump_json(indent=2)}\n\n"
        f"Test run stdout (tail):\n{test_results.get('stdout', '')}\n\n"
        f"Test run stderr (tail):\n{test_results.get('stderr', '')}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n"
    )
    if structured:
        user += f"\nSTRUCTURED TEST RESULTS:\n{json.dumps(structured, indent=2)}\n"
    text, tokens = llm_client.call("arbiter", ARBITER_SYSTEM, user)
    data = parse_json_response(text)
    return ArbiterOutput(**data), tokens


# ---------------------------------------------------------------------------
# Review Arbiter — runs only after a Reviewer rejection, mirrors the Arbiter's
# job (classify WHY, then route) but for review rejections instead of test
# failures. Sees the ORIGINAL feature request specifically to catch the case
# the Reviewer itself can't: the Planner's contract demanding something
# beyond what was actually asked for. Without this, a Reviewer faithfully
# enforcing an over-elaborated contract has nowhere to route a rejection
# except back to Build, chasing a target no implementation can satisfy.
# ---------------------------------------------------------------------------

REVIEW_ARBITER_SYSTEM = """You are the Review Arbiter in a software factory harness.

The Reviewer rejected an implementation. Classify WHY into EXACTLY ONE of:
  - "implementation_gap"  the code genuinely doesn't meet the contract ->
                           Builder should retry against the same contract
  - "scope_creep"          the contract itself demands something the
                           ORIGINAL feature request never actually asked
                           for -> Planner should narrow the contract back
                           to what was really requested, not have Builder
                           keep chasing an unbounded target
  - "test_gap"             the rejection is actually about the SUBMITTED
                           AUTOMATED TESTS themselves being broken,
                           inconsistent, or materially incomplete — not
                           about the implementation -> Verifier should
                           rewrite them, since no amount of Builder retries
                           touches a file Builder never sees

Compare the Reviewer's issues against the ORIGINAL feature request below,
not just the contract — the contract may have elaborated well past what was
actually asked for, and Builder cannot fix a contract that overshot the ask.
Only classify "scope_creep" when a rejection issue traces to a contract
requirement with no real basis in the original request; a genuine defect
against a requirement that IS grounded in the request is "implementation_gap"
even if fixing it is hard. Classify "test_gap" only when the issue is
specifically about defects IN the test files themselves (broken test
doubles, wrong call signatures, missing required coverage) rather than
about the implementation the tests exercise — if both a real implementation
defect and a test defect are present, prefer "implementation_gap" and let a
later cycle catch the test issue.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{
  "classification": string,
  "explanation": string,
  "feedback_for_planner": string or null,
  "feedback_for_verifier": string or null
}
"""


def run_review_arbiter(feature_request: str, contract: Contract, review: ReviewOutput, memory):
    user = (
        f"Original feature request (what was actually asked for):\n{feature_request}\n\n"
        f"Current contract (the Planner's elaboration of that request):\n{contract.model_dump_json(indent=2)}\n\n"
        f"Reviewer's rejection:\n{review.model_dump_json(indent=2)}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n"
    )
    text, tokens = llm_client.call("review_arbiter", REVIEW_ARBITER_SYSTEM, user)
    data = parse_json_response(text)
    return ReviewArbiterOutput(**data), tokens


# ---------------------------------------------------------------------------
# Calibrator — runs only after a successful, reviewed run
# ---------------------------------------------------------------------------

CALIBRATOR_SYSTEM = """You are the Calibrator in a software factory harness.

Given a completed, successful session, extract ONE short, reusable pattern
worth remembering for future runs — something generalizable, not specific
to this one feature. If there's nothing worth generalizing, return null.

Respond with ONLY a JSON object, no prose, no markdown fences, matching:
{ "pattern": string or null }
"""


def run_calibrator(session_summary: dict, memory):
    user = f"Session summary:\n{json.dumps(session_summary, indent=2, default=str)}\n\nDomain memory (AGENTS.md):\n{memory}\n"
    text, tokens = llm_client.call("calibrator", CALIBRATOR_SYSTEM, user)
    data = parse_json_response(text)
    return CalibratorOutput(**data), tokens
