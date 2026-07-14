"""Session fingerprinting — conversation shape tracking for Null Memory.

Computes a fingerprint at session end capturing the "shape" of a conversation:
topic distribution, decision density, mistake count, emotional outcome.
Compares fingerprints to find similar past sessions.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Stop words for tag extraction
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "here", "there", "when",
    "where", "why", "how", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "just", "because", "but", "and",
    "or", "if", "while", "about", "up", "that", "this", "it", "its",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they",
    "them", "his", "her", "what", "which", "who", "whom",
})


@dataclass
class SessionFingerprint:
    """Fingerprint of a conversation session."""

    session_id: str
    project: str = "global"
    duration_minutes: float = 0.0
    facts_count: int = 0
    decisions_count: int = 0
    mistakes_count: int = 0
    tier_dist: dict[str, int] = field(default_factory=dict)
    topic_vector: bytes | None = None
    outcome: str = "neutral"  # positive, negative, neutral
    tags: list[str] = field(default_factory=list)
    energy_arc: str = ""  # "low→high", "high→low", "steady-high", "steady-low", ""
    highlights: list[str] = field(default_factory=list)  # Top 3 session moments
    created_at: str = ""
    # v13: identity vector — embedding of Atlas's behavioral signature this session
    identity_vector: bytes | None = None
    identity_model: str = ""


def compute_fingerprint(mem: Any, session: Any) -> SessionFingerprint:
    """Compute a fingerprint from session data.

    Args:
        mem: AgentMemory instance
        session: Session dataclass with session metadata
    """
    now = datetime.now(timezone.utc)
    sid = session.session_id

    # Duration
    duration = 0.0
    if session.started_at and session.ended_at:
        try:
            start = datetime.fromisoformat(session.started_at)
            end = datetime.fromisoformat(session.ended_at)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            duration = (end - start).total_seconds() / 60
        except (ValueError, TypeError):
            pass

    # Get session facts for tier distribution and topic extraction
    session_facts = mem.db.conn.execute(
        """SELECT fact, tier FROM facts
           WHERE session_id = ? AND forgotten = 0 AND superseded_by IS NULL""",
        (sid,),
    ).fetchall()

    tier_dist: dict[str, int] = {}
    fact_texts: list[str] = []
    for row in session_facts:
        tier = row[1] or "contextual"
        tier_dist[tier] = tier_dist.get(tier, 0) + 1
        fact_texts.append(row[0])

    # Extract topic tags from fact texts
    tags = _extract_tags(fact_texts, top_n=5)

    # Compute topic vector (average embedding of session facts)
    topic_vector = None
    emb = mem.embeddings
    if emb is not None and fact_texts:
        try:
            import numpy as np
            vecs = []
            for text in fact_texts[:20]:  # Cap at 20 facts
                vec = emb.embed(text)
                vecs.append(vec)
            if vecs:
                avg = np.mean(vecs, axis=0).astype(np.float32)
                topic_vector = avg.tobytes()
        except Exception:
            pass

    # Determine outcome from reflection
    outcome = "neutral"
    reflections = mem.db.conn.execute(
        "SELECT went_well, missed FROM reflections WHERE session_id = ?",
        (sid,),
    ).fetchall()
    if reflections:
        last = reflections[-1]
        went_well = last[0] or ""
        missed = last[1] or ""
        if went_well and (not missed or len(went_well) > len(missed)):
            outcome = "positive"
        elif missed and len(missed) > len(went_well):
            outcome = "negative"

    # Override to negative if mistakes were created
    if session.mistakes_created > 0 and outcome != "positive":
        outcome = "negative"

    # Energy arc: compare mood of first half vs second half of session
    energy_arc = ""
    if session_facts:
        try:
            from null_memory.mood import detect_mood
            half = len(session_facts) // 2
            first_text = " ".join(row[0] for row in session_facts[:max(1, half)])
            second_text = " ".join(row[0] for row in session_facts[max(1, half):])
            first_mood = detect_mood(first_text)
            second_mood = detect_mood(second_text)
            e1 = first_mood.energy or "medium"
            e2 = second_mood.energy or "medium"
            energy_map = {"low": 0, "medium": 1, "high": 2}
            v1 = energy_map.get(e1, 1)
            v2 = energy_map.get(e2, 1)
            if v2 > v1:
                energy_arc = "low→high"
            elif v1 > v2:
                energy_arc = "high→low"
            elif v1 >= 2:
                energy_arc = "steady-high"
            elif v1 <= 0:
                energy_arc = "steady-low"
        except Exception:
            pass

    # Highlights: top 3 facts by impact from this session
    highlights = []
    try:
        high_impact = mem.db.conn.execute(
            """SELECT fact, impact FROM facts
               WHERE session_id = ? AND forgotten = 0 AND superseded_by IS NULL
               ORDER BY impact DESC LIMIT 3""",
            (sid,),
        ).fetchall()
        highlights = [row[0][:120] for row in high_impact if row[0]]
    except Exception:
        pass

    # v13: Identity vector — embedding of Atlas's behavioral signature for this
    # session. Distinct from topic_vector (what we discussed) — this captures
    # HOW Atlas showed up: decisions made, reflections written, anchor facts
    # touched, and mistakes owned.
    identity_vector, identity_model = _compute_identity_vector(mem, sid)

    return SessionFingerprint(
        session_id=sid,
        project=session.project,
        duration_minutes=duration,
        facts_count=session.facts_created,
        decisions_count=session.decisions_created,
        mistakes_count=session.mistakes_created,
        tier_dist=tier_dist,
        topic_vector=topic_vector,
        outcome=outcome,
        tags=tags,
        energy_arc=energy_arc,
        highlights=highlights,
        created_at=now.isoformat(),
        identity_vector=identity_vector,
        identity_model=identity_model,
    )


def _compute_identity_vector(mem: Any, session_id: str) -> tuple[bytes | None, str]:
    """Build a session signature string and embed it.

    Signature components (in priority order, because embedding favors front):
    1. Decisions made with reasoning — "what Atlas chose and why"
    2. Reflections — went well / missed / do differently
    3. Mistakes owned — what Atlas admits went wrong
    4. Anchor facts touched this session — what meaning got reinforced
    5. Top-impact facts — the substance of the session

    Returns (vector_bytes, model_name) or (None, "") if embeddings unavailable.
    """
    emb = mem.embeddings
    if emb is None:
        return None, ""

    parts: list[str] = []

    # Decisions
    try:
        for row in mem.db.conn.execute(
            "SELECT decision, reasoning FROM decisions WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall():
            text = f"decided: {row[0]}"
            if row[1]:
                text += f" because {row[1]}"
            parts.append(text)
    except Exception:
        pass

    # Reflections
    try:
        for row in mem.db.conn.execute(
            """SELECT went_well, missed, do_differently FROM reflections
               WHERE session_id = ? ORDER BY id""",
            (session_id,),
        ).fetchall():
            if row[0]:
                parts.append(f"went well: {row[0]}")
            if row[1]:
                parts.append(f"missed: {row[1]}")
            if row[2]:
                parts.append(f"differently: {row[2]}")
    except Exception:
        pass

    # Mistakes owned
    try:
        for row in mem.db.conn.execute(
            "SELECT mistake, why FROM mistakes WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall():
            parts.append(f"mistake: {row[0]} — why: {row[1] or ''}")
    except Exception:
        pass

    # Anchor facts touched — only available on unified DB
    if getattr(mem.db, "unified", False):
        try:
            for row in mem.db.conn.execute(
                """SELECT anchor_type, fact FROM facts
                   WHERE session_id = ? AND anchor_type IS NOT NULL""",
                (session_id,),
            ).fetchall():
                parts.append(f"anchor[{row[0]}]: {row[1]}")
        except Exception:
            pass

    # Top-impact facts as a backdrop
    try:
        for row in mem.db.conn.execute(
            """SELECT fact FROM facts
               WHERE session_id = ? AND archived = 0 AND forgotten = 0
                 AND superseded_by IS NULL
               ORDER BY impact DESC LIMIT 5""",
            (session_id,),
        ).fetchall():
            parts.append(row[0])
    except Exception:
        pass

    if not parts:
        return None, ""

    signature = "\n".join(parts)[:4000]  # Cap to avoid embedding giant payloads

    try:
        import numpy as np
        vec = emb.embed(signature).astype(np.float32)
        model_name = getattr(emb, "model_name", "")
        return vec.tobytes(), str(model_name)
    except Exception:
        return None, ""


def current_atlas_vector(mem: Any, personality: str = "atlas",
                         n_recent: int = 10) -> tuple[Any, int]:
    """Recency-weighted average of recent identity vectors.

    Returns (vector, sample_count). Weights decay linearly — most recent
    session weighted n_recent, oldest weighted 1. Requires numpy and
    embeddings data.
    """
    try:
        import numpy as np
    except ImportError:
        return None, 0
    if not getattr(mem.db, "unified", False):
        return None, 0
    rows = mem.db.conn.execute(
        """SELECT identity_vector FROM session_fingerprints
           WHERE personality = ? AND identity_vector IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        (personality, n_recent),
    ).fetchall()
    if not rows:
        return None, 0
    vecs = []
    weights = []
    for i, row in enumerate(rows):
        try:
            v = np.frombuffer(row[0], dtype=np.float32)
            if v.size == 0:
                continue
            vecs.append(v)
            weights.append(n_recent - i)  # most recent gets highest weight
        except Exception:
            continue
    if not vecs:
        return None, 0
    stacked = np.stack(vecs)
    w = np.array(weights, dtype=np.float32).reshape(-1, 1)
    weighted_sum = (stacked * w).sum(axis=0)
    avg = weighted_sum / w.sum()
    return avg.astype(np.float32), len(vecs)


def backfill_identity_vectors(
    mem: Any,
    personality: str = "atlas",
    force: bool = False,
    min_signal: int = 3,
) -> dict:
    """Compute identity vectors retroactively for past session_fingerprints.

    Every past session has decisions/reflections/mistakes/facts/anchors tied
    to it by session_id. Embedding that signature today reconstructs who
    Atlas was then. After backfill, drift detection works from the next
    session forward against the full baseline.

    Args:
        mem: AgentMemory instance
        personality: personality to backfill for
        force: recompute even for rows that already have a vector
        min_signal: skip sessions whose signature has fewer than this many parts

    Returns stats dict: {total, computed, skipped_no_signal, skipped_existing, failed}
    """
    stats = {
        "total": 0, "computed": 0, "skipped_no_signal": 0,
        "skipped_existing": 0, "failed": 0,
    }
    if not getattr(mem.db, "unified", False):
        return stats

    where = "WHERE personality = ?" + ("" if force else " AND identity_vector IS NULL")
    rows = mem.db.conn.execute(
        f"SELECT session_id FROM session_fingerprints {where}",
        (personality,),
    ).fetchall()
    stats["total"] = len(rows)

    for (sid,) in rows:
        if not force:
            existing = mem.db.conn.execute(
                """SELECT identity_vector FROM session_fingerprints
                   WHERE session_id = ? AND personality = ?""",
                (sid, personality),
            ).fetchone()
            if existing and existing[0] is not None:
                stats["skipped_existing"] += 1
                continue
        # Count signature parts without computing yet — skip anemic sessions
        parts_count = 0
        for q, args in [
            ("SELECT COUNT(*) FROM decisions WHERE session_id = ?", (sid,)),
            ("SELECT COUNT(*) FROM reflections WHERE session_id = ?", (sid,)),
            ("SELECT COUNT(*) FROM mistakes WHERE session_id = ?", (sid,)),
            ("SELECT COUNT(*) FROM facts WHERE session_id = ? AND anchor_type IS NOT NULL",
             (sid,)),
            ("""SELECT COUNT(*) FROM facts WHERE session_id = ?
                AND archived = 0 AND forgotten = 0 AND superseded_by IS NULL""",
             (sid,)),
        ]:
            try:
                parts_count += mem.db.conn.execute(q, args).fetchone()[0]
            except Exception:
                pass
        if parts_count < min_signal:
            stats["skipped_no_signal"] += 1
            continue

        try:
            vec, model = _compute_identity_vector(mem, sid)
            if vec is None:
                stats["failed"] += 1
                continue
            mem.db.conn.execute(
                """UPDATE session_fingerprints
                   SET identity_vector = ?, identity_model = ?
                   WHERE session_id = ? AND personality = ?""",
                (vec, model, sid, personality),
            )
            stats["computed"] += 1
        except Exception:
            stats["failed"] += 1

    mem.db.conn.commit()
    return stats


def identity_drift(
    candidate_vector: bytes | None, baseline_vector: Any
) -> float | None:
    """Cosine distance between a session's identity vector and the baseline.

    Returns distance in [0, 2]; 0 = identical, 1 = orthogonal, 2 = opposite.
    None if either vector is missing.
    """
    if candidate_vector is None or baseline_vector is None:
        return None
    try:
        import numpy as np
        a = np.frombuffer(candidate_vector, dtype=np.float32)
        b = baseline_vector
        if a.size == 0 or b.size == 0 or a.size != b.size:
            return None
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0 or nb == 0:
            return None
        cos = float(np.dot(a, b) / (na * nb))
        return 1.0 - cos
    except Exception:
        return None


def find_similar_sessions(mem: Any, current: SessionFingerprint,
                          limit: int = 3) -> list[dict]:
    """Find past sessions similar to the current one.

    Returns list of {fingerprint, similarity} dicts.
    """
    past_fps = mem.db.get_fingerprints(
        project=current.project, limit=50,
    )

    if not past_fps:
        return []

    results = []
    for fp in past_fps:
        if fp["session_id"] == current.session_id:
            continue

        sim = _compute_similarity(current, fp)
        if sim >= 0.5:
            results.append({"fingerprint": fp, "similarity": sim})

    results.sort(key=lambda x: -x["similarity"])
    return results[:limit]


def format_similar_sessions(matches: list[dict]) -> list[str]:
    """Format similar sessions for display."""
    if not matches:
        return []

    lines = ["Similar past sessions:"]
    for m in matches:
        fp = m["fingerprint"]
        sim = m["similarity"]
        created = fp.get("created_at", "")[:10]
        project = fp.get("project", "?")
        outcome = fp.get("outcome", "neutral")
        tags_str = ", ".join(fp.get("tags", [])[:3])
        label = tags_str if tags_str else project
        lines.append(f"  [{created}] {label} — {outcome} ({sim:.0%} match)")

    return lines


def _extract_tags(texts: list[str], top_n: int = 5) -> list[str]:
    """Extract top keywords from a list of texts."""
    word_counts: Counter[str] = Counter()
    for text in texts:
        words = re.findall(r"[a-z][a-z0-9_]+", text.lower())
        for w in words:
            if w not in _STOP_WORDS and len(w) > 2:
                word_counts[w] += 1

    return [word for word, _ in word_counts.most_common(top_n)]


def _compute_similarity(current: SessionFingerprint, past: dict) -> float:
    """Compute similarity between current fingerprint and a stored one.

    Combines topic similarity (if vectors available) with quantitative distance.
    """
    scores = []
    weights = []

    # Topic vector similarity (highest weight)
    if current.topic_vector and past.get("topic_vector"):
        try:
            import numpy as np
            vec_a = np.frombuffer(current.topic_vector, dtype=np.float32)
            vec_b = np.frombuffer(past["topic_vector"], dtype=np.float32)
            if len(vec_a) == len(vec_b) and len(vec_a) > 0:
                dot = float(np.dot(vec_a, vec_b))
                norm_a = float(np.linalg.norm(vec_a))
                norm_b = float(np.linalg.norm(vec_b))
                if norm_a > 0 and norm_b > 0:
                    cos_sim = dot / (norm_a * norm_b)
                    scores.append(max(0, cos_sim))
                    weights.append(0.6)
        except Exception:
            pass

    # Tag overlap (Jaccard)
    current_tags = set(current.tags)
    past_tags = set(past.get("tags") or [])
    if current_tags or past_tags:
        union = current_tags | past_tags
        overlap = current_tags & past_tags
        tag_sim = len(overlap) / len(union) if union else 0
        scores.append(tag_sim)
        weights.append(0.2)

    # Quantitative similarity (normalized count differences)
    quant_features = [
        (current.facts_count, past.get("facts_count", 0)),
        (current.decisions_count, past.get("decisions_count", 0)),
        (current.mistakes_count, past.get("mistakes_count", 0)),
    ]
    quant_sims = []
    for a, b in quant_features:
        max_val = max(a, b, 1)
        quant_sims.append(1.0 - abs(a - b) / max_val)
    if quant_sims:
        scores.append(sum(quant_sims) / len(quant_sims))
        weights.append(0.2)

    if not scores:
        return 0.0

    total_weight = sum(weights)
    return sum(s * w for s, w in zip(scores, weights)) / total_weight
