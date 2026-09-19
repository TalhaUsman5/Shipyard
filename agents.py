"""
Layer 3 (contract) + the seven LLM sub-agents (Layer role/orchestration).
The contract's scope is now checked by a human ("plan_review" in
graph.py/engine.py), not an eighth LLM role.

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
import prompts
from context import describe_snapshot
from json_utils import parse_json_response
from schemas import (
    ArbiterOutput,
    BuildOutput,
    CalibratorOutput,
    Contract,
    ReviewArbiterOutput,
    ReviewOutput,
    TestOutput,
)

# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def run_planner(feature_request, memory, snapshot, pkg, refinement_feedback=None, snapshot_truncated=False):
    user = (
        f"Feature request:\n{feature_request}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n\n"
        f"{describe_snapshot(snapshot, snapshot_truncated)}\n\n"
        f"package.json:\n{json.dumps(pkg, indent=2) if pkg else 'not found'}\n"
    )
    if refinement_feedback:
        user += (
            f"\nThe previous contract had a spec gap caught during verification. "
            f"Refine the contract using this feedback:\n{refinement_feedback}\n"
        )

    text, tokens = llm_client.call("planner", prompts.load("planner"), user)
    data = parse_json_response(text)
    return Contract(**data), tokens


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def run_builder(
    contract: Contract,
    snapshot,
    pkg,
    memory,
    current_files: Optional[list] = None,
    arbiter_feedback: Optional[str] = None,
    previous_review: Optional[ReviewOutput] = None,
    snapshot_truncated: bool = False,
    omitted_current_files: int = 0,
):
    user = (
        f"Contract:\n{contract.model_dump_json(indent=2)}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n\n"
        f"{describe_snapshot(snapshot, snapshot_truncated)}\n\n"
        f"package.json:\n{json.dumps(pkg, indent=2) if pkg else 'not found'}\n"
    )
    if current_files:
        note = f" ({omitted_current_files} more not shown — byte cap reached)" if omitted_current_files else ""
        user += f"\nCURRENT PROJECT FILES{note}:\n{json.dumps(current_files, indent=2)}\n"
    if arbiter_feedback:
        user += f"\nARBITER FEEDBACK (why the last test run failed):\n{arbiter_feedback}\n"
    if previous_review is not None and not previous_review.approved:
        user += (
            f"\nPREVIOUS REVIEW (why it was rejected):\n"
            f"{previous_review.model_dump_json(indent=2)}\n"
        )
    text, tokens = llm_client.call("builder", prompts.load("builder"), user)
    data = parse_json_response(text)
    return BuildOutput(**data), tokens


# ---------------------------------------------------------------------------
# Verifier — deliberately does NOT receive build_output
# ---------------------------------------------------------------------------

def run_verifier(
    contract: Contract,
    snapshot,
    pkg,
    memory,
    current_files=None,
    feedback: Optional[str] = None,
    snapshot_truncated: bool = False,
    omitted_current_files: int = 0,
):
    user = (
        f"Contract:\n{contract.model_dump_json(indent=2)}\n\n"
        f"{describe_snapshot(snapshot, snapshot_truncated)}\n\n"
        f"package.json:\n{json.dumps(pkg, indent=2) if pkg else 'not found'}\n"
    )
    if current_files:
        note = f" ({omitted_current_files} more not shown — byte cap reached)" if omitted_current_files else ""
        user += f"\nCURRENT TEST FILES{note} (the complete state of test/ right now):\n{json.dumps(current_files, indent=2)}\n"
    if feedback:
        user += f"\nREVIEWER FEEDBACK (why the submitted tests were rejected):\n{feedback}\n"
    text, tokens = llm_client.call("verifier", prompts.load("verifier"), user)
    data = parse_json_response(text)
    return TestOutput(**data), tokens


# ---------------------------------------------------------------------------
# Reviewer — runs only after tests pass
# ---------------------------------------------------------------------------

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
    # A Reviewer's whole output is {approved, issues[], summary} — well
    # under 16000 tokens of plain JSON, but still a wide margin over that
    # (not the tightest number that "should" work) precisely because of
    # the reasoning-token risk documented in llm_client.call's docstring: a
    # cap too tight can exhaust itself on reasoning alone and return empty
    # content before ever writing the JSON.
    text, tokens = llm_client.call("reviewer", prompts.load("reviewer"), user, max_tokens=6000)
    data = parse_json_response(text)
    return ReviewOutput(**data), tokens


# ---------------------------------------------------------------------------
# Arbiter — runs only after a failed test run
# ---------------------------------------------------------------------------

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
    # Same reasoning as run_reviewer's max_tokens: {classification,
    # explanation, feedback_for_*} is a small, bounded shape — 6000 is a
    # wide margin over its plain-JSON size, not the tightest cap that works.
    text, tokens = llm_client.call("arbiter", prompts.load("arbiter"), user, max_tokens=6000)
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

def run_review_arbiter(feature_request: str, contract: Contract, review: ReviewOutput, memory):
    user = (
        f"Original feature request (what was actually asked for):\n{feature_request}\n\n"
        f"Current contract (the Planner's elaboration of that request):\n{contract.model_dump_json(indent=2)}\n\n"
        f"Reviewer's rejection:\n{review.model_dump_json(indent=2)}\n\n"
        f"Domain memory (AGENTS.md):\n{memory}\n"
    )
    # Same shape and same reasoning as run_arbiter's max_tokens.
    text, tokens = llm_client.call("review_arbiter", prompts.load("review_arbiter"), user, max_tokens=6000)
    data = parse_json_response(text)
    return ReviewArbiterOutput(**data), tokens


# ---------------------------------------------------------------------------
# Calibrator — runs only after a successful, reviewed run
# ---------------------------------------------------------------------------

def run_calibrator(session_summary: dict, memory):
    user = f"Session summary:\n{json.dumps(session_summary, indent=2, default=str)}\n\nDomain memory (AGENTS.md):\n{memory}\n"
    # Small, bounded output — a handful of short strings at most — but
    # still a real margin above that, not the bare minimum, for the same
    # reasoning-token-exhaustion reason as the other roles above.
    text, tokens = llm_client.call("calibrator", prompts.load("calibrator"), user, max_tokens=2000)
    data = parse_json_response(text)
    return CalibratorOutput(**data), tokens
