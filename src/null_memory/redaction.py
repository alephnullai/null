"""Broadcast redaction (P2-17).

Multiverse ``broadcast()`` replicates event text into every target
personality's database. Before that happens, this module strips strings
that look like credentials, plus deployment-specific shared secrets from
``identity_terms.core_terms``, so a stray API key pasted into one
instance doesn't get copied into all of them.

Design: a small, conservative pattern set — high-precision shapes
(key=value credentials, known token prefixes, PEM blocks) rather than
entropy heuristics, to avoid mangling ordinary prose. A custom
``classifier`` hook can veto a broadcast entirely.
"""

from __future__ import annotations

import re
from typing import Callable

REDACTED = "[REDACTED]"

# (label, pattern) — applied in order; each replaces matches with REDACTED.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    # PEM / private key blocks
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
        re.S)),
    # Known token prefixes (GitHub, OpenAI/Anthropic/Stripe-style sk-, Slack, AWS)
    ("api_token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|sk-[A-Za-z0-9_\-]{16,}"
        r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
        r"|AKIA[0-9A-Z]{16})\b")),
    # JWTs
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    # Bearer headers
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{16,}")),
    # key=value / key: value credential assignments
    ("credential_assignment", re.compile(
        r"(?i)\b(password|passwd|pass[ _-]?phrase|secret|token|api[_-]?key|"
        r"access[_-]?key|private[_-]?key|credentials?)\b"
        r"\s*(?:=|:|\bis\b)\s*"
        r"(['\"]?)[^\s'\"]{6,}\2")),
    # Connection-string passwords (postgres://user:pass@host)
    ("connection_string", re.compile(r"(?<=://)[^\s:/@]+:[^\s@]+(?=@)")),
]


def redact(
    text: str,
    identity_terms: dict | None = None,
) -> tuple[str, list[str]]:
    """Redact secret-shaped substrings from *text*.

    Returns ``(clean_text, labels)`` where *labels* names each pattern
    category that fired (deduplicated, in match order). Deployment
    ``identity_terms['core_terms']`` (shared secrets / code words) are
    redacted as well.
    """
    labels: list[str] = []

    def _note(label: str) -> None:
        if label not in labels:
            labels.append(label)

    clean = text
    for label, pattern in SECRET_PATTERNS:
        clean, n = pattern.subn(REDACTED, clean)
        if n:
            _note(label)

    core_terms = []
    if isinstance(identity_terms, dict):
        vals = identity_terms.get("core_terms") or []
        if isinstance(vals, str):
            vals = [vals]
        core_terms = [str(v).strip() for v in vals if str(v).strip()]
    for term in core_terms:
        words = [re.escape(w) for w in term.split()]
        pattern = re.compile(r"\b" + r"\s*".join(words) + r"\b", re.I)
        clean, n = pattern.subn(REDACTED, clean)
        if n:
            _note("identity_term")

    return clean, labels


# Optional veto hook: a callable str -> bool installed by the deployment.
# Returning True blocks the broadcast outright (e.g. an LLM-based
# sensitivity classifier). None means "never block".
BroadcastClassifier = Callable[[str], bool]
_broadcast_classifier: BroadcastClassifier | None = None


def set_broadcast_classifier(fn: BroadcastClassifier | None) -> None:
    global _broadcast_classifier
    _broadcast_classifier = fn


def should_block_broadcast(text: str) -> bool:
    if _broadcast_classifier is None:
        return False
    try:
        return bool(_broadcast_classifier(text))
    except Exception:
        # A broken classifier should fail closed: don't replicate.
        return True
