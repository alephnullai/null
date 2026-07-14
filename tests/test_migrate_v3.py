"""Tests for the v3 unified-substrate migration.

Uses synthetic mini-DBs so tests run anywhere without depending on a real
~/.null/ layout. Covers:
- dedup of facts across personalities by id
- overlay row per (fact_id, personality)
- attribution column propagation to decisions/mistakes/reflections/etc
- decision_outcomes remapping to new decision ids
- orphan fact deletion → no matching embedding copied
- multiverse.db (personalities, broadcasts, dreams, xrefs, xref_facts) folded in
- idempotency (force=True re-run produces identical stats)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from null_memory.migrate_v3 import migrate, verify, UNIFIED_SCHEMA_VERSION


# ── Fixture helpers ────────────────────────────────────────────────────────


def _source_schema(conn: sqlite3.Connection) -> None:
    """Create a minimal v11-ish source schema."""
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE facts (
            id TEXT PRIMARY KEY,
            fact TEXT NOT NULL,
            confidence REAL DEFAULT 0.8,
            base_confidence REAL DEFAULT 0.8,
            project TEXT DEFAULT 'global',
            source TEXT DEFAULT 'observation',
            provenance TEXT DEFAULT 'observation',
            impact REAL DEFAULT 0.5,
            session_id TEXT,
            created_at TEXT NOT NULL,
            last_accessed TEXT,
            access_count INTEGER DEFAULT 0,
            last_verified TEXT,
            verified_by TEXT,
            superseded_by TEXT,
            forgotten INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            related_to TEXT DEFAULT '[]',
            tier TEXT DEFAULT 'contextual'
        );
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision TEXT NOT NULL,
            reasoning TEXT,
            project TEXT DEFAULT 'global',
            session_id TEXT,
            trace TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE mistakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mistake TEXT NOT NULL,
            why TEXT,
            project TEXT DEFAULT 'global',
            confidence REAL DEFAULT 0.95,
            session_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            went_well TEXT, missed TEXT, do_differently TEXT,
            project TEXT DEFAULT 'global', session_id TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE exemplars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scenario TEXT, pete TEXT NOT NULL, atlas TEXT,
            calibration TEXT, tags TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE session_fingerprints (
            session_id TEXT PRIMARY KEY, project TEXT,
            duration_minutes REAL, facts_count INTEGER DEFAULT 0,
            decisions_count INTEGER DEFAULT 0, mistakes_count INTEGER DEFAULT 0,
            tier_dist TEXT DEFAULT '{}', topic_vector BLOB,
            outcome TEXT DEFAULT 'neutral', tags TEXT DEFAULT '[]',
            energy_arc TEXT DEFAULT '', highlights TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE probes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL, expected TEXT NOT NULL,
            fact_id TEXT, probe_type TEXT DEFAULT 'user',
            created_at TEXT NOT NULL, last_run TEXT, last_result TEXT,
            run_count INTEGER DEFAULT 0, pass_count INTEGER DEFAULT 0
        );
        CREATE TABLE decision_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, decision TEXT NOT NULL,
            reasoning TEXT, project TEXT DEFAULT 'global',
            status TEXT DEFAULT 'provisional', created_at TEXT NOT NULL
        );
        CREATE TABLE decision_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            outcome TEXT NOT NULL, success INTEGER,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE fact_embeddings (
            fact_id TEXT PRIMARY KEY,
            embedding BLOB NOT NULL, model TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE hypnos_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, started_at TEXT NOT NULL,
            stage TEXT NOT NULL, action TEXT NOT NULL,
            fact_id TEXT, detail TEXT
        );
        CREATE TABLE evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL, score REAL NOT NULL,
            metrics TEXT NOT NULL, notes TEXT DEFAULT ''
        );
        """
    )


def _multiverse_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE personalities (
            name TEXT PRIMARY KEY, dir TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'worker',
            active INTEGER DEFAULT 1, created_at TEXT NOT NULL,
            bootstrapped_from TEXT, description TEXT, focus TEXT
        );
        CREATE TABLE broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, source TEXT NOT NULL, targets TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE dreams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis TEXT NOT NULL, source_facts TEXT NOT NULL,
            confidence REAL, status TEXT DEFAULT 'open', created_at TEXT NOT NULL
        );
        CREATE TABLE xrefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE xref_facts (
            xref_id INTEGER NOT NULL, personality TEXT NOT NULL, fact_id TEXT NOT NULL
        );
        """
    )


def _put_fact(conn, fid, text, project="global", created="2026-01-01T00:00:00+00:00"):
    conn.execute(
        "INSERT OR IGNORE INTO facts (id, fact, project, created_at) VALUES (?,?,?,?)",
        (fid, text, project, created),
    )


@pytest.fixture
def synthetic_world(tmp_path: Path):
    """Build a fake ~/.null/ layout with three personalities + multiverse."""
    root = tmp_path / ".null"
    (root / "atlas").mkdir(parents=True)
    (root / "personalities" / "mercury").mkdir(parents=True)
    (root / "personalities" / "logos").mkdir(parents=True)

    atlas_db = root / "atlas" / "memory.db"
    mercury_db = root / "personalities" / "mercury" / "memory.db"
    logos_db = root / "personalities" / "logos" / "memory.db"
    mv_db = root / "multiverse.db"

    # Atlas: 3 facts, 1 decision with outcome, 1 mistake, 1 embedding
    a = sqlite3.connect(atlas_db)
    _source_schema(a)
    _put_fact(a, "shared1", "Pete is founder of Aleph Null")
    _put_fact(a, "shared2", "Sam is Samuel Jr.")
    _put_fact(a, "atlas_only", "Atlas observation about architecture")
    a.execute(
        "INSERT INTO decisions (decision, reasoning, created_at) VALUES (?,?,?)",
        ("unify memory substrate", "fragmentation is real", "2026-04-17T00:00:00+00:00"),
    )
    a.execute(
        "INSERT INTO decision_outcomes (decision_id, outcome, success, recorded_at) "
        "VALUES (1, 'shipped', 1, '2026-04-17T01:00:00+00:00')"
    )
    a.execute(
        "INSERT INTO mistakes (mistake, why, created_at) VALUES (?,?,?)",
        ("forgot to call null_observe", "overconfidence", "2026-04-01T00:00:00+00:00"),
    )
    a.execute(
        "INSERT INTO fact_embeddings (fact_id, embedding, model, created_at) "
        "VALUES ('shared1', ?, 'test-model', ?)",
        (b"\x00" * 16, "2026-04-01T00:00:00+00:00"),
    )
    # Orphan embedding — fact does not exist in Atlas
    a.execute(
        "INSERT INTO fact_embeddings (fact_id, embedding, model, created_at) "
        "VALUES ('ghost', ?, 'test-model', ?)",
        (b"\x00" * 16, "2026-04-01T00:00:00+00:00"),
    )
    a.commit()
    a.close()

    # Mercury: shares shared1, has its own
    m = sqlite3.connect(mercury_db)
    _source_schema(m)
    _put_fact(m, "shared1", "Pete is founder of Aleph Null")
    _put_fact(m, "mercury_only", "X growth: Morning slot hits 2k imp")
    m.commit()
    m.close()

    # Logos: shares shared2, has its own
    l = sqlite3.connect(logos_db)
    _source_schema(l)
    _put_fact(l, "shared2", "Sam is Samuel Jr.")
    _put_fact(l, "logos_only", "Analyst: market structure thesis")
    l.commit()
    l.close()

    # Multiverse: registry + 1 broadcast + 1 dream + 1 xref
    mv = sqlite3.connect(mv_db)
    _multiverse_schema(mv)
    for name, role in [("atlas", "manager"), ("mercury", "worker"), ("logos", "worker")]:
        mv.execute(
            "INSERT INTO personalities (name, dir, role, created_at) VALUES (?,?,?,?)",
            (name, str(root / name), role, "2026-03-24T00:00:00+00:00"),
        )
    mv.execute(
        "INSERT INTO broadcasts (event, source, targets, created_at) "
        "VALUES ('test', 'atlas', 'all', '2026-04-01T00:00:00+00:00')"
    )
    mv.execute(
        "INSERT INTO dreams (hypothesis, source_facts, confidence, created_at) "
        "VALUES ('Pete dreams of quitting job', '[]', 0.9, '2026-04-01T00:00:00+00:00')"
    )
    mv.execute(
        "INSERT INTO xrefs (event, created_at) VALUES ('cross_reference', '2026-04-01T00:00:00+00:00')"
    )
    mv.execute(
        "INSERT INTO xref_facts (xref_id, personality, fact_id) VALUES (1, 'atlas', 'shared1')"
    )
    mv.commit()
    mv.close()

    return {
        "root": root,
        "sources": [
            ("atlas", str(atlas_db)),
            ("mercury", str(mercury_db)),
            ("logos", str(logos_db)),
        ],
        "target": str(root / "unified.db"),
        "multiverse": str(mv_db),
    }


# ── Tests ──────────────────────────────────────────────────────────────────


def test_migrate_dedupes_facts_by_id(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    stats = migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    # 5 unique ids: shared1, shared2, atlas_only, mercury_only, logos_only
    assert stats.facts_inserted == 5
    # shared1 (atlas→mercury) and shared2 (atlas→logos) each merge once
    assert stats.facts_merged == 2


def test_overlay_has_one_row_per_personality_per_fact(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    conn = sqlite3.connect(synthetic_world["target"])
    counts = dict(conn.execute(
        "SELECT personality, COUNT(*) FROM personality_views GROUP BY personality"
    ).fetchall())
    assert counts == {"atlas": 3, "mercury": 2, "logos": 2}
    # shared1 appears in atlas + mercury
    shared1_peeps = {r[0] for r in conn.execute(
        "SELECT personality FROM personality_views WHERE fact_id='shared1'"
    ).fetchall()}
    assert shared1_peeps == {"atlas", "mercury"}
    conn.close()


def test_attribution_on_decisions_and_mistakes(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    conn = sqlite3.connect(synthetic_world["target"])
    row = conn.execute("SELECT personality, decision FROM decisions").fetchone()
    assert row == ("atlas", "unify memory substrate")
    row = conn.execute("SELECT personality, mistake FROM mistakes").fetchone()
    assert row[0] == "atlas"
    conn.close()


def test_decision_outcome_remapping(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    conn = sqlite3.connect(synthetic_world["target"])
    outcome = conn.execute(
        "SELECT o.outcome, d.decision FROM decision_outcomes o "
        "JOIN decisions d ON d.id = o.decision_id"
    ).fetchone()
    assert outcome == ("shipped", "unify memory substrate")
    conn.close()


def test_orphan_embeddings_dropped(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    stats = migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    assert any("orphan" in w for w in stats.warnings)
    conn = sqlite3.connect(synthetic_world["target"])
    ghost = conn.execute(
        "SELECT COUNT(*) FROM fact_embeddings WHERE fact_id='ghost'"
    ).fetchone()[0]
    assert ghost == 0
    # shared1 embedding survived
    real = conn.execute(
        "SELECT COUNT(*) FROM fact_embeddings WHERE fact_id='shared1'"
    ).fetchone()[0]
    assert real == 1
    conn.close()


def test_multiverse_folded_in(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    conn = sqlite3.connect(synthetic_world["target"])
    assert conn.execute("SELECT COUNT(*) FROM personalities").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM broadcasts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM dreams").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM xrefs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM xref_facts").fetchone()[0] == 1
    conn.close()


def test_idempotency_force_rerun(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    a = migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    b = migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    # Stat fields should match
    for field in ("facts_inserted", "facts_merged", "views_inserted", "decisions",
                  "mistakes", "personalities", "broadcasts", "dreams", "xrefs"):
        assert getattr(a, field) == getattr(b, field), f"{field} differs"


def test_force_rebuild_backs_up_existing_db(synthetic_world, monkeypatch):
    """--force must snapshot the existing unified DB (with intact content)
    before destroying it — it's the only copy of post-migration memory."""
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    target = synthetic_world["target"]
    root = Path(target).parent
    migrate(
        target_path=target,
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    # First run had nothing to destroy — no backup expected
    assert not list(root.glob("unified.db.bak.*"))

    # Plant a marker that only exists in the pre-rebuild DB
    conn = sqlite3.connect(target)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('backup_marker', 'precious')"
    )
    conn.commit()
    conn.close()

    migrate(
        target_path=target,
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )

    backups = [p for p in root.glob("unified.db.bak.*")
               if not p.name.endswith(("-wal", "-shm"))]
    assert len(backups) == 1
    bconn = sqlite3.connect(str(backups[0]))
    row = bconn.execute(
        "SELECT value FROM meta WHERE key='backup_marker'"
    ).fetchone()
    # Backup contains the pre-rebuild content, including real facts
    facts = bconn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    bconn.close()
    assert row == ("precious",)
    assert facts == 5
    # Rebuilt DB is fresh — marker gone
    conn = sqlite3.connect(target)
    assert conn.execute(
        "SELECT value FROM meta WHERE key='backup_marker'"
    ).fetchone() is None
    conn.close()


def test_refuses_rerun_without_force(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    with pytest.raises(RuntimeError, match="migration_complete"):
        migrate(
            target_path=synthetic_world["target"],
            force=False,
            sources=synthetic_world["sources"],
            orphan_root_db=None,
            backfill=False,
        )


def test_verify_reports_complete_state(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    out = verify(synthetic_world["target"])
    assert out["schema_version"] == str(UNIFIED_SCHEMA_VERSION)
    assert out["migration_complete"] == "1"
    assert out["facts"] == 5
    assert out["per_personality"] == {"atlas": 3, "mercury": 2, "logos": 2}
    assert out["facts_without_embedding"] == 4  # only shared1 has one in synth world
    assert out["fts_count"] == 5


def test_fts_search_works_post_migration(synthetic_world, monkeypatch):
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    conn = sqlite3.connect(synthetic_world["target"])
    rows = conn.execute(
        "SELECT f.fact FROM facts_fts JOIN facts f ON f.rowid = facts_fts.rowid "
        "WHERE facts_fts MATCH 'Sam'"
    ).fetchall()
    assert any("Sam" in r[0] for r in rows)
    conn.close()


def test_fact_id_shared_across_personalities(synthetic_world, monkeypatch):
    """shared1 should produce ONE facts row but TWO personality_views rows."""
    monkeypatch.setattr("null_memory.migrate_v3.MULTIVERSE_DB", synthetic_world["multiverse"])
    migrate(
        target_path=synthetic_world["target"],
        force=True,
        sources=synthetic_world["sources"],
        orphan_root_db=None,
        backfill=False,
    )
    conn = sqlite3.connect(synthetic_world["target"])
    assert conn.execute(
        "SELECT COUNT(*) FROM facts WHERE id='shared1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM personality_views WHERE fact_id='shared1'"
    ).fetchone()[0] == 2
    conn.close()


# ── v19: session_verifications (Phase A on-boot identity) ─────────────────


def test_v19_session_verifications_table_exists_in_fresh_db(tmp_path):
    """Fresh unified DB created by init_unified_db must include the v19
    session_verifications table — Phase A's coherence-score sink."""
    from null_memory.migrate_v3 import init_unified_db
    db_path = tmp_path / "fresh.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(session_verifications)"
        ).fetchall()}
        assert cols == {
            "id", "session_id", "personality", "boot_time",
            "coherence_score", "verified", "sample_size",
            "identity_payload_hash", "identity_model", "created_at",
        }
        # Indexes are present
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='session_verifications'"
        ).fetchall()}
        assert "idx_session_verifications_session" in idx
        assert "idx_session_verifications_personality" in idx
    finally:
        conn.close()


def test_v19_upgrade_is_idempotent_on_old_db(tmp_path):
    """A pre-v19 unified DB (no session_verifications table) gets the
    table after _apply_unified_upgrades; running upgrades again is safe."""
    from null_memory.migrate_v3 import _apply_unified_upgrades, init_unified_db
    db_path = tmp_path / "upgrade.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        # Simulate pre-v19 state by dropping the table.
        conn.execute("DROP TABLE IF EXISTS session_verifications")
        conn.commit()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='session_verifications'"
        ).fetchone()
        # Run upgrades — recreates the table.
        _apply_unified_upgrades(conn)
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='session_verifications'"
        ).fetchone()
        # Re-run is a no-op (no error).
        _apply_unified_upgrades(conn)
        conn.commit()
    finally:
        conn.close()


def test_v20_facts_has_crystallization_columns(tmp_path):
    """Crystallization lineage columns exist on a fresh DB."""
    from null_memory.migrate_v3 import init_unified_db
    db_path = tmp_path / "v20.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(facts)"
        ).fetchall()}
        assert "crystallized_from" in cols
        assert "crystallized_into" in cols
    finally:
        conn.close()


def test_v20_upgrade_is_idempotent(tmp_path):
    """A pre-v20 facts table (no crystallization cols) gets them after
    _apply_unified_upgrades; second run is a no-op."""
    from null_memory.migrate_v3 import _apply_unified_upgrades, init_unified_db
    db_path = tmp_path / "v20up.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        # Re-run upgrades (idempotency check)
        _apply_unified_upgrades(conn)
        _apply_unified_upgrades(conn)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(facts)"
        ).fetchall()}
        assert "crystallized_from" in cols
        assert "crystallized_into" in cols
    finally:
        conn.close()


def test_v21_doc_claims_table_exists(tmp_path):
    """Fresh unified DB ships with the v21 doc_claims table + indexes."""
    from null_memory.migrate_v3 import init_unified_db
    db_path = tmp_path / "v21.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(doc_claims)"
        ).fetchall()}
        assert cols == {
            "id", "source_path", "claim_text", "claim_type",
            "extracted_at", "last_verified_at", "last_seen_at",
            "status", "refute_evidence",
        }
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='doc_claims'"
        ).fetchall()}
        assert "idx_doc_claims_status" in idx
        assert "idx_doc_claims_source" in idx
    finally:
        conn.close()


def test_v21_doc_claims_unique_constraint(tmp_path):
    """(source_path, claim_text) is unique — re-extraction upserts, not duplicates."""
    from null_memory.migrate_v3 import init_unified_db
    db_path = tmp_path / "v21u.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO doc_claims(source_path, claim_text, claim_type, "
            "extracted_at, last_seen_at) "
            "VALUES ('/tmp/x.md', 'phase 7 shipped', 'ship_status', "
            "'2026-04-29', '2026-04-29')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO doc_claims(source_path, claim_text, claim_type, "
                "extracted_at, last_seen_at) "
                "VALUES ('/tmp/x.md', 'phase 7 shipped', 'ship_status', "
                "'2026-04-29', '2026-04-29')"
            )
    finally:
        conn.close()


def test_v19_session_verifications_round_trip(tmp_path):
    """Insert + read back a verification row to confirm the schema is usable."""
    from null_memory.migrate_v3 import init_unified_db
    db_path = tmp_path / "rt.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO session_verifications
               (session_id, personality, boot_time, coherence_score,
                verified, sample_size, identity_payload_hash,
                identity_model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("sess_x", "atlas", "2026-04-29T00:00:00+00:00",
             0.92, 1, 47, "deadbeef", "BAAI/bge-small-en-v1.5",
             "2026-04-29T00:00:00+00:00"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT coherence_score, verified, sample_size FROM session_verifications "
            "WHERE session_id='sess_x'"
        ).fetchone()
        assert row == (0.92, 1, 47)
    finally:
        conn.close()
