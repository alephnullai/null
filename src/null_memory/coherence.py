"""Phase A — on-boot coherence scoring.

Question this module answers: "Does the bootstrapping Atlas instance match
historical Atlas?" Cosine similarity between an embedding of the current
identity payload and the recency-weighted average of the historical
identity_vector centroid.

  score >= VERIFIED_THRESHOLD (0.80) → verified=True
  score <  WARN_THRESHOLD     (0.60) → log warning
  no historical vectors             → score=None, cold-start note

Pure inputs (sqlite3.Connection + flags). No MCP/network. Embedding load
is lazy via EmbeddingEngine, so callers without fastembed get a graceful
None-score result instead of an exception.

Language-pattern classifier (decision-text style match) is deliberately
deferred to v2.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from null_memory.identity_payload import (
    IdentityPayload,
    build_identity_payload,
)


logger = logging.getLogger(__name__)


VERIFIED_THRESHOLD = 0.80
WARN_THRESHOLD = 0.60
DEFAULT_N_RECENT = 10


@dataclass
class CoherenceResult:
    """Outcome of one boot-time coherence check.

    score=None means we couldn't compute one (cold start or no embedder).
    Callers should treat None as "informational" and never as drift.
    """
    score: float | None
    verified: bool
    sample_size: int
    payload_hash: str
    embedding_model: str = ""
    notes: list[str] = field(default_factory=list)
    payload: IdentityPayload | None = None


def _historical_centroid(
    conn: sqlite3.Connection,
    personality: str,
    n_recent: int,
):
    """Recency-weighted average of the most recent N identity_vectors.

    Mirrors `null_memory.fingerprint.current_atlas_vector` but works on a
    bare connection (no AgentMemory dep) so it's testable in isolation.
    Returns (np.ndarray | None, sample_count, model_name).
    """
    try:
        import numpy as np
    except ImportError:
        return None, 0, ""
    rows = conn.execute(
        """SELECT identity_vector, identity_model
           FROM session_fingerprints
           WHERE personality = ? AND identity_vector IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        (personality, n_recent),
    ).fetchall()
    if not rows:
        return None, 0, ""
    vecs: list = []
    weights: list[int] = []
    model_name = ""
    for i, (blob, model) in enumerate(rows):
        try:
            v = np.frombuffer(blob, dtype=np.float32)
            if v.size == 0:
                continue
            vecs.append(v)
            weights.append(n_recent - i)  # newest weighted highest
            if not model_name and model:
                model_name = model
        except Exception:
            continue
    if not vecs:
        return None, 0, model_name
    stacked = np.stack(vecs)
    w = np.array(weights, dtype=np.float32).reshape(-1, 1)
    centroid = (stacked * w).sum(axis=0) / w.sum()
    return centroid.astype(np.float32), len(vecs), model_name


def _cosine(a, b) -> float:
    """Cosine similarity, clamped to [-1, 1] then mapped to [0, 1].
    Returns 0.0 if either vector is degenerate (zero norm)."""
    import numpy as np
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    raw = float(np.dot(a, b) / (na * nb))
    # Numeric drift can push raw slightly outside [-1, 1].
    raw = max(-1.0, min(1.0, raw))
    # Map to [0, 1] — negative similarity is rare for embeddings of
    # related text, but if it happens treat it as fully un-aligned.
    return max(0.0, raw)


def compute_coherence(
    conn: sqlite3.Connection,
    personality: str = "atlas",
    n_recent: int = DEFAULT_N_RECENT,
    payload: IdentityPayload | None = None,
) -> CoherenceResult:
    """Compute boot-time coherence score for `personality`.

    Steps:
      1. Build (or accept) the identity payload
      2. Embed payload.text → current_vec
      3. Recency-weighted centroid of last `n_recent` historical vectors
      4. cosine(current_vec, centroid) → score

    Cold-start (no historical vectors) → score=None, verified=False.
    Embedder unavailable → score=None, "embedder not available" note.
    """
    if payload is None:
        payload = build_identity_payload(conn, personality=personality)

    notes: list[str] = []

    # 1. Historical centroid
    centroid, sample_size, hist_model = _historical_centroid(
        conn, personality, n_recent,
    )
    if centroid is None or sample_size == 0:
        notes.append("cold start: no historical identity vectors yet")
        return CoherenceResult(
            score=None, verified=False, sample_size=0,
            payload_hash=payload.sha256, embedding_model="",
            notes=notes, payload=payload,
        )

    if sample_size < 3:
        notes.append(f"low sample size ({sample_size}) — score is noisy")

    # 2. Embed current payload
    try:
        from null_memory.embeddings import EmbeddingEngine
    except ImportError:
        notes.append("embeddings module not importable")
        return CoherenceResult(
            score=None, verified=False, sample_size=sample_size,
            payload_hash=payload.sha256, embedding_model=hist_model,
            notes=notes, payload=payload,
        )

    try:
        engine = EmbeddingEngine(conn)
        if not engine.available:
            notes.append("fastembed not installed — coherence skipped")
            return CoherenceResult(
                score=None, verified=False, sample_size=sample_size,
                payload_hash=payload.sha256, embedding_model=hist_model,
                notes=notes, payload=payload,
            )
        current_vec = engine.embed(payload.text)
        current_model = engine.model_name
    except Exception as exc:
        notes.append(f"embedding failed: {exc}")
        logger.warning("[coherence] embedding failed: %s", exc)
        return CoherenceResult(
            score=None, verified=False, sample_size=sample_size,
            payload_hash=payload.sha256, embedding_model=hist_model,
            notes=notes, payload=payload,
        )

    # 3. Dimension sanity
    if current_vec.shape != centroid.shape:
        notes.append(
            f"dimension mismatch: current={current_vec.shape} "
            f"hist={centroid.shape} — likely model upgrade"
        )
        return CoherenceResult(
            score=None, verified=False, sample_size=sample_size,
            payload_hash=payload.sha256,
            embedding_model=current_model or hist_model,
            notes=notes, payload=payload,
        )

    # 4. Cosine
    score = _cosine(current_vec, centroid)
    verified = score >= VERIFIED_THRESHOLD
    if score < WARN_THRESHOLD:
        notes.append(
            f"coherence below warn threshold ({score:.3f} < {WARN_THRESHOLD})"
        )
        logger.warning(
            "[coherence] score %.3f for %s — possible drift", score, personality,
        )

    return CoherenceResult(
        score=score, verified=verified, sample_size=sample_size,
        payload_hash=payload.sha256,
        embedding_model=current_model or hist_model,
        notes=notes, payload=payload,
    )
