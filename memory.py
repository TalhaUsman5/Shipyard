"""
Layer 4 (context/memory) of the harness.

AGENTS.md has two sections:
  - Domain Rules (Permanent)   - hand-edited, never touched by the factory
  - Recent Patterns (Rolling)  - appended to by the Calibrator after each
                                  successful run; capped so it can't grow
                                  unbounded and crowd out the context window
"""
import os

MEMORY_PATH = os.path.join(os.path.dirname(__file__), "AGENTS.md")
MAX_ROLLING_PATTERNS = 15

DEFAULT_MEMORY = """# Software Factory Memory

## Domain Rules (Permanent)
- Keep changes scoped to the files the contract names.
- Prefer explicit error handling over silent failures.

## Recent Patterns (Rolling)
"""


def load_memory() -> str:
    if not os.path.exists(MEMORY_PATH):
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_MEMORY)
    with open(MEMORY_PATH, "r", encoding="utf-8") as f:
        return f.read()


def append_pattern(pattern: str) -> None:
    content = load_memory()
    lines = content.splitlines()

    header = "## Recent Patterns (Rolling)"
    if header not in lines:
        lines.append(header)
    idx = lines.index(header)

    after = lines[idx + 1 :]
    bullets = [l for l in after if l.strip().startswith("-")]
    other_after = [l for l in after if not l.strip().startswith("-")]

    bullets.append(f"- {pattern}")
    bullets = bullets[-MAX_ROLLING_PATTERNS:]  # keep only the most recent N

    new_lines = lines[: idx + 1] + bullets + other_after
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")
