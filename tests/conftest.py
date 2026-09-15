"""
Shared fixtures for the test suite.

Everything here is deterministic and offline: agents.run_* and
execution.run_tests are monkeypatched (fake_agents) so tests never call a
real LLM and never require npm/Node to be installed. WORKSPACE_DIR and
SESSIONS_DIR are redirected into pytest's tmp_path (isolated_dirs, autouse)
so no test ever touches the real workspace/ or sessions/ directories.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import agents
import engine
import execution
import sessions
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

CONTRACT_FIELDS = dict(
    title="Add greet helper",
    description="A small greeting helper.",
    requirements=["Export greet(name)"],
    constraints=[],
    acceptance_criteria=["greet('Ada') === 'Hello, Ada!'"],
    out_of_scope=[],
    target_files=["src/greeting.js"],
)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    sdir = tmp_path / "sessions"
    ws.mkdir()
    sdir.mkdir()
    monkeypatch.setattr(engine, "WORKSPACE_DIR", str(ws))
    monkeypatch.setattr(sessions, "SESSIONS_DIR", str(sdir))
    return {"workspace": str(ws), "sessions": str(sdir)}


class Calls(dict):
    """Dict with attribute access, purely so assertions can read
    `calls.build` instead of `calls["build"]`."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e

    def __setattr__(self, key, value):
        self[key] = value


@pytest.fixture
def calls():
    return Calls(
        plan=0, scope_check=0, build=0, verify=0, test_run=0, arbiter=0, review=0, review_arbiter=0, calibrate=0
    )


class Knobs:
    """Tunable behavior for the fake agents, set per-test."""

    def __init__(self):
        self.build_ok_from_call = 1
        self.arbiter_classification = "bug"
        self.review_approved_from_call = 1
        self.review_arbiter_classification = "implementation_gap"
        self.scope_check_in_scope = True


@pytest.fixture
def knobs():
    return Knobs()


@pytest.fixture
def fake_agents(monkeypatch, calls, knobs):
    """Monkeypatches every agents.run_* function and execution.run_tests
    with deterministic fakes. Tests tune behavior via `knobs` and read call
    counts via `calls`."""

    def fake_planner(feature_request, memory, snapshot, pkg, refinement_feedback=None):
        calls["plan"] += 1
        return Contract(**CONTRACT_FIELDS), {"input": 1, "output": 1}

    def fake_scope_check(feature_request, contract, memory):
        calls["scope_check"] += 1
        in_scope = knobs.scope_check_in_scope
        return (
            ScopeCheckOutput(
                in_scope=in_scope,
                issues=[] if in_scope else ["acceptance criteria invent a requirement not in the request"],
                summary="in scope" if in_scope else "contract overshoots the original request",
            ),
            {"input": 1, "output": 1},
        )

    received_current_files = []
    received_build_arbiter_feedback = []
    received_build_previous_review = []
    received_previous_reviews = []

    def fake_builder(contract, snapshot, pkg, memory, current_files=None, arbiter_feedback=None, previous_review=None):
        calls["build"] += 1
        received_current_files.append(current_files)
        received_build_arbiter_feedback.append(arbiter_feedback)
        received_build_previous_review.append(previous_review)
        ok = calls["build"] >= knobs.build_ok_from_call
        content = "// correct" if ok else "// wrong"
        return BuildOutput(files=[{"path": "src/greeting.js", "content": content}]), {"input": 1, "output": 1}

    received_verifier_feedback = []
    received_verifier_current_files = []

    def fake_verifier(contract, snapshot, pkg, memory, current_files=None, feedback=None):
        calls["verify"] += 1
        received_verifier_feedback.append(feedback)
        received_verifier_current_files.append(current_files)
        return TestOutput(files=[{"path": "test/greeting.test.js", "content": "// test"}]), {"input": 1, "output": 1}

    def fake_arbiter(contract, build_output, test_output, test_results, memory):
        calls["arbiter"] += 1
        return (
            ArbiterOutput(
                classification=knobs.arbiter_classification,
                explanation="synthetic failure",
                feedback_for_builder="fix the greeting string",
                feedback_for_planner="clarify the greeting format",
                feedback_for_verifier="fix the broken test fixture",
            ),
            {"input": 1, "output": 1},
        )

    def fake_reviewer(contract, build_output, test_output, test_results, memory, previous_review=None):
        calls["review"] += 1
        received_previous_reviews.append(previous_review)
        approved = calls["review"] >= knobs.review_approved_from_call
        return (
            ReviewOutput(
                approved=approved,
                issues=[] if approved else ["naming is unclear"],
                summary="looks good" if approved else "needs work",
            ),
            {"input": 1, "output": 1},
        )

    def fake_review_arbiter(feature_request, contract, review, memory):
        calls["review_arbiter"] += 1
        return (
            ReviewArbiterOutput(
                classification=knobs.review_arbiter_classification,
                explanation="synthetic review rejection classification",
                feedback_for_planner="narrow the contract back to what was actually requested",
                feedback_for_verifier="fix the broken test double",
            ),
            {"input": 1, "output": 1},
        )

    def fake_calibrator(session_summary, memory):
        calls["calibrate"] += 1
        return CalibratorOutput(pattern="check exact output strings"), {"input": 1, "output": 1}

    def fake_test_run(project_path, timeout=60):
        calls["test_run"] += 1
        path = os.path.join(project_path, "src", "greeting.js")
        content = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        passed = "correct" in content
        return {
            "passed": passed,
            "returncode": 0 if passed else 1,
            "stdout": "",
            "stderr": "" if passed else "assertion failed",
            "structured": {},
        }

    monkeypatch.setattr(agents, "run_planner", fake_planner)
    monkeypatch.setattr(agents, "run_scope_check", fake_scope_check)
    monkeypatch.setattr(agents, "run_builder", fake_builder)
    monkeypatch.setattr(agents, "run_verifier", fake_verifier)
    monkeypatch.setattr(agents, "run_arbiter", fake_arbiter)
    monkeypatch.setattr(agents, "run_reviewer", fake_reviewer)
    monkeypatch.setattr(agents, "run_review_arbiter", fake_review_arbiter)
    monkeypatch.setattr(agents, "run_calibrator", fake_calibrator)
    monkeypatch.setattr(execution, "run_tests", fake_test_run)

    return {
        "current_files": received_current_files,
        "build_arbiter_feedback": received_build_arbiter_feedback,
        "build_previous_review": received_build_previous_review,
        "previous_reviews": received_previous_reviews,
        "verifier_feedback": received_verifier_feedback,
        "verifier_current_files": received_verifier_current_files,
    }


@pytest.fixture
def make_repo(isolated_dirs):
    """Pre-creates a project directory in the isolated workspace, so
    _resolve_project_path sees it as already-existing (no scaffolding)."""

    def _make(name):
        target = os.path.join(isolated_dirs["workspace"], name)
        os.makedirs(os.path.join(target, "src"), exist_ok=True)
        with open(os.path.join(target, "src", "greeting.js"), "w", encoding="utf-8") as f:
            f.write("// placeholder")
        return target

    return _make
