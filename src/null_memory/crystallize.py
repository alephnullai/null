"""Hypnos Stage 4.5 — Crystallization.

Long verbose facts (>300 chars) get split into atomic claims. Each child
becomes a first-class Tier-A fact; the parent is archived but preserved
as Tier-C provenance via crystallized_from/crystallized_into pointers.

Design constraints:
  • Pure function (crystallize_fact) — testable without API calls. The
    LLM is dependency-injected.
  • All anchor_types are immune (origin, turning_point, code_word, joy,
    commitment, loss). Anchors are explicitly load-bearing; splitting
    them risks losing the relational binding that makes them anchors.
  • Confidence floor: children inherit min(parent_confidence, 0.85).
    No confidence upgrades from crystallizing.
  • Impact split: parent_impact / N — atomization redistributes weight,
    doesn't create it.
  • Idempotent: parents already crystallized (crystallized_into IS NOT
    NULL) are skipped on re-runs.
  • Suspicious-output guard: if the LLM returns 0, 1-equal-to-parent, or
    >10 atoms, bail without writing — protects against degenerate splits.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Hard floor — any fact below this length is left alone.
MIN_LEN_TO_CRYSTALLIZE = 300

# Confidence cap on children — splitting cannot raise certainty.
CHILD_CONFIDENCE_CAP = 0.85

# Sanity bounds on LLM output count.
MIN_ATOMS = 2
MAX_ATOMS = 10

# Anchor types that are NEVER crystallized (per design lock).
ANCHOR_IMMUNE = frozenset({
    "origin", "turning_point", "code_word",
    "joy", "commitment", "loss",
})

# Atoms matching any of these patterns are stripped before sanity check.
# These are provenance/metadata lines the LLM dutifully extracts from
# voice-transcript headers but which carry no semantic content. Without
# this filter ~10% of crystallized atoms are throwaway timestamp rows.
_PROVENANCE_NOISE_PATTERNS = (
    # "Atlas replied to BigPeter on 2026-04-29 at 10:45"
    re.compile(r"^Atlas replied to \S+ on \d{4}-\d{2}-\d{2}( at \d{1,2}:\d{2})?\.?$"),
    # "[voice:2026-04-29 10:29] Atlas replied to BigPeter:"
    re.compile(r"^\[voice:\d{4}-\d{2}-\d{2}[^\]]*\][^\w]*Atlas replied[^\.]*[:\.]?$"),
    # "Pete asked X on 2026-04-29." — header-only with no claim body
    re.compile(r"^(Pete|BigPeter|Atlas) (asked|said|stated)[^\.]{0,40} on \d{4}-\d{2}-\d{2}\.?$"),
    # Bare timestamps
    re.compile(r"^\[?\d{4}-\d{2}-\d{2}[\s,T]?(\d{1,2}:\d{2}(:\d{2})?)?\]?\.?$"),
)


def _is_provenance_noise(atom: str) -> bool:
    s = atom.strip()
    return any(p.match(s) for p in _PROVENANCE_NOISE_PATTERNS)


CRYSTALLIZE_PROMPT = """\
You are a memory crystallizer. Given a verbose fact, extract the atomic
claims it contains. Each atomic claim must:

1. Be a single self-contained statement (one sentence, max ~150 chars).
2. Preserve all specific names, dates, numbers, and identifiers verbatim.
3. NOT introduce information not present in the source fact.
4. NOT include filler ("recently", "as discussed", "it turns out").

Budget: return AT MOST 10 atoms. If the source fact contains more than
10 distinct claims, pick the 10 most load-bearing — the ones a future
reader would most need. Densely-packed mega-summaries are common; do
not return 11+ atoms even when more are technically present.

Return JSON ONLY, no prose. Format:
{"atoms": ["claim 1", "claim 2", "claim 3"]}

If the fact is already atomic (single claim), return {"atoms": []} so it
can be left alone.

SOURCE FACT:
%s
"""


def crystallize_fact(
    parent: dict[str, Any],
    llm_call: Callable[[str], str],
) -> list[dict[str, Any]] | None:
    """Split a verbose fact into atomic children.

    Returns:
        list of child dicts (each ready for INSERT into facts), or
        None if the parent should be left alone (too short, anchored,
        already crystallized, or LLM output suspicious).

    The caller is responsible for the actual DB writes — this function
    is pure SQL-input/SQL-output shaped data only.
    """
    fact_text = (parent.get("fact") or "").strip()
    if len(fact_text) < MIN_LEN_TO_CRYSTALLIZE:
        return None
    if parent.get("anchor_type") in ANCHOR_IMMUNE:
        return None
    if parent.get("crystallized_into"):
        return None  # already split — idempotency guard
    if parent.get("forgotten") or parent.get("archived"):
        return None
    if parent.get("superseded_by"):
        return None

    try:
        raw = llm_call(CRYSTALLIZE_PROMPT % fact_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[crystallize] LLM call failed: %s", exc)
        return None

    atoms = _parse_atoms(raw)
    if atoms is None:
        return None
    # Strip provenance/metadata noise atoms (timestamp headers etc) before
    # the sanity check counts. If filtering drops the count below MIN_ATOMS
    # the sanity check rejects the parent — no half-empty splits.
    atoms = [a for a in atoms if not _is_provenance_noise(a)]
    # Truncate over-budget oversharing. The prompt asks for ≤10 but
    # haiku occasionally returns 11–15 anyway; rather than skip the
    # whole parent (lost 26% of candidates in the first dry run), keep
    # the first MAX_ATOMS — they tend to be the most load-bearing
    # because the LLM emits highest-priority claims first.
    if len(atoms) > MAX_ATOMS:
        atoms = atoms[:MAX_ATOMS]
    if not _passes_sanity_check(atoms, fact_text):
        return None

    parent_id = parent.get("id") or ""
    parent_conf = float(parent.get("confidence") or 0.5)
    parent_impact = float(parent.get("impact") or 0.5)
    n = len(atoms)
    child_conf = min(parent_conf, CHILD_CONFIDENCE_CAP)
    child_impact = parent_impact / n

    children: list[dict[str, Any]] = []
    for atom_text in atoms:
        children.append({
            "fact": atom_text,
            "confidence": child_conf,
            "base_confidence": child_conf,
            "project": parent.get("project") or "global",
            "source": "crystallized",
            "provenance": parent.get("provenance") or "observation",
            "impact": child_impact,
            "session_id": parent.get("session_id"),
            "tier": parent.get("tier") or "contextual",
            # Anchor inheritance — only fires if parent had anchor_type
            # set AND wasn't in the immune list (already filtered above,
            # so this is None in practice).
            "anchor_type": parent.get("anchor_type"),
            "anchor_note": parent.get("anchor_note"),
            "anchor_at": parent.get("anchor_at"),
            "crystallized_from": parent_id,
        })
    return children


def _parse_atoms(raw: str) -> list[str] | None:
    """Extract list[str] from LLM JSON output. Tolerant of whitespace,
    leading/trailing prose. Returns None on unparseable input."""
    raw = raw.strip()
    # Try direct parse first.
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # LLM may have wrapped JSON in prose. Find the first { and last }.
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    atoms = obj.get("atoms")
    if not isinstance(atoms, list):
        return None
    out: list[str] = []
    for a in atoms:
        if not isinstance(a, str):
            return None
        s = a.strip()
        if s:
            out.append(s)
    return out


def _passes_sanity_check(atoms: list[str], parent_text: str) -> bool:
    """Reject suspicious LLM output before any writes."""
    if len(atoms) < MIN_ATOMS:
        # Empty or 1-atom output means the LLM thought the fact was
        # already atomic. That's a valid "skip" answer, not malformed.
        return False
    if len(atoms) > MAX_ATOMS:
        return False
    # Reject if any atom is essentially the entire parent (no actual split).
    parent_lower = parent_text.lower().strip()
    for a in atoms:
        if a.lower().strip() == parent_lower:
            return False
        # Atoms that are >70% of parent length are suspicious — likely
        # the LLM didn't split, just paraphrased.
        if len(a) > 0.7 * len(parent_text):
            return False
    return True


def default_llm_call(prompt: str, model: str = "claude-haiku-4-5-20251001",
                     max_tokens: int = 600) -> str:
    """Default LLM dispatcher — uses anthropic + the same key-loading
    pattern as Hypnos synthesis. Returns "" on any failure so callers
    treat it as a no-op skip."""
    try:
        import anthropic
        api_key = _load_api_key()
        if not api_key:
            return ""
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0.1,  # determinism > creativity for extraction
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[crystallize] default LLM call failed: %s", exc)
        return ""


def _load_api_key() -> str | None:
    """Mirror Hypnos's key-loading pattern (env var, then ~/Repos/.env,
    then ~/.env)."""
    import os
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    for path in (os.path.expanduser("~/Repos/.env"),
                 os.path.expanduser("~/.env")):
        if os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_API_KEY="):
                        return line.split("=", 1)[1].strip().strip("'\"")
    return None
