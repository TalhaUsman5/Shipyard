"""
Unit tests for execution.py's TAP parser and directory-replacing file
writer.

The TAP parser is validated against real `node --test --test-reporter=tap`
output (captured once, embedded below), not a hand-guessed format. Node's
diagnostic YAML block closes with "..." (not a second "---"), which a
first draft of this parser got wrong; these tests pin that down.
"""
import json
import os

import execution
from execution import _parse_tap, _uses_node_test_runner, apply_files_replacing_directory
from schemas import FileEdit

REAL_TAP_OUTPUT = """TAP version 13
# Subtest: passes
ok 1 - passes
  ---
  duration_ms: 1.4983
  type: 'test'
  ...
# Subtest: assertion fails
not ok 2 - assertion fails
  ---
  duration_ms: 1.571
  type: 'test'
  location: 'sample.test.js:4:1'
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly equal:

    1 !== 2

  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  expected: 2
  actual: 1
  operator: 'strictEqual'
  stack: |-
    TestContext.<anonymous> (sample.test.js:4:40)
  ...
# Subtest: throws something else
not ok 3 - throws something else
  ---
  duration_ms: 0.3127
  type: 'test'
  location: 'sample.test.js:5:1'
  failureType: 'testCodeFailure'
  error: 'boom'
  code: 'ERR_TEST_FAILURE'
  name: 'TypeError'
  stack: |-
    TestContext.<anonymous> (sample.test.js:5:45)
  ...
1..3
# tests 3
# suites 0
# pass 1
# fail 2
# cancelled 0
# skipped 0
# todo 0
# duration_ms 143.8667
"""


def test_parse_tap_extracts_summary_counts():
    result = _parse_tap(REAL_TAP_OUTPUT)
    assert result["total"] == 3
    assert result["passed"] == 1
    assert result["failed"] == 2


def test_parse_tap_only_reports_failed_tests():
    result = _parse_tap(REAL_TAP_OUTPUT)
    names = [f["name"] for f in result["failures"]]
    assert names == ["assertion fails", "throws something else"]


def test_parse_tap_flags_a_real_assertion_failure():
    result = _parse_tap(REAL_TAP_OUTPUT)
    assertion_failure = result["failures"][0]
    assert assertion_failure["code"] == "ERR_ASSERTION"
    assert assertion_failure["error_name"] == "AssertionError"
    assert assertion_failure["is_assertion_failure"] is True


def test_parse_tap_flags_a_non_assertion_exception_as_not_an_assertion_failure():
    result = _parse_tap(REAL_TAP_OUTPUT)
    other_failure = result["failures"][1]
    assert other_failure["code"] == "ERR_TEST_FAILURE"
    assert other_failure["error_name"] == "TypeError"
    assert other_failure["is_assertion_failure"] is False


def test_parse_tap_block_boundaries_dont_bleed_into_the_next_test():
    """Regression test for the bug a first draft had: treating a second
    "---" as the block terminator (it isn't — "..." is) meant the parser
    ran away and swallowed everything after the first failure, including
    later tests, into one "detail" blob."""
    result = _parse_tap(REAL_TAP_OUTPUT)
    assert len(result["failures"]) == 2
    first_detail = result["failures"][0]["detail"]
    assert "throws something else" not in first_detail
    assert "ERR_TEST_FAILURE" not in first_detail


def test_parse_tap_returns_empty_dict_for_non_tap_output():
    assert _parse_tap("") == {}
    assert _parse_tap("some random npm error output\nnot TAP at all") == {}


def test_apply_files_replacing_directory_deletes_stale_files_not_in_the_new_set(tmp_path):
    """Regression test for the bug this was built to fix: a retry that
    picks a new filename instead of overwriting its prior attempt used to
    leave the old file behind — and `node --test` discovers every
    test-shaped file on disk, so every abandoned generation kept running
    forever alongside the new one."""
    project_root = str(tmp_path)
    os.makedirs(os.path.join(project_root, "test"))
    with open(os.path.join(project_root, "test", "old_attempt.test.js"), "w", encoding="utf-8") as f:
        f.write("// stale, from an earlier retry")
    with open(os.path.join(project_root, "test", "helper.cjs"), "w", encoding="utf-8") as f:
        f.write("// also stale")

    written = apply_files_replacing_directory(
        project_root, "test", [FileEdit(path="test/new_attempt.test.js", content="// current")]
    )

    remaining = sorted(os.listdir(os.path.join(project_root, "test")))
    assert remaining == ["new_attempt.test.js"]
    assert written == ["test/new_attempt.test.js"]


def test_apply_files_replacing_directory_leaves_files_outside_the_directory_alone(tmp_path):
    project_root = str(tmp_path)
    os.makedirs(os.path.join(project_root, "src"))
    with open(os.path.join(project_root, "src", "app.js"), "w", encoding="utf-8") as f:
        f.write("// implementation, not touched by this call")

    apply_files_replacing_directory(project_root, "test", [FileEdit(path="test/a.test.js", content="// a")])

    assert os.path.isfile(os.path.join(project_root, "src", "app.js"))


def _write_package_json(project_root, test_script):
    with open(os.path.join(project_root, "package.json"), "w", encoding="utf-8") as f:
        json.dump({"scripts": {"test": test_script}}, f)


def test_uses_node_test_runner_true_for_a_plain_node_test_script(tmp_path):
    _write_package_json(str(tmp_path), "node --test")
    assert _uses_node_test_runner(str(tmp_path)) is True


def test_uses_node_test_runner_false_for_a_different_test_runner(tmp_path):
    _write_package_json(str(tmp_path), "jest")
    assert _uses_node_test_runner(str(tmp_path)) is False


def test_uses_node_test_runner_defaults_true_when_package_json_is_missing(tmp_path):
    assert _uses_node_test_runner(str(tmp_path)) is True


def test_uses_node_test_runner_defaults_true_when_package_json_is_malformed(tmp_path):
    with open(os.path.join(tmp_path, "package.json"), "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert _uses_node_test_runner(str(tmp_path)) is True


def test_run_tests_skips_the_structured_rerun_for_a_non_node_test_project(tmp_path, monkeypatch):
    """The real point of _uses_node_test_runner: run_tests must not pay
    for the second full subprocess call when the project clearly isn't
    node --test — confirmed by monkeypatching _try_structured_failure_detail
    to explode if it's ever called."""
    project_root = str(tmp_path)
    _write_package_json(project_root, "jest")

    def _run_npm_stub(args, root, timeout, cancel_event=None):
        return 1, "", "some failure", False  # a failing primary run

    def _boom(*args, **kwargs):
        raise AssertionError("_try_structured_failure_detail must not run for a non-node-test project")

    monkeypatch.setattr(execution.shutil, "which", lambda name: "npm")
    monkeypatch.setattr(execution, "_run_npm", _run_npm_stub)
    monkeypatch.setattr(execution, "_try_structured_failure_detail", _boom)

    result = execution.run_tests(project_root)
    assert result["passed"] is False
    assert result["structured"] == {}
