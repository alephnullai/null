"""Pure maintenance primitives shared by all of Null's maintenance engines.

The schedulers — ``hypnos.Hypnos`` (batch sleep stages),
``hypnos_live.HypnosLiveWorker`` (60s ticks), and AgentMemory's own
``gc()`` / ``consolidate()`` passes — decide WHEN to run and apply the
results to the DB. The functions in this module decide WHAT qualifies:
they are pure (no DB handles, no clock reads — callers pass ``now``),
so they can be unit-tested exhaustively and can never diverge between
engines again.

ONE similarity definition
=========================
Duplicate detection uses embedding cosine similarity when vectors are
available, word-level Jaccard as the fallback:

  * ``MERGE_COSINE_THRESHOLD = 0.85`` — on the MiniLM-class embeddings
    Null uses, ≥0.85 cosine is paraphrase-level: the two facts assert
    the same thing in different words. Below that, facts are merely
    *related* and must not be auto-merged.
  * ``MERGE_JACCARD_THRESHOLD = 0.65`` — the lexical bar that has
    empirically tracked the 0.85-cosine semantic bar: two facts sharing
    ≥65% of their vocabulary are restatements, not neighbors. Used only
    when no embedding is available for the pair.

History: hypnos_live used cos≥0.85, gc dedup used Jaccard≥0.65, and
consolidate() merged a 0.40–0.65 Jaccard *band*. The first two were the
same duplicate intuition expressed in two metrics — they are now the
single definition above. The consolidate band is intentionally NOT a
duplicate test: it group-merges related restatements just below the
duplicate bar (everything at/above the bar is already handled by the
dedup pass), so ``find_band_merge_groups`` takes the band explicitly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# ── Canonical thresholds ──────────────────────────────────────────────────

# The single duplicate-merge threshold (see module docstring for rationale).
MERGE_COSINE_THRESHOLD = 0.85
MERGE_JACCARD_THRESHOLD = 0.65

# Live-demote eligibility: a fact may be archived by the live worker only
# when its confidence has collapsed AND it hasn't been touched in ~2 months.
DEMOTE_CONFIDENCE_MAX = 0.10
DEMOTE_AGE_DAYS_MIN = 60


# ── Similarity ────────────────────────────────────────────────────────────

def jaccard_words(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two strings."""
    set_a = set(a.lower().split())
    set_b = set(b.lower().split())
    union = set_a | set_b
    return len(set_a & set_b) / len(union) if union else 0.0


def similarity(text_a: str, text_b: str,
               vec_a=None, vec_b=None,
               cosine_fn=None) -> tuple[float, str]:
    """The ONE similarity definition: embedding cosine when both vectors
    are available, Jaccard fallback otherwise.

    Returns (score, method) where method is "cosine" or "jaccard".
    ``cosine_fn(vec_a, vec_b) -> float`` is injected by the caller so this
    module stays dependency-free (numpy lives with the embedding engine).
    """
    if vec_a is not None and vec_b is not None and cosine_fn is not None:
        return float(cosine_fn(vec_a, vec_b)), "cosine"
    return jaccard_words(text_a, text_b), "jaccard"


def merge_threshold(method: str) -> float:
    """Duplicate threshold for the given similarity method."""
    return MERGE_COSINE_THRESHOLD if method == "cosine" else MERGE_JACCARD_THRESHOLD


# ── Merge pairs (duplicate detection) ─────────────────────────────────────

def pick_merge_winner(a: dict, b: dict) -> tuple[dict, dict]:
    """Decide which of two duplicate facts survives a merge.

    Higher confidence wins; on a tie the older (more established,
    earlier ``created_at``) fact wins; final tie keeps ``a``.
    Returns (winner, loser).
    """
    conf_a = a.get("confidence") or 0
    conf_b = b.get("confidence") or 0
    if conf_b > conf_a:
        return b, a
    if conf_a > conf_b:
        return a, b
    if (b.get("created_at") or "") < (a.get("created_at") or ""):
        return b, a
    return a, b


def merge_decision(a: dict, b: dict, score: float,
                   method: str = "cosine",
                   threshold: float | None = None) -> tuple[dict, dict] | None:
    """Single-pair merge decision used by the live worker.

    Returns (winner, loser) when the pair qualifies as a duplicate, else
    None. Anchored facts are untouchable on either side.
    """
    if a.get("anchor_type") or b.get("anchor_type"):
        return None
    if threshold is None:
        threshold = merge_threshold(method)
    if score < threshold:
        return None
    return pick_merge_winner(a, b)


def find_merge_pairs(facts: list[dict],
                     vectors: dict | None = None,
                     cosine_fn=None,
                     threshold: float | None = None,
                     min_words: int = 3) -> list[tuple[dict, dict, float]]:
    """Batch duplicate detection over a fact list (greedy, O(n²)).

    Embedding cosine is used for a pair when both facts have a vector in
    ``vectors`` (id -> vec) and ``cosine_fn`` is provided; otherwise the
    pair falls back to Jaccard. Same-project pairs only; facts shorter
    than ``min_words`` words and anchored facts are skipped.

    Returns ordered [(winner, loser, score), ...]. Greedy semantics match
    the historical gc dedup pass: once a fact loses a merge it can't
    participate again, and if fact i itself loses, scanning for i stops.
    """
    if len(facts) < 2:
        return []
    vectors = vectors or {}
    pairs: list[tuple[dict, dict, float]] = []
    removed: set[str] = set()

    for i in range(len(facts)):
        if facts[i]["id"] in removed:
            continue
        if facts[i].get("anchor_type"):
            continue
        text_i = facts[i].get("fact", "")
        if len(text_i.split()) < min_words:
            continue
        proj_i = facts[i].get("project", "global")

        for j in range(i + 1, len(facts)):
            if facts[j]["id"] in removed:
                continue
            if facts[j].get("anchor_type"):
                continue
            if facts[j].get("project", "global") != proj_i:
                continue
            text_j = facts[j].get("fact", "")
            if len(text_j.split()) < min_words:
                continue

            score, method = similarity(
                text_i, text_j,
                vectors.get(facts[i]["id"]), vectors.get(facts[j]["id"]),
                cosine_fn,
            )
            pair_threshold = threshold if threshold is not None else merge_threshold(method)
            if score < pair_threshold:
                continue

            winner, loser = pick_merge_winner(facts[i], facts[j])
            pairs.append((winner, loser, score))
            removed.add(loser["id"])
            if loser is facts[i]:
                break  # fact i lost — stop scanning for it

    return pairs


def find_band_merge_groups(facts: list[dict],
                           eff_conf: dict[str, float],
                           low: float, high: float,
                           min_words: int = 5) -> list[tuple[dict, list[dict]]]:
    """Consolidation pass: group facts whose Jaccard similarity falls in
    the [low, high) band and pick the highest-effective-confidence member
    as the survivor.

    NOT duplicate detection — see module docstring. Everything at/above
    ``high`` (= MERGE_JACCARD_THRESHOLD) belongs to find_merge_pairs.

    ``eff_conf`` maps fact id -> effective confidence (caller computes it
    so this stays pure). Returns [(winner, [losers...]), ...].
    """
    groups: list[tuple[dict, list[dict]]] = []
    removed: set[str] = set()

    for i in range(len(facts)):
        if facts[i]["id"] in removed:
            continue
        if facts[i].get("superseded_by"):
            continue
        fact_i = facts[i].get("fact", "")
        words_i = set(fact_i.lower().split())
        if len(words_i) < min_words:
            continue
        proj_i = facts[i].get("project", "global")
        merge_targets: list[int] = []

        for j in range(i + 1, len(facts)):
            if facts[j]["id"] in removed:
                continue
            if facts[j].get("superseded_by"):
                continue
            if facts[j].get("project", "global") != proj_i:
                continue
            words_j = set(facts[j].get("fact", "").lower().split())
            if len(words_j) < min_words:
                continue
            overlap = words_i & words_j
            union = words_i | words_j
            sim = len(overlap) / len(union) if union else 0.0
            if low <= sim < high:
                merge_targets.append(j)

        if merge_targets:
            best_idx = i
            best_conf = eff_conf.get(facts[i]["id"], 0.0)
            for j in merge_targets:
                j_conf = eff_conf.get(facts[j]["id"], 0.0)
                if j_conf > best_conf:
                    best_idx = j
                    best_conf = j_conf
            losers = [facts[j] for j in [i] + merge_targets if j != best_idx]
            for loser in losers:
                removed.add(loser["id"])
            groups.append((facts[best_idx], losers))

    return groups


# ── Demote / fade candidates ──────────────────────────────────────────────

def demote_candidates(facts: list[dict], now: datetime,
                      max_confidence: float = DEMOTE_CONFIDENCE_MAX,
                      min_idle_days: int = DEMOTE_AGE_DAYS_MIN) -> list[dict]:
    """Facts eligible for live-demotion (archival): non-anchored, with
    collapsed confidence, untouched for ``min_idle_days`` (or never
    accessed at all). Mirrors the live worker's historical SQL filter."""
    cutoff = (now - timedelta(days=min_idle_days)).isoformat()
    out: list[dict] = []
    for f in facts:
        if f.get("anchor_type"):
            continue
        conf = f.get("confidence")
        if conf is not None and conf >= max_confidence:
            continue
        last = f.get("last_accessed")
        if last is not None and last >= cutoff:
            continue
        out.append(f)
    return out


def fade_candidates(facts: list[dict], now: datetime,
                    fade_days: int = 30,
                    fade_factor: float = 0.8) -> list[tuple[dict, float]]:
    """Consolidation fade pass: untouched facts older than ``fade_days``
    have their base confidence reduced by ``fade_factor``.

    Returns [(fact, new_base_confidence), ...].
    """
    out: list[tuple[dict, float]] = []
    for f in facts:
        if f.get("access_count", 0) != 0:
            continue
        ts_str = f.get("created_at", f.get("ts", ""))
        try:
            entry_time = datetime.fromisoformat(ts_str)
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=now.tzinfo)
            if (now - entry_time).days > fade_days:
                base = f.get("base_confidence", f.get("confidence", 0.5))
                out.append((f, base * fade_factor))
        except (ValueError, TypeError):
            continue
    return out


# ── Archive candidates ────────────────────────────────────────────────────

def _age_days(ts_str: str, now: datetime) -> float:
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        return max(0, (now - ts).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 999.0


def decay_archive_candidates(facts: list[dict],
                             eff_conf: dict[str, float],
                             now: datetime,
                             threshold: float = 0.05,
                             min_age_days: int = 60) -> list[tuple[dict, str]]:
    """Hypnos Stage 1 (decay sweep): untouched facts whose effective
    confidence collapsed. Returns [(fact, reason_detail), ...].

    Two routes in: old + untouched + below threshold, or untouched +
    below half the threshold regardless of age (ultra-low).
    """
    out: list[tuple[dict, str]] = []
    for fact in facts:
        eff = eff_conf.get(fact["id"], 1.0)
        if eff >= threshold:
            continue
        if fact.get("access_count", 0) != 0:
            continue
        age = _age_days(fact.get("created_at", ""), now)
        if age >= min_age_days:
            out.append((fact, f"eff_conf={eff:.3f}, age={age:.0f}d"))
        elif eff < threshold * 0.5:
            out.append((fact, f"eff_conf={eff:.3f}"))
    return out


def cold_storage_candidates(facts: list[dict],
                            eff_conf: dict[str, float],
                            now: datetime,
                            min_age_days: int = 90,
                            conf_threshold: float = 0.3) -> list[tuple[dict, str]]:
    """Hypnos Stage 4 (cold storage): truly dormant facts — never
    accessed, very old, low effective confidence.

    Returns [(fact, reason_detail), ...].
    """
    out: list[tuple[dict, str]] = []
    for fact in facts:
        if fact.get("access_count", 0) > 0:
            continue
        age = _age_days(fact.get("created_at", ""), now)
        if age < min_age_days:
            continue
        eff = eff_conf.get(fact["id"], 1.0)
        if eff < conf_threshold:
            out.append((fact, f"age={age:.0f}d, eff_conf={eff:.3f}"))
    return out
