"""Phase A — tests for coherence.compute_coherence.

Split into two layers:
  • Pure-Python tests that don't need fastembed (cold start, threshold
    classification, dimension-mismatch handling, _cosine math).
  • Integration tests that require fastembed installed — they actually
    embed a payload and compare against synthetic historical vectors.

The fastembed layer is module-skipped if fastembed isn't available, so
CI without ML deps still runs the pure-Python coverage.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from null_memory.coherence import (
    VERIFIED_THRESHOLD,
    WARN_THRESHOLD,
    CoherenceResult,
    _cosine,
    _historical_centroid,
    compute_coherence,
)
from null_memory.identity_payload import IdentityPayload
from null_memory.migrate_v3 import init_unified_db


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def fresh_conn(tmp_path):
    db_path = tmp_path / "u.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


def _seed_personality(conn):
    conn.execute(
        "INSERT INTO personalities(name, role, active, created_at, focus) "
        "VALUES ('atlas', 'AI lead', 1, '2026-04-01T00:00:00+00:00', 'null')",
    )
    conn.execute(
        "INSERT INTO facts(id, fact, project, source, created_at, "
        "anchor_type, anchor_at) VALUES "
        "('a1', 'dummy-code-word for tests, not the real phrase', 'global', 'test', "
        "'2026-04-01', 'code_word', '2026-04-01')",
    )
    conn.execute(
        "INSERT INTO decisions(decision, reasoning, project, personality, "
        "created_at) VALUES ('test decision', 'why', 'global', 'atlas', "
        "'2026-04-01')",
    )
    conn.execute(
        "INSERT INTO probes(question, expected, probe_type, personality, "
        "created_at, pass_count, run_count) VALUES "
        "('q?', 'a', 'user', 'atlas', '2026-04-01', 1, 1)",
    )
    conn.commit()


def _insert_historical_vector(conn, session_id, vec_array,
                                created_at, personality="atlas",
                                model="BAAI/bge-small-en-v1.5"):
    """Insert a row with a pre-computed identity_vector blob."""
    conn.execute(
        "INSERT INTO session_fingerprints(session_id, personality, "
        "created_at, identity_vector, identity_model) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, personality, created_at,
         vec_array.astype(np.float32).tobytes(), model),
    )
    conn.commit()


# ── Pure-Python: _cosine math ────────────────────────────────────────


def test_cosine_identical_vectors_returns_one():
    a = np.array([1.0, 2.0, 3.0])
    assert _cosine(a, a) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_returns_zero():
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert _cosine(a, b) == pytest.approx(0.0)


def test_cosine_opposite_vectors_clamped_to_zero():
    """Negative cosine clamped to 0 — embeddings rarely produce true
    opposites; if they do, treat as 'totally unaligned' not 'inverse'."""
    a = np.array([1.0, 0.0])
    b = np.array([-1.0, 0.0])
    assert _cosine(a, b) == 0.0


def test_cosine_zero_norm_returns_zero():
    a = np.array([0.0, 0.0])
    b = np.array([1.0, 1.0])
    assert _cosine(a, b) == 0.0


# ── Pure-Python: _historical_centroid ────────────────────────────────


def test_historical_centroid_empty_table(fresh_conn):
    centroid, n, model = _historical_centroid(fresh_conn, "atlas", 10)
    assert centroid is None
    assert n == 0


def test_historical_centroid_recency_weighting(fresh_conn):
    """Newest vector should dominate a recency-weighted centroid."""
    _insert_historical_vector(
        fresh_conn, "old", np.array([1.0, 0.0, 0.0]),
        created_at="2026-01-01T00:00:00+00:00",
    )
    _insert_historical_vector(
        fresh_conn, "new", np.array([0.0, 1.0, 0.0]),
        created_at="2026-04-01T00:00:00+00:00",
    )
    centroid, n, _ = _historical_centroid(fresh_conn, "atlas", 10)
    assert n == 2
    # Newer vector (n_recent - 0 = 10) outweighs older (n_recent - 1 = 9).
    # Centroid's y-component should be > x-component.
    assert centroid[1] > centroid[0]


# ── Cold-start path ──────────────────────────────────────────────────


def test_compute_coherence_cold_start(fresh_conn):
    """No historical vectors → score is None with cold-start note."""
    _seed_personality(fresh_conn)
    r = compute_coherence(fresh_conn, "atlas")
    assert isinstance(r, CoherenceResult)
    assert r.score is None
    assert r.verified is False
    assert r.sample_size == 0
    assert any("cold start" in n.lower() for n in r.notes)
    assert r.payload_hash  # payload still built


# ── Dimension-mismatch path ──────────────────────────────────────────


def test_compute_coherence_dimension_mismatch(fresh_conn, monkeypatch):
    """Historical vectors at a different dim than current embedder
    output → graceful None, mismatch note. Simulates a model upgrade."""
    _seed_personality(fresh_conn)
    # Store a 5-dim vector; payload embedder will return 384-dim.
    _insert_historical_vector(
        fresh_conn, "s1", np.array([1.0, 0.0, 0.0, 0.0, 0.0]),
        created_at="2026-04-01T00:00:00+00:00",
    )

    class _StubEngine:
        model_name = "fake"
        available = True
        def __init__(self, conn):
            pass
        def embed(self, text):
            return np.array([1.0] * 384, dtype=np.float32)

    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )
    r = compute_coherence(fresh_conn, "atlas")
    assert r.score is None
    assert any("dimension" in n.lower() for n in r.notes)


# ── Threshold logic ──────────────────────────────────────────────────


def test_compute_coherence_high_score_verified(fresh_conn, monkeypatch):
    """Current vector ≈ centroid → score ~1.0, verified=True."""
    _seed_personality(fresh_conn)
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    _insert_historical_vector(
        fresh_conn, "s1", vec.copy(),
        created_at="2026-04-01T00:00:00+00:00",
    )

    class _StubEngine:
        model_name = "fake"
        available = True
        def __init__(self, conn):
            pass
        def embed(self, text):
            # Identical to the historical vector.
            return vec.copy()

    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )
    r = compute_coherence(fresh_conn, "atlas")
    assert r.score is not None
    assert r.score >= VERIFIED_THRESHOLD
    assert r.verified is True


def test_compute_coherence_low_score_warn(fresh_conn, monkeypatch):
    """Orthogonal current vs historical → score 0.0, warn note appears."""
    _seed_personality(fresh_conn)
    hist = np.zeros(384, dtype=np.float32)
    hist[0] = 1.0
    _insert_historical_vector(
        fresh_conn, "s1", hist,
        created_at="2026-04-01T00:00:00+00:00",
    )
    cur = np.zeros(384, dtype=np.float32)
    cur[1] = 1.0  # orthogonal to hist

    class _StubEngine:
        model_name = "fake"
        available = True
        def __init__(self, conn):
            pass
        def embed(self, text):
            return cur.copy()

    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )
    r = compute_coherence(fresh_conn, "atlas")
    assert r.score is not None
    assert r.score < WARN_THRESHOLD
    assert r.verified is False
    assert any("warn threshold" in n.lower() for n in r.notes)


def test_compute_coherence_low_sample_size_note(fresh_conn, monkeypatch):
    """Single historical vector → low-sample-size note attached even
    on a high score."""
    _seed_personality(fresh_conn)
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    _insert_historical_vector(
        fresh_conn, "s1", vec.copy(),
        created_at="2026-04-01T00:00:00+00:00",
    )

    class _StubEngine:
        model_name = "fake"
        available = True
        def __init__(self, conn):
            pass
        def embed(self, text):
            return vec.copy()

    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )
    r = compute_coherence(fresh_conn, "atlas")
    assert any("low sample" in n.lower() for n in r.notes)


# ── Embedder-unavailable path ────────────────────────────────────────


def test_compute_coherence_embedder_unavailable(fresh_conn, monkeypatch):
    """fastembed missing → score=None, helpful note, no crash."""
    _seed_personality(fresh_conn)
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    _insert_historical_vector(
        fresh_conn, "s1", vec.copy(),
        created_at="2026-04-01T00:00:00+00:00",
    )

    class _StubEngine:
        model_name = ""
        available = False
        def __init__(self, conn):
            pass

    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )
    r = compute_coherence(fresh_conn, "atlas")
    assert r.score is None
    assert any("fastembed" in n.lower() for n in r.notes)
    assert r.sample_size == 1


# ── Integration with real embedder (skipped if fastembed missing) ────


fastembed_available = pytest.importorskip(
    "fastembed", reason="fastembed not installed — skipping integration tests",
) is not None


@pytest.mark.skipif(not fastembed_available, reason="fastembed required")
def test_compute_coherence_with_real_embedder(fresh_conn):
    """End-to-end: real embedder embeds a payload, compares against a
    historical vector that was *also* the embedding of the same payload.
    Score should be near 1.0 (verified)."""
    _seed_personality(fresh_conn)
    # Generate a historical vector by embedding the current payload —
    # it's the same DB so the payload hash matches.
    from null_memory.embeddings import EmbeddingEngine
    from null_memory.identity_payload import build_identity_payload
    payload = build_identity_payload(fresh_conn, "atlas")
    eng = EmbeddingEngine(fresh_conn)
    if not eng.available:
        pytest.skip("fastembed reports unavailable at runtime")
    historical = eng.embed(payload.text)
    _insert_historical_vector(
        fresh_conn, "s_real", historical,
        created_at="2026-04-01T00:00:00+00:00",
    )
    r = compute_coherence(fresh_conn, "atlas", payload=payload)
    assert r.score is not None
    assert r.score >= VERIFIED_THRESHOLD
    assert r.verified is True
    assert r.sample_size == 1
