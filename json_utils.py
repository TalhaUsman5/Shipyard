import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


def parse_json_response(text: str) -> dict:
    """Strip an optional ```json fence and parse. Raises ValueError with the
    raw text attached if the model didn't return valid JSON, so the caller
    can log exactly what came back instead of a bare traceback."""
    cleaned = text.strip()
    m = _FENCE_RE.match(cleaned)
    if m:
        cleaned = m.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON: {e}\n---raw---\n{text}") from e
