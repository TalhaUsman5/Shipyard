"""
Unit tests for context.py's truncation visibility (project_snapshot's
max_files cap, priority_paths ordering, describe_snapshot's honest
labeling) and execution.py's read_text_files omitted-count reporting.
Regression coverage for a real gap: both caps used to truncate silently,
with no signal to the model that anything was cut, and the cut followed
directory-walk order rather than contract relevance.
"""
import os

from context import describe_snapshot, project_snapshot
from execution import read_text_files


def test_project_snapshot_reports_truncated_false_when_nothing_is_cut(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.js").write_text("x")
    files, truncated = project_snapshot(str(tmp_path), max_files=200)
    assert len(files) == 5
    assert truncated is False


def test_project_snapshot_reports_truncated_true_when_the_cap_is_hit(tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.js").write_text("x")
    files, truncated = project_snapshot(str(tmp_path), max_files=5)
    assert len(files) == 5
    assert truncated is True


def test_project_snapshot_prioritizes_given_paths_ahead_of_the_cap(tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.js").write_text("x")
    important = "f9.js"  # would be dropped by a plain walk-order cap of 3
    files, truncated = project_snapshot(str(tmp_path), max_files=3, priority_paths=[important])
    assert important in files
    assert truncated is True


def test_describe_snapshot_labels_a_complete_listing_honestly():
    text = describe_snapshot(["a.js", "b.js"], truncated=False)
    assert "(complete)" in text
    assert "truncated" not in text.lower()


def test_describe_snapshot_names_a_walk_level_truncation():
    text = describe_snapshot(["a.js"], truncated=True)
    assert "1 files" in text or "stopped at 1" in text


def test_describe_snapshot_names_its_own_slice_truncation():
    files = [f"f{i}.js" for i in range(150)]
    text = describe_snapshot(files, truncated=False, shown_limit=100)
    assert "50 more collected file path(s) not shown" in text


def test_read_text_files_reports_zero_omitted_when_everything_fits(tmp_path):
    (tmp_path / "a.js").write_text("small")
    files, omitted = read_text_files(str(tmp_path), ["a.js"], max_total_bytes=1000)
    assert len(files) == 1
    assert omitted == 0


def test_read_text_files_reports_omitted_count_when_the_byte_cap_is_hit(tmp_path):
    (tmp_path / "a.js").write_text("x" * 100)
    (tmp_path / "b.js").write_text("y" * 100)
    (tmp_path / "c.js").write_text("z" * 100)
    files, omitted = read_text_files(str(tmp_path), ["a.js", "b.js", "c.js"], max_total_bytes=150)
    assert len(files) == 1
    assert omitted == 2
