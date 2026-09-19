"""
Unit tests for prompts.py — every role's system prompt now lives as its
own file under prompts/, loaded fresh on every call rather than baked in
as a module-level constant, so an edit takes effect without a restart.
"""
import pytest

import prompts

ROLE_NAMES = ["planner", "builder", "verifier", "reviewer", "arbiter", "review_arbiter", "calibrator"]


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_every_role_prompt_file_exists_and_loads(name):
    text = prompts.load(name)
    assert isinstance(text, str)
    assert len(text) > 0


@pytest.mark.parametrize("name", ROLE_NAMES)
def test_every_role_prompt_asks_for_json_only(name):
    """A loose sanity check that the extraction didn't mangle content —
    every one of these prompts instructs JSON-only output somewhere."""
    text = prompts.load(name).lower()
    assert "json object" in text


def test_load_reads_fresh_off_disk_every_call(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", str(tmp_path))
    path = tmp_path / "sample.md"
    path.write_text("version one")
    assert prompts.load("sample") == "version one"

    path.write_text("version two")
    assert prompts.load("sample") == "version two", "must not cache — an edit takes effect on the next call"


def test_load_raises_loudly_for_an_unknown_role():
    with pytest.raises(FileNotFoundError):
        prompts.load("not_a_real_role")
