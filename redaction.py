"""
The secrets boundary. Every role's prompt funnels through llm_client.call()
before it reaches an inference provider — this module is invoked exactly
there, the one chokepoint every role (present and future) passes through
without having to individually remember to redact its own inputs. That's
what "enforced structurally, not by convention" means in practice: a new
agents.py role added tomorrow gets this for free just by calling
llm_client.call() like every other role already does.

Deliberately regex/pattern-based, not a full entropy-analysis secret
scanner. Two asymmetric failure costs, and this leans toward the cheaper
one: a false positive (a string that merely LOOKS like a credential) costs
the role a placeholder in place of that one string — usually harmless,
since the role rarely needs the literal value, just to know something
credential-shaped was there. A false negative (a real secret in a format
this doesn't recognize) leaves the process entirely. Given that asymmetry,
patterns here are intentionally a little trigger-happy rather than tightly
tuned to avoid false positives.

Policy: redact-and-continue, not block-the-run. A regex-based scanner will
have false positives (a test fixture's fake token, a placeholder value in
a README) at a rate too high to make blocking the whole run tolerable —
that would make the harness unusable on any project with realistic-looking
sample credentials in its docs or tests. Redaction preserves the pipeline's
ability to keep working while still ensuring the actual secret VALUE never
leaves this process. Set FACTORY_SECRET_POLICY=block in the environment to
raise instead of redact, for an operator who'd rather stop a run outright
than risk a false negative getting through unredacted.
"""
import os
import re
from dataclasses import dataclass
from typing import List, Tuple

# Each pattern's name is what gets logged/placed in the placeholder — never
# the matched text itself, so a redaction event in the trace can say WHAT
# kind of thing was found without repeating the secret it just removed.
_PATTERNS = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai_or_anthropic_key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")),
    ("credential_in_url", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@'\"]+:[^\s:/@'\"]+@")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*")),
]


@dataclass(frozen=True)
class Finding:
    pattern: str
    count: int


def scan(text: str) -> List[Finding]:
    """Reports what WOULD be redacted, without modifying anything — used
    wherever a caller wants to know/log findings separately from the
    redact() call that actually acts on them."""
    findings = []
    for name, pattern in _PATTERNS:
        n = len(pattern.findall(text))
        if n:
            findings.append(Finding(name, n))
    return findings


def redact(text: str) -> Tuple[str, List[Finding]]:
    """Returns (cleaned_text, findings). Each match is replaced with
    "[REDACTED:<pattern>]" — visible enough that a role reasoning over the
    text can tell something was removed (rather than silently seeing
    nothing, which could read as "no credential is used here" and produce
    wrong reasoning), but the placeholder never contains the actual value."""
    findings = []
    cleaned = text
    for name, pattern in _PATTERNS:
        cleaned, n = pattern.subn(f"[REDACTED:{name}]", cleaned)
        if n:
            findings.append(Finding(name, n))
    return cleaned, findings


def policy() -> str:
    """"redact" (default) or "block" — see module docstring."""
    value = (os.getenv("FACTORY_SECRET_POLICY") or "redact").strip().lower()
    return value if value in ("redact", "block") else "redact"


class SecretDetected(Exception):
    """Raised instead of redacting when FACTORY_SECRET_POLICY=block finds
    something. Carries `findings` so the caller can log what kind of
    pattern tripped this without needing to re-scan."""

    def __init__(self, findings: List[Finding]):
        self.findings = findings
        kinds = ", ".join(f"{f.pattern}x{f.count}" for f in findings)
        super().__init__(f"blocked: prompt contains credential-shaped content ({kinds})")
