"""
System prompts for every LLM role, as standalone files under prompts/ —
not hardcoded triple-quoted strings in agents.py. Two things this buys:

1. Iterating on a prompt's wording is a text-file edit, reviewable in a
   diff on its own (no Python noise around it) and git-blamable per line —
   not a change buried inside a much larger agents.py.
2. load() reads fresh off disk on every call, the same philosophy
   factory.py already uses for dashboard.html (see its docstring) — a
   prompt edit takes effect on the NEXT call, no restart needed, rather
   than being baked in at import time the way a module-level constant
   would be.

Deliberately not a bigger versioning/A-B-testing system: extraction to
files plus this loader is the whole fix for "iterating costs a code
change + redeploy" — nothing here justifies more machinery than that.
"""
import os

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def load(name: str) -> str:
    """Reads prompts/<name>.md fresh off disk every call. Raises
    FileNotFoundError with the path if `name` doesn't exist — a typo'd
    role name fails loudly here, not as a confusing empty system prompt
    sent to the model."""
    path = os.path.join(_PROMPTS_DIR, f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read().rstrip("\n")
