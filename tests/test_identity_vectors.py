"""Tests for identity vectors + drift detection (Phase 2b, schema v13).

An identity vector is an embedding of Atlas's behavioral signature per session
(decisions, reflections, mistakes, anchor touches). Drift is cosine distance
between the latest session's vector and a recency-weighted baseline.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from null_memory.agent import AgentMemory
from null_memory.fingerprint import current_atlas_vector, identity_drift
from null_memory.migrate_v3 import init_unified_db


pytestmark = pytest.mark.skipif(
    not pytest.importorskip("fastembed", reason="fastembed not installed"),
    reason="identity vectors require embeddings",
)


@pytest.fixture
def unified_agent(tmp_path, monkeypatch):
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified
    return mem


def _put_fp(mem, sid, vec_bytes, model="BAAI/bge-small-en-v1.5",
            created_at=None):
    created = created_at or datetime.now(timezone.utc).isoformat()
    mem.db.insert_fingerprint({
        "session_id": sid,
        "project": "null",
        "duration_minutes": 60,
        "facts_count": 5,
        "decisions_count": 2,
        "mistakes_count": 0,
        "tier_dist": {},
        "topic_vector": None,
        "outcome": "positive",
        "tags": [],
        "energy_arc": "",
        "highlights": [],
        "created_at": created,
        "identity_vector": vec_bytes,
        "identity_model": model,
    })
    mem.db.conn.commit()


def test_compute_identity_vector_embeds_session(unified_agent):
    mem = unified_agent
    from null_memory.fingerprint import _compute_identity_vector
    # Populate a session with rich data
    sid = "sess-1"
    mem.db.conn.execute(
        "INSERT INTO decisions (decision, reasoning, personality, session_id, created_at) "
        "VALUES (?, ?, 'atlas', ?, ?)",
        ("ship phase 2", "thesis alignment", sid, "2026-04-17T00:00:00+00:00"),
    )
    mem.db.conn.execute(
        "INSERT INTO reflections (went_well, missed, do_differently, personality, session_id, created_at) "
        "VALUES (?, ?, ?, 'atlas', ?, ?)",
        ("anchored memories", "auto-tagging", "ship smaller slices", sid,
         "2026-04-17T00:00:00+00:00"),
    )
    mem.db.conn.commit()
    vec, model = _compute_identity_vector(mem, sid)
    assert vec is not None
    assert model  # Non-empty model name
    import numpy as np
    arr = np.frombuffer(vec, dtype=np.float32)
    assert arr.size == 384  # bge-small-en-v1.5 dimension


def test_current_atlas_vector_weighted_average(unified_agent):
    mem = unified_agent
    import numpy as np
    # Three sessions with identical vectors — baseline should equal each.
    vec = np.ones(384, dtype=np.float32).tobytes()
    for i in range(3):
        _put_fp(mem, f"sess-{i}", vec,
                created_at=f"2026-04-{10 + i:02d}T00:00:00+00:00")
    baseline, n = current_atlas_vector(mem, personality="atlas")
    assert n == 3
    assert baseline is not None
    assert np.allclose(baseline, np.ones(384), atol=1e-6)


def test_identity_drift_zero_for_matching_vectors():
    import numpy as np
    v = np.ones(384, dtype=np.float32)
    dist = identity_drift(v.tobytes(), v)
    assert dist is not None
    assert dist < 1e-6


def test_identity_drift_high_for_orthogonal_vectors():
    import numpy as np
    a = np.zeros(384, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(384, dtype=np.float32)
    b[1] = 1.0
    dist = identity_drift(a.tobytes(), b)
    # Orthogonal → cos=0, distance = 1.0
    assert dist is not None
    assert 0.99 < dist < 1.01


def test_drift_returns_none_for_missing_vectors():
    import numpy as np
    v = np.ones(384, dtype=np.float32)
    assert identity_drift(None, v) is None
    assert identity_drift(v.tobytes(), None) is None


def test_briefing_shows_drift_line_when_enough_sessions(unified_agent):
    mem = unified_agent
    import numpy as np
    # 4 consistent sessions with slight noise so baseline has real weight
    rng = np.random.default_rng(seed=42)
    base = np.ones(384, dtype=np.float32)
    for i in range(4):
        noise = rng.standard_normal(384).astype(np.float32) * 0.01
        vec = (base + noise).tobytes()
        _put_fp(mem, f"sess-{i}", vec,
                created_at=f"2026-04-{10 + i:02d}T00:00:00+00:00")
    briefing = mem.briefing()
    assert "Identity drift" in briefing


def test_briefing_hides_drift_when_insufficient_data(unified_agent):
    mem = unified_agent
    import numpy as np
    vec = np.ones(384, dtype=np.float32).tobytes()
    _put_fp(mem, "only-one", vec)
    briefing = mem.briefing()
    assert "Identity drift" not in briefing


def test_backfill_identity_vectors_from_existing_fingerprints(unified_agent):
    """Retroactive compute: a fingerprint row that lacks identity_vector
    should be populated when its session has decisions/reflections/etc."""
    mem = unified_agent
    from null_memory.fingerprint import backfill_identity_vectors
    # Seed: a fingerprint row with NULL identity_vector, paired with session data.
    sid = "backfill-sess"
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           duration_minutes, facts_count, decisions_count, mistakes_count,
           tier_dist, topic_vector, outcome, tags, energy_arc, highlights,
           created_at) VALUES (?, 'atlas', 'null', 60, 5, 2, 0, '{}', NULL,
           'positive', '[]', '', '[]', ?)""",
        (sid, "2026-04-10T00:00:00+00:00"),
    )
    mem.db.conn.execute(
        "INSERT INTO decisions (decision, reasoning, personality, session_id, created_at) "
        "VALUES ('decide', 'because', 'atlas', ?, ?)",
        (sid, "2026-04-10T00:00:00+00:00"),
    )
    mem.db.conn.execute(
        "INSERT INTO reflections (went_well, missed, do_differently, personality, session_id, created_at) "
        "VALUES ('x', 'y', 'z', 'atlas', ?, ?)",
        (sid, "2026-04-10T00:00:00+00:00"),
    )
    mem.db.conn.execute(
        "INSERT INTO mistakes (mistake, why, personality, session_id, created_at) "
        "VALUES ('bad', 'reason', 'atlas', ?, ?)",
        (sid, "2026-04-10T00:00:00+00:00"),
    )
    mem.db.conn.commit()
    stats = backfill_identity_vectors(mem, personality="atlas")
    assert stats["computed"] == 1
    row = mem.db.conn.execute(
        "SELECT identity_vector FROM session_fingerprints WHERE session_id = ?",
        (sid,),
    ).fetchone()
    assert row[0] is not None


def test_backfill_skips_sessions_with_insufficient_signal(unified_agent):
    mem = unified_agent
    from null_memory.fingerprint import backfill_identity_vectors
    sid = "thin-sess"
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           duration_minutes, facts_count, decisions_count, mistakes_count,
           tier_dist, topic_vector, outcome, tags, energy_arc, highlights,
           created_at) VALUES (?, 'atlas', 'null', 0, 0, 0, 0, '{}', NULL,
           'neutral', '[]', '', '[]', ?)""",
        (sid, "2026-04-10T00:00:00+00:00"),
    )
    mem.db.conn.commit()
    stats = backfill_identity_vectors(mem, personality="atlas", min_signal=3)
    assert stats["skipped_no_signal"] >= 1
    assert stats["computed"] == 0


def test_drift_flags_divergent_session(unified_agent):
    mem = unified_agent
    import numpy as np
    rng = np.random.default_rng(seed=7)
    base = np.zeros(384, dtype=np.float32)
    base[0] = 1.0
    for i in range(4):
        noise = rng.standard_normal(384).astype(np.float32) * 0.01
        _put_fp(mem, f"sess-{i}", (base + noise).tobytes(),
                created_at=f"2026-04-{i + 1:02d}T00:00:00+00:00")
    # Last session — orthogonal direction
    divergent = np.zeros(384, dtype=np.float32)
    divergent[1] = 1.0
    _put_fp(mem, "sess-divergent", divergent.tobytes(),
            created_at="2026-04-15T00:00:00+00:00")
    briefing = mem.briefing()
    assert "drift detected" in briefing
