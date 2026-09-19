"""
Layer 4 (context/memory) of the harness.

AGENTS.md has two sections:
  - Domain Rules (Permanent)   - hand-edited, never touched by the factory,
                                  and never auto-promoted into or retired
                                  from by anything in this file. A pattern
                                  earning its way in, or a rule that's since
                                  proven wrong, is a deliberate human edit —
                                  see promotion_candidates below for the
                                  detection half of that (surfacing what's
                                  worth considering), never the writing half.
  - Recent Patterns (Rolling)  - appended to by the Calibrator after each
                                  successful run; capped so it can't grow
                                  unbounded and crowd out the context window.
                                  Each bullet carries a machine-parseable
                                  "[meta: seen=N, source=<project>]" suffix:
                                  a pattern the Calibrator re-derives on a
                                  later run increments `seen` and updates
                                  `source` to the latest project it came
                                  from, rather than duplicating the line
                                  (observed live: 14 duplicate copies of one
                                  pattern before this existed) — this is
                                  also what lets promotion_candidates judge
                                  which patterns have actually recurred
                                  enough to be worth a human's attention,
                                  not just repeated the exact same run.
"""
import os
import re
from typing import Optional

MEMORY_PATH = os.path.join(os.path.dirname(__file__), "AGENTS.md")
MAX_ROLLING_PATTERNS = 15

DEFAULT_MEMORY = """# Software Factory Memory

## Domain Rules (Permanent)
- Keep changes scoped to the files the contract names.
- Prefer explicit error handling over silent failures.

## Recent Patterns (Rolling)
"""

_META_RE = re.compile(r" \[meta: seen=(\d+)(?:, source=([^\]]*))?\]$")


def load_memory() -> str:
    if not os.path.exists(MEMORY_PATH):
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_MEMORY)
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _split_meta(bullet_line: str):
    """'- pattern text [meta: seen=2, source=proj]' -> ('pattern text', 2, 'proj').
    A bullet with no meta suffix (hand-written, or from before this format
    existed) is treated as seen once, with no known source."""
    text = bullet_line[2:] if bullet_line.startswith("- ") else bullet_line
    m = _META_RE.search(text)
    if not m:
        return text, 1, None
    return text[: m.start()], int(m.group(1)), m.group(2)


def _format_bullet(pattern: str, seen: int, source: Optional[str]) -> str:
    meta = [f"seen={seen}"]
    if source:
        meta.append(f"source={source}")
    return f"- {pattern} [meta: {', '.join(meta)}]"


def append_pattern(pattern: str, source: Optional[str] = None) -> None:
    """`source` is typically the target project_path the pattern was
    learned from — recorded so a future reader (human or Planner) can
    judge relevance instead of every project seeing every pattern with no
    idea where it came from."""
    content = load_memory()
    lines = content.splitlines()

    header = "## Recent Patterns (Rolling)"
    if header not in lines:
        lines.append(header)
    idx = lines.index(header)

    after = lines[idx + 1 :]
    bullets = [l for l in after if l.strip().startswith("-")]
    other_after = [l for l in after if not l.strip().startswith("-")]

    existing_index = None
    seen = 1
    for i, b in enumerate(bullets):
        text, count, _source = _split_meta(b)
        if text == pattern:
            existing_index = i
            seen = count + 1
            break
    if existing_index is not None:
        bullets.pop(existing_index)

    bullets.append(_format_bullet(pattern, seen, source))
    bullets = bullets[-MAX_ROLLING_PATTERNS:]  # keep only the most recent N

    new_lines = lines[: idx + 1] + bullets + other_after
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")


def promotion_candidates(min_seen: int = 3) -> list:
    """Rolling patterns that have recurred at least `min_seen` times —
    surfaced as candidates worth a human copying into Domain Rules
    (Permanent). Deliberately read-only: this never writes to Domain
    Rules itself, and there is no code path that does — promoting a
    pattern, or retiring a Domain Rule that's since proven wrong, stays a
    deliberate manual edit to AGENTS.md, same as it always has been."""
    content = load_memory()
    lines = content.splitlines()
    header = "## Recent Patterns (Rolling)"
    if header not in lines:
        return []
    idx = lines.index(header)
    bullets = [l for l in lines[idx + 1 :] if l.strip().startswith("-")]

    candidates = []
    for b in bullets:
        text, count, source = _split_meta(b)
        if count >= min_seen:
            candidates.append({"pattern": text, "seen": count, "last_source": source})
    return candidates
