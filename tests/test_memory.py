"""
Unit tests for memory.py's rolling-pattern append logic, recurrence
tracking, source attribution, and promotion-candidate detection.

Regression coverage for a real bug found live: append_pattern had no
dedup, so a pattern the Calibrator re-derived across many runs piled up
as identical lines under "## Recent Patterns (Rolling)" — 14 duplicate
copies of the same pattern were found and manually cleaned out of the
real AGENTS.md. These tests pin down the fix: re-appending an existing
pattern increments its recurrence count and refreshes its recency instead
of duplicating it.
"""
import memory


def _rolling_bullets(content: str) -> list:
    lines = content.splitlines()
    idx = lines.index("## Recent Patterns (Rolling)")
    return [l for l in lines[idx + 1 :] if l.strip().startswith("-")]


def test_append_pattern_deduplicates_a_repeated_pattern(tmp_path, monkeypatch):
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    memory.append_pattern("check exact output strings")
    memory.append_pattern("check exact output strings")
    memory.append_pattern("check exact output strings")

    bullets = _rolling_bullets(memory.load_memory())
    assert len(bullets) == 1
    text, seen, _source = memory._split_meta(bullets[0])
    assert text == "check exact output strings"
    assert seen == 3


def test_append_pattern_refreshes_recency_of_a_repeated_pattern(tmp_path, monkeypatch):
    """A pattern seen again should move to the end (most recent), not stay
    stuck at its original position — otherwise it could get evicted by the
    MAX_ROLLING_PATTERNS cap ahead of patterns actually seen more recently."""
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    memory.append_pattern("pattern A")
    memory.append_pattern("pattern B")
    memory.append_pattern("pattern A")  # seen again — should move to the end

    bullets = _rolling_bullets(memory.load_memory())
    texts = [memory._split_meta(b)[0] for b in bullets]
    assert texts == ["pattern B", "pattern A"]


def test_append_pattern_still_caps_distinct_patterns_at_max_rolling(tmp_path, monkeypatch):
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    for i in range(memory.MAX_ROLLING_PATTERNS + 5):
        memory.append_pattern(f"distinct pattern {i}")

    bullets = _rolling_bullets(memory.load_memory())
    assert len(bullets) == memory.MAX_ROLLING_PATTERNS
    texts = [memory._split_meta(b)[0] for b in bullets]
    # the oldest ones should have been evicted, the newest kept
    assert texts[-1] == f"distinct pattern {memory.MAX_ROLLING_PATTERNS + 4}"
    assert "distinct pattern 0" not in texts


def test_append_pattern_leaves_domain_rules_section_untouched(tmp_path, monkeypatch):
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    memory.append_pattern("some pattern")

    content = memory.load_memory()
    assert "Keep changes scoped to the files the contract names." in content
    assert "Prefer explicit error handling over silent failures." in content


def test_append_pattern_records_and_updates_source(tmp_path, monkeypatch):
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    memory.append_pattern("a lesson", source="project-a")
    memory.append_pattern("a lesson", source="project-b")

    bullets = _rolling_bullets(memory.load_memory())
    text, seen, source = memory._split_meta(bullets[0])
    assert text == "a lesson"
    assert seen == 2
    assert source == "project-b", "source should reflect the MOST RECENT project it recurred in"


def test_promotion_candidates_only_returns_patterns_meeting_the_threshold(tmp_path, monkeypatch):
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    for _ in range(3):
        memory.append_pattern("recurring lesson", source="proj")
    memory.append_pattern("one-off lesson", source="proj")

    candidates = memory.promotion_candidates(min_seen=3)
    assert candidates == [{"pattern": "recurring lesson", "seen": 3, "last_source": "proj"}]


def test_promotion_candidates_is_empty_when_nothing_meets_the_threshold(tmp_path, monkeypatch):
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    memory.append_pattern("only seen once")

    assert memory.promotion_candidates(min_seen=3) == []


def test_promotion_candidates_never_touches_domain_rules(tmp_path, monkeypatch):
    """Promotion is detection-only — it must never write anything, to
    Domain Rules or otherwise."""
    memory_path = tmp_path / "AGENTS.md"
    monkeypatch.setattr(memory, "MEMORY_PATH", str(memory_path))

    before = memory.load_memory()
    memory.promotion_candidates()
    after = memory.load_memory()
    assert before == after
