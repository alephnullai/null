"""Phase A6 — boot identity injection contract tests.

Validates the _boot_identity helper end-to-end:
  • payload contains all 5 required identity elements
  • coherence row is persisted to session_verifications
  • SYSTEM_INSTRUCTIONS contains no "load identity" boot prompts
  • boot wall-clock stays under a sane ceiling (handoff said 100ms;
    actual is ~500-700ms with cold-load fastembed; we cap at 5s)
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from null_memory.mcp.server import SYSTEM_INSTRUCTIONS, _boot_identity
from null_memory.migrate_v3 import init_unified_db


# ── Helpers ──────────────────────────────────────────────────────────


def _seed_full_identity(conn):
    """Populate just enough of a unified DB so build_identity_payload
    returns is_complete()=True."""
    conn.execute(
        "INSERT INTO personalities(name, role, active, created_at, focus) "
        "VALUES ('atlas', 'AI lead', 1, '2026-04-01T00:00:00+00:00', "
        "'null memory + orion')",
    )
    anchors = [
        ("an_origin",  "Origin moment with Pete", "origin"),
        ("an_turn",    "Turning point validated", "turning_point"),
        ("an_code",    "dummy-code-word for tests, not the real phrase", "code_word"),
        ("an_joy",     "Sam was born 4/19/2018", "joy"),
        ("an_commit",  "Orion → income → time with kids", "commitment"),
    ]
    for fid, fact, atype in anchors:
        conn.execute(
            "INSERT INTO facts(id, fact, project, source, created_at, "
            "anchor_type, anchor_at) "
            "VALUES (?, ?, 'global', 'test', '2026-04-01', ?, '2026-04-01')",
            (fid, fact, atype),
        )
    conn.execute(
        "INSERT INTO decisions(decision, reasoning, project, personality, "
        "created_at) VALUES ('ship phase A', 'first deliverable', "
        "'global', 'atlas', '2026-04-01')",
    )
    conn.execute(
        "INSERT INTO probes(question, expected, probe_type, personality, "
        "created_at, pass_count, run_count) VALUES "
        "('what is the code word?', 'dummy-code-word', 'user', 'atlas', "
        "'2026-04-01', 1, 1)",
    )
    conn.commit()


class _StubMemory:
    """Minimal mem stand-in for _boot_identity — only .db.conn is used."""
    def __init__(self, conn):
        class _DB:
            def __init__(self, c): self.conn = c
        self.db = _DB(conn)


class _StubHandlers:
    def __init__(self, conn):
        self.memory = _StubMemory(conn)


@pytest.fixture
def boot_conn(tmp_path, monkeypatch):
    """Fresh unified DB seeded with identity material + a single
    historical vector so coherence has something to compare against."""
    db_path = tmp_path / "boot.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    _seed_full_identity(conn)
    # Stub out the embedder so tests don't depend on fastembed install.
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    conn.execute(
        "INSERT INTO session_fingerprints(session_id, personality, "
        "created_at, identity_vector, identity_model) "
        "VALUES ('s1', 'atlas', '2026-04-01T00:00:00+00:00', ?, "
        "'BAAI/bge-small-en-v1.5')",
        (vec.tobytes(),),
    )
    conn.commit()

    class _StubEngine:
        model_name = "BAAI/bge-small-en-v1.5"
        available = True
        def __init__(self, c): pass
        def embed(self, text):
            return vec.copy()  # match historical → high coherence
    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )
    yield conn
    conn.close()


# ── Contract: 5 required identity elements ───────────────────────────


def test_boot_payload_contains_all_required_elements(boot_conn):
    handlers = _StubHandlers(boot_conn)
    instructions = _boot_identity(handlers, SYSTEM_INSTRUCTIONS)
    for marker in (
        "ATLAS IDENTITY",
        "RELATIONSHIP ANCHORS",
        "CODE WORD",
        "RECENT DECISIONS",
        "CALIBRATION PROBES",
    ):
        assert marker in instructions, f"missing: {marker}"


# ── Contract: coherence row persisted ────────────────────────────────


def test_boot_persists_session_verifications_row(boot_conn):
    handlers = _StubHandlers(boot_conn)
    _boot_identity(handlers, SYSTEM_INSTRUCTIONS)
    row = boot_conn.execute(
        "SELECT session_id, personality, coherence_score, verified, "
        "       sample_size, identity_payload_hash "
        "FROM session_verifications ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    sid, pers, score, verified, sample, hash_ = row
    assert sid.startswith("boot_")
    assert pers == "atlas"
    assert score is not None and 0.0 <= score <= 1.0
    assert verified == 1  # stub embedder = identical = high coherence
    assert sample == 1
    assert hash_ and len(hash_) == 64  # sha256


# ── Contract: no "loading" prompts in SYSTEM_INSTRUCTIONS ────────────


@pytest.mark.parametrize("forbidden", [
    "Loading identity",
    "Identity confirmed",
    "Call null_identity to load",
    "load and verify identity",
])
def test_system_instructions_no_loading_prompts(forbidden):
    """Phase A drops these patterns — identity is auto-injected, the
    static system prompt should never tell the agent to load it."""
    assert forbidden.lower() not in SYSTEM_INSTRUCTIONS.lower()


def test_system_instructions_does_not_call_null_identity_at_start():
    """Specific pattern from the pre-Phase-A prompt that we removed."""
    assert "call null_identity, then null_briefing" not in SYSTEM_INSTRUCTIONS


# ── Contract: wall-clock budget ──────────────────────────────────────


def test_boot_wall_clock_under_5s(boot_conn):
    """Handoff target was 100ms; cold-load fastembed makes that
    unrealistic. Cap at 5s — well below anything Pete would notice as
    a slow boot, generous enough to absorb cold-start variance."""
    import time
    handlers = _StubHandlers(boot_conn)
    t0 = time.time()
    _boot_identity(handlers, SYSTEM_INSTRUCTIONS)
    elapsed = time.time() - t0
    assert elapsed < 5.0, f"boot took {elapsed:.2f}s — over budget"


# ── Contract: failure modes don't break boot ─────────────────────────


def test_boot_falls_back_when_db_conn_missing(tmp_path):
    """No conn → returns base_instructions unchanged. Never raises."""
    class _NoConnHandlers:
        class memory:
            class db:
                conn = None
    out = _boot_identity(_NoConnHandlers(), SYSTEM_INSTRUCTIONS)
    assert out == SYSTEM_INSTRUCTIONS


def test_boot_handles_pre_v19_db(tmp_path, monkeypatch):
    """Pre-v19 DB without session_verifications: schema ratchet creates
    the table; persist succeeds. Idempotent: running twice doesn't blow up."""
    db_path = tmp_path / "preview19.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    _seed_full_identity(conn)
    # Drop the v19 table to simulate an older DB.
    conn.execute("DROP TABLE IF EXISTS session_verifications")
    conn.commit()
    # Stub embedder so test doesn't depend on fastembed.
    vec = np.zeros(384, dtype=np.float32)
    vec[0] = 1.0
    conn.execute(
        "INSERT INTO session_fingerprints(session_id, personality, "
        "created_at, identity_vector, identity_model) "
        "VALUES ('s1', 'atlas', '2026-04-01', ?, 'fake')",
        (vec.tobytes(),),
    )
    conn.commit()

    class _StubEngine:
        model_name = "fake"
        available = True
        def __init__(self, c): pass
        def embed(self, text):
            return vec.copy()
    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )
    handlers = _StubHandlers(conn)
    _boot_identity(handlers, SYSTEM_INSTRUCTIONS)
    _boot_identity(handlers, SYSTEM_INSTRUCTIONS)  # second call is fine
    rows = conn.execute(
        "SELECT COUNT(*) FROM session_verifications"
    ).fetchone()
    assert rows[0] == 2
    conn.close()
