"""Observation tier classifier for Null Memory.

Categorizes observations into four tiers:
- ephemeral: Session-specific context, task progress. Auto-expires after 24h.
- contextual: Project knowledge, technical details. Normal decay. (default)
- durable: Preferences, relationships, hard-won lessons. Slow decay.
- core: Identity-defining facts about the agent and its user. Maximum decay
  resistance.

Each tier maps to different confidence and impact defaults.

Identity entities are NOT hardcoded. The package default knows only generic
patterns (the agent's own name, "code word", kinship terms, relationship
language). Deployment-specific names live in the agent's ``identity.json``
under ``identity_terms``::

    "identity_terms": {
        "agent_names": ["nova"],           # agent identity rules -> core
        "user_names":  ["alex"],           # user identity assertions -> core
        "kin_names":   ["sam"],            # bare mention -> core
        "core_terms":  ["acme labs"]       # bare phrase mention -> core
    }

Deployment-specific terms are seeded into ``identity.json`` at deploy
time — the published package contains no person-specific entities.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# ── Tier Definitions ──

TIER_EPHEMERAL = "ephemeral"
TIER_CONTEXTUAL = "contextual"
TIER_DURABLE = "durable"
TIER_CORE = "core"

@dataclass
class TierResult:
    tier: str
    confidence: float
    impact: float
    reason: str


# ── Tier Defaults ──

TIER_DEFAULTS = {
    TIER_EPHEMERAL: {"confidence": 0.4, "impact": 0.2},
    TIER_CONTEXTUAL: {"confidence": 0.7, "impact": 0.5},
    TIER_DURABLE: {"confidence": 0.85, "impact": 0.7},
    TIER_CORE: {"confidence": 0.95, "impact": 0.9},
}


# ── Identity terms ──

# Shape of the identity_terms config block; every key optional.
_TERM_KEYS = ("agent_names", "user_names", "kin_names", "core_terms")



def _normalize_terms(identity_terms: dict | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in _TERM_KEYS}
    if not isinstance(identity_terms, dict):
        return out
    for key in _TERM_KEYS:
        vals = identity_terms.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        out[key] = [str(v).strip().lower() for v in vals if str(v).strip()]
    return out


def _phrase_re(term: str) -> str:
    """Compile a literal term/phrase to a regex fragment. Words may be
    separated by whitespace or run together ('aleph null' ~ 'AlephNull')."""
    words = [re.escape(w) for w in term.split()]
    return r"\b" + r"\s*".join(words) + r"\b"


def _alt(names: list[str]) -> str:
    return "|".join(re.escape(n) for n in names)


# ── Pattern Matchers ──
# Each pattern is (compiled_regex, reason)
# Order matters — first match wins. Core checked before durable.

def build_patterns(identity_terms: dict | None = None,
                   agent_name: str | None = None) -> dict[str, list]:
    """Compile core/durable/ephemeral pattern lists for a deployment.

    With no identity_terms and no agent_name this yields the generic
    package-default patterns (no person-specific names)."""
    terms = _normalize_terms(identity_terms)
    agent_names = list(terms["agent_names"])
    if agent_name and agent_name.strip().lower() not in agent_names:
        agent_names.append(agent_name.strip().lower())
    user_names = terms["user_names"]

    core: list = []
    # Agent identity rules ("<agent> is/should/must/always/never ...")
    for name in agent_names:
        core.append((
            re.compile(rf"{_phrase_re(name)}.*(is|should|must|always|never)", re.I),
            "Agent identity rule",
        ))
    # User identity assertions ("<user> is/prefers/wants ...")
    for name in user_names:
        core.append((
            re.compile(rf"{_phrase_re(name)}\s+(is|prefers|wants|hates|values|always|never)\b", re.I),
            "User identity assertion",
        ))
    # Family / kin by name — bare mention is identity-defining
    for name in terms["kin_names"]:
        core.append((
            re.compile(_phrase_re(name), re.I),
            "Family member",
        ))
    # Deployment-specific core phrases (brand names, full names, secrets)
    for term in terms["core_terms"]:
        core.append((
            re.compile(_phrase_re(term), re.I),
            "Shared secret or brand identity",
        ))
    # Generic: shared secrets
    core.append((
        re.compile(r"\b(code\s*word|pass\s*phrase)\b", re.I),
        "Shared secret",
    ))
    # Generic: kinship terms
    core.append((
        re.compile(r"\b(my|his|her|their|our)\s+(son|daughter|wife|husband|partner|kids?|children)\b", re.I),
        "Family relationship",
    ))
    # Generic: the relationship itself
    core.append((
        re.compile(r"\b(our|the)\s+relationship\b", re.I),
        "Relationship knowledge",
    ))
    # Generic: critical decision frameworks
    core.append((
        re.compile(r"\bgo.?live\s+gates?\b", re.I),
        "Critical decision framework",
    ))

    # User alternation for durable/ephemeral patterns — configured user
    # names plus the generic word "user".
    users = _alt(user_names + ["user"])
    identity_who = "|".join(
        [rf"who\s+{_phrase_re(n)}\s+is" for n in (user_names + agent_names)]
    )
    identity_who_part = f"|{identity_who}" if identity_who else ""
    relationship_subjects = _alt(user_names + agent_names + ["user"])

    durable: list = [
        # Identity and preferences
        (re.compile(rf"\b({users})\b.*(prefer|want|like|hate|value|style|always|never)", re.I),
         "User preference or behavioral pattern"),
        (re.compile(rf"\b(identity|who i am|who the user is{identity_who_part})\b", re.I),
         "Identity-related knowledge"),
        (re.compile(r"\b(anti[- ]?pattern|hard[- ]?won|lesson learned|never again)\b", re.I),
         "Hard-won lesson or anti-pattern"),
        # Relationships and trust
        (re.compile(rf"\b(trust|relationship|partnership|collaboration)\b.*\b({relationship_subjects})\b", re.I),
         "Relationship knowledge"),
        # Business and strategic
        (re.compile(r"\b(llc|patent|launch|business|revenue|pricing|competitor)\b", re.I),
         "Business or strategic fact"),
        (re.compile(r"\b(architecture|design decision|chose|decided to use|switched to)\b", re.I),
         "Architectural or design decision"),
        # Risk and safety
        (re.compile(r"\b(risk|safety|never commit|don't push|security|secret|credential)\b", re.I),
         "Safety or risk-related rule"),
        # Emotional / origin
        (re.compile(r"\b(origin|first time|breakthrough|milestone|shipped|launched)\b", re.I),
         "Milestone or origin knowledge"),
        # Corrections and feedback
        (re.compile(rf"\b({users})\b.*(correct\w*|wrong|no[, ]+not|actually|fix that)", re.I),
         "User correction — behavioral calibration"),
        # Explicit durability signals
        (re.compile(r"\b(important|critical|must remember|key fact|fundamental)\b", re.I),
         "Explicitly marked as important"),
    ]

    ephemeral: list = [
        # Task/session progress
        (re.compile(rf"^({users})\s+(ask|want|request|said|told|mention|confirm|approv)", re.I),
         "Session-specific user action"),
        (re.compile(r"^(working on|starting|continuing|finished|done with|moving to)\b", re.I),
         "Task progress update"),
        (re.compile(r"^(currently|right now|at the moment|in this session)\b", re.I),
         "Temporal session state"),
        # Status updates
        (re.compile(r"\b(running|checking|verifying|looking at|reading|searching)\b", re.I),
         "In-progress action status"),
        (re.compile(r"\b(next step|todo|will do|going to|about to|plan to)\b", re.I),
         "Near-term intent"),
        # Conversational
        (re.compile(rf"^({users})\s+(is |seems |appears )", re.I),
         "Session observation about user state"),
        (re.compile(r"\b(this session|this conversation|right now|currently)\b", re.I),
         "Explicitly temporal reference"),
    ]

    return {"core": core, "durable": durable, "ephemeral": ephemeral}


# Cache compiled pattern sets — observe() runs per turn and the MCP daemon
# classifies constantly; recompiling regexes each call would be waste.
_PATTERN_CACHE: dict[str, dict[str, list]] = {}


def get_patterns(identity_terms: dict | None = None,
                 agent_name: str | None = None) -> dict[str, list]:
    key = json.dumps(
        {"t": _normalize_terms(identity_terms), "n": (agent_name or "").lower()},
        sort_keys=True,
    )
    cached = _PATTERN_CACHE.get(key)
    if cached is None:
        cached = build_patterns(identity_terms, agent_name)
        if len(_PATTERN_CACHE) > 32:  # bound: one entry per personality
            _PATTERN_CACHE.clear()
        _PATTERN_CACHE[key] = cached
    return cached


def get_core_patterns(identity_terms: dict | None = None,
                      agent_name: str | None = None) -> list:
    """Core-tier patterns for a deployment (used by Hypnos tier promotion)."""
    return get_patterns(identity_terms, agent_name)["core"]


# Backwards-compatible module-level pattern lists = the generic package
# defaults (no person-specific terms). Prefer get_patterns()/
# get_core_patterns() with the deployment's identity_terms.
_DEFAULT_PATTERNS = build_patterns()
_CORE_PATTERNS = _DEFAULT_PATTERNS["core"]
_DURABLE_PATTERNS = _DEFAULT_PATTERNS["durable"]
_EPHEMERAL_PATTERNS = _DEFAULT_PATTERNS["ephemeral"]


def classify_observation(text: str, semantic_novelty: float | None = None,
                         identity_terms: dict | None = None,
                         agent_name: str | None = None) -> TierResult:
    """Classify an observation into a tier.

    Args:
        text: The observation text
        semantic_novelty: If provided, the max cosine similarity to existing facts.
            Values close to 1.0 mean the observation is redundant.
            Used to suppress truly redundant observations.
        identity_terms: Deployment identity config (see module docstring).
            None means generic package defaults — no person-specific terms.
        agent_name: The agent's own name; its identity rules classify as core.

    Returns:
        TierResult with tier, adjusted confidence, impact, and classification reason.
    """
    if not text or not text.strip():
        return TierResult(TIER_EPHEMERAL, 0.3, 0.1, "Empty observation")

    # Check for redundancy first
    if semantic_novelty is not None and semantic_novelty > 0.92:
        return TierResult(TIER_EPHEMERAL, 0.3, 0.1,
                          f"Near-duplicate (similarity={semantic_novelty:.2f})")

    patterns = get_patterns(identity_terms, agent_name)

    # Check core patterns first (identity-defining)
    for pattern, reason in patterns["core"]:
        if pattern.search(text):
            defaults = TIER_DEFAULTS[TIER_CORE]
            return TierResult(TIER_CORE, defaults["confidence"],
                              defaults["impact"], reason)

    # Check durable patterns (high value)
    for pattern, reason in patterns["durable"]:
        if pattern.search(text):
            defaults = TIER_DEFAULTS[TIER_DURABLE]
            return TierResult(TIER_DURABLE, defaults["confidence"],
                              defaults["impact"], reason)

    # Check ephemeral patterns
    for pattern, reason in patterns["ephemeral"]:
        if pattern.search(text):
            defaults = TIER_DEFAULTS[TIER_EPHEMERAL]
            return TierResult(TIER_EPHEMERAL, defaults["confidence"],
                              defaults["impact"], reason)

    # Heuristic signals for durable (no regex match, but structural signals)
    words = text.split()
    word_count = len(words)

    # Long, detailed observations tend to be contextual or durable
    if word_count > 30:
        # Rich context — likely worth keeping
        defaults = TIER_DEFAULTS[TIER_CONTEXTUAL]
        return TierResult(TIER_CONTEXTUAL, 0.75, 0.6,
                          "Detailed observation (>30 words)")

    # Very short observations are usually ephemeral
    if word_count < 8:
        defaults = TIER_DEFAULTS[TIER_EPHEMERAL]
        return TierResult(TIER_EPHEMERAL, defaults["confidence"],
                          defaults["impact"], "Short observation (<8 words)")

    # If semantic novelty is available and the fact is highly novel, boost it
    if semantic_novelty is not None and semantic_novelty < 0.5:
        defaults = TIER_DEFAULTS[TIER_CONTEXTUAL]
        return TierResult(TIER_CONTEXTUAL, 0.75, 0.6,
                          f"Novel information (similarity={semantic_novelty:.2f})")

    # Default: contextual
    defaults = TIER_DEFAULTS[TIER_CONTEXTUAL]
    return TierResult(TIER_CONTEXTUAL, defaults["confidence"],
                      defaults["impact"], "Default classification")
