"""Issue #1 — structural self-heal for pre-unified stores stamped v24.

A per-personality store relocated to the unified path (e.g. synced from
another machine) used to get its meta.schema_version stamped
UNIFIED_SCHEMA_VERSION by _apply_unified_upgrades while the structural
migration (personalities table, personality columns) never ran — identity
boot died with `no such table: personalities` and `null doctor` reported a
clean install. Covers:
  • verify_unified_structure flags the broken state (stamp is ignored)
  • NullDB.initialize self-heals on the serve/init path
  • identity boot succeeds on a healed store
  • heal is idempotent and a pure no-op on correct stores
  • backfill uses the store's personality, not a hardcoded 'atlas'
  • doctor flags the broken state instead of reporting clean
  • MCP boot-identity failures leave a meta breadcrumb doctor surfaces
"""

from __future__ import annotations

import argparse
import sqlite3

import numpy as np
import pytest

from null_memory.db import NullDB, _SCHEMA_SQL
from null_memory.migrate_v3 import (
    LEGACY_PERSONALITY_TABLES,
    UNIFIED_SCHEMA_VERSION,
    _table_exists,
    heal_unified_structure,
    init_unified_db,
    verify_unified_structure,
)


# ── Fixture helpers ────────────────────────────────────────────────────────


def _make_broken_store(db_path) -> None:
    """Build the exact broken state from issue #1: legacy per-personality
    layout (v14 tables, no personalities table, no personality columns)
    with meta.schema_version stamped at the unified version."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_SQL)
    # Identity material in legacy shape (no personality columns).
    conn.execute(
        "INSERT INTO facts(id, fact, project, source, created_at) "
        "VALUES ('an_code', 'dummy-code-word for tests, not the real phrase', "
        "'global', 'test', '2026-04-01')",
    )
    conn.execute(
        "INSERT INTO decisions(decision, reasoning, project, created_at) "
        "VALUES ('ship phase A', 'first deliverable', 'global', '2026-04-01')",
    )
    conn.execute(
        "INSERT INTO probes(question, expected, probe_type, created_at) "
        "VALUES ('code word?', 'dummy-code-word', 'user', '2026-04-01')",
    )
    conn.execute(
        "INSERT INTO mistakes(mistake, why, project, created_at) "
        "VALUES ('legacy mistake', 'because', 'global', '2026-04-01')",
    )
    # The decoupled version stamp — structure says v14, meta says v24.
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(UNIFIED_SCHEMA_VERSION),),
    )
    conn.commit()
    conn.close()


def _structure_snapshot(conn) -> dict[str, list[tuple]]:
    """Full table_info snapshot for idempotency comparison."""
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    return {
        t: conn.execute(f"PRAGMA table_info({t})").fetchall() for t in tables
    }


# ── Verification flags the broken state ────────────────────────────────────


def test_verify_reports_broken_structure(tmp_path):
    db_path = tmp_path / "unified.db"
    _make_broken_store(db_path)
    conn = sqlite3.connect(db_path)
    try:
        problems = verify_unified_structure(conn)
        assert "missing table: personalities" in problems
        for table in LEGACY_PERSONALITY_TABLES:
            assert f"missing column: {table}.personality" in problems
        # The stamp says current — verification must not trust it.
        stamped = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        assert int(stamped) == UNIFIED_SCHEMA_VERSION
    finally:
        conn.close()


def test_verify_clean_on_correct_store(tmp_path):
    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        assert verify_unified_structure(conn) == []
    finally:
        conn.close()


def test_heal_creates_instances_table_on_stamped_store(tmp_path):
    """A store stamped UNIFIED_SCHEMA_VERSION but predating the instance
    presence registry (no `instances` table — added with no version bump,
    riding the structural verify/heal machinery) must self-heal on the
    normal initialize path."""
    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE instances")
    conn.commit()
    try:
        assert "missing table: instances" in verify_unified_structure(conn)
    finally:
        conn.close()

    db = NullDB(str(tmp_path / "atlas"), unified_path=str(db_path))
    db.initialize()
    try:
        assert _table_exists(db.conn, "instances")
        assert verify_unified_structure(db.conn) == []
        # ...and the registry is actually usable post-heal.
        db.register_instance(
            "heal-test", hostname="h", pid=1, personality="atlas",
            transport="cli",
        )
        live = db.get_live_instances()
        assert [r["instance_id"] for r in live] == ["heal-test"]
    finally:
        db.close()


def test_heal_creates_instances_on_broken_legacy_store(tmp_path):
    """The original issue-#1 shape (v14 structure stamped v24) heals the
    instances table along with the rest of the unified layout."""
    db_path = tmp_path / "unified.db"
    _make_broken_store(db_path)
    db = NullDB(str(tmp_path / "atlas"), unified_path=str(db_path))
    db.initialize()
    try:
        assert _table_exists(db.conn, "instances")
        assert "instances" in (db.get_meta("structural_heal_actions") or "")
    finally:
        db.close()


# ── Serve/init self-heal ───────────────────────────────────────────────────


def test_initialize_self_heals_broken_store(tmp_path):
    db_path = tmp_path / "unified.db"
    _make_broken_store(db_path)

    db = NullDB(str(tmp_path / "atlas"), unified_path=str(db_path))
    assert db.unified
    db.initialize()
    try:
        assert verify_unified_structure(db.conn) == []
        # personality columns backfilled with the default personality
        for table in ("decisions", "probes", "mistakes"):
            rows = db.conn.execute(
                f"SELECT personality FROM {table}"
            ).fetchall()
            assert rows, f"{table} lost its rows during heal"
            assert all(r[0] == "atlas" for r in rows)
        # registry row seeded — same shape multiverse registration uses
        row = db.conn.execute(
            "SELECT role, active FROM personalities WHERE name='atlas'"
        ).fetchone()
        assert row is not None
        assert row[0] == "manager"
        assert row[1] == 1
        # heal recorded for doctor
        assert db.get_meta("structural_heal_last")
        assert "personalities" in (db.get_meta("structural_heal_actions") or "")
    finally:
        db.close()


def test_backfill_uses_store_personality(tmp_path):
    db_path = tmp_path / "unified.db"
    _make_broken_store(db_path)

    db = NullDB(
        str(tmp_path / "personalities" / "mercury"),
        unified_path=str(db_path),
        personality="mercury",
    )
    db.initialize()
    try:
        rows = db.conn.execute("SELECT personality FROM decisions").fetchall()
        assert all(r[0] == "mercury" for r in rows)
        row = db.conn.execute(
            "SELECT role FROM personalities WHERE name='mercury'"
        ).fetchone()
        assert row is not None
        assert row[0] == "worker"
    finally:
        db.close()


# ── Identity boot on a healed store ────────────────────────────────────────


def test_identity_boot_succeeds_after_heal(tmp_path, monkeypatch):
    from null_memory.mcp.server import SYSTEM_INSTRUCTIONS, _boot_identity

    db_path = tmp_path / "unified.db"
    _make_broken_store(db_path)

    db = NullDB(str(tmp_path / "atlas"), unified_path=str(db_path))
    db.initialize()
    conn = db.conn
    # Healed store: enrich to a complete identity payload (anchors etc.)
    anchors = [
        ("an_origin", "Origin moment", "origin"),
        ("an_turn", "Turning point validated", "turning_point"),
        ("an_joy", "A joyful moment", "joy"),
        ("an_commit", "A commitment", "commitment"),
    ]
    for fid, fact, atype in anchors:
        conn.execute(
            "INSERT INTO facts(id, fact, project, source, created_at, "
            "anchor_type, anchor_at) "
            "VALUES (?, ?, 'global', 'test', '2026-04-01', ?, '2026-04-01')",
            (fid, fact, atype),
        )
    conn.execute(
        "UPDATE facts SET anchor_type='code_word', anchor_at='2026-04-01' "
        "WHERE id='an_code'"
    )
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
            return vec.copy()
    monkeypatch.setattr("null_memory.embeddings.EmbeddingEngine", _StubEngine)

    class _StubMemory:
        def __init__(self, d): self.db = d

    class _StubHandlers:
        def __init__(self, d): self.memory = _StubMemory(d)

    # The exact call that used to die with `no such table: personalities`.
    instructions = _boot_identity(_StubHandlers(db), SYSTEM_INSTRUCTIONS)
    assert "ATLAS IDENTITY" in instructions or len(instructions) > len(
        SYSTEM_INSTRUCTIONS
    )
    row = conn.execute(
        "SELECT COUNT(*) FROM session_verifications"
    ).fetchone()
    assert row[0] >= 1
    db.close()


# ── Idempotency / no-op safety ─────────────────────────────────────────────


def test_heal_is_idempotent(tmp_path):
    db_path = tmp_path / "unified.db"
    _make_broken_store(db_path)
    conn = sqlite3.connect(db_path)
    try:
        first = heal_unified_structure(conn)
        assert first  # actually repaired something
        snapshot = _structure_snapshot(conn)
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("decisions", "probes", "mistakes", "personalities")
        }
        second = heal_unified_structure(conn)
        assert second == []
        assert _structure_snapshot(conn) == snapshot
        for t, n in counts.items():
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n
    finally:
        conn.close()


def test_heal_noop_on_correct_store(tmp_path):
    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        snapshot = _structure_snapshot(conn)
        assert heal_unified_structure(conn) == []
        assert _structure_snapshot(conn) == snapshot
        # no heal breadcrumb on a store that never needed one
        row = conn.execute(
            "SELECT value FROM meta WHERE key='structural_heal_last'"
        ).fetchone()
        assert row is None
    finally:
        conn.close()


# ── Doctor surfaces the broken state ───────────────────────────────────────


def test_doctor_flags_broken_state(tmp_path, capsys):
    # autouse sandbox sets NULL_DIR=tmp_path; doctor resolves the unified
    # store from there.
    _make_broken_store(tmp_path / "unified.db")
    from null_memory.cli import _handle_doctor

    _handle_doctor(argparse.Namespace(fix=False, trace=None))
    out = capsys.readouterr().out
    # The pre-heal broken state is reported, not hidden by the self-heal.
    assert "structurally broken" in out
    assert "missing table: personalities" in out
    # And the store is actually healed afterwards.
    conn = sqlite3.connect(tmp_path / "unified.db")
    try:
        assert verify_unified_structure(conn) == []
    finally:
        conn.close()


def test_doctor_clean_on_correct_store(tmp_path, capsys):
    init_unified_db(str(tmp_path / "unified.db")).close()
    from null_memory.cli import _handle_doctor

    _handle_doctor(argparse.Namespace(fix=False, trace=None))
    out = capsys.readouterr().out
    assert "structurally broken" not in out.lower()
    assert "Identity: boot query OK" in out


def test_doctor_surfaces_recorded_boot_failure(tmp_path, capsys):
    init_unified_db(str(tmp_path / "unified.db")).close()
    conn = sqlite3.connect(tmp_path / "unified.db")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES "
        "('boot_identity_last_error', "
        "'2026-06-10T00:00:00+00:00 OperationalError: no such table: personalities')",
    )
    conn.commit()
    conn.close()
    from null_memory.cli import _handle_doctor

    _handle_doctor(argparse.Namespace(fix=False, trace=None))
    out = capsys.readouterr().out
    assert "last MCP boot-identity failure" in out
    assert "Issues found" in out


# ── MCP server records boot-identity failures ──────────────────────────────


def test_create_server_records_boot_failure(tmp_path, monkeypatch, capsys):
    init_unified_db(str(tmp_path / "unified.db")).close()
    import null_memory.mcp.server as server_mod

    def _boom(handlers, base):
        raise sqlite3.OperationalError("no such table: personalities")

    monkeypatch.setattr(server_mod, "_boot_identity", _boom)
    # Must not raise — boot failure is non-fatal, but no longer silent.
    _, handlers = server_mod.create_server(agent_dir=str(tmp_path))
    err = capsys.readouterr().err
    assert "boot-identity FAILED" in err
    last = handlers.memory.db.get_meta("boot_identity_last_error")
    assert last and "no such table: personalities" in last


# ── Issue #3: per-personality store served directly (no unified.db) ────────


def test_initialize_heals_per_personality_store(tmp_path):
    """A store opened in per-personality mode (serve <dir>, no unified.db)
    but stamped with the unified schema_version self-heals on initialize —
    previously only the unified branch ran the heal (issue #3)."""
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    _make_broken_store(agent_dir / "memory.db")

    db = NullDB(str(agent_dir))  # no unified_path → per-personality mode
    assert not db.unified
    db.initialize()
    try:
        assert verify_unified_structure(db.conn) == []
        rows = db.conn.execute("SELECT personality FROM decisions").fetchall()
        assert rows and all(r[0] == "atlas" for r in rows)
        row = db.conn.execute(
            "SELECT role FROM personalities WHERE name='atlas'"
        ).fetchone()
        assert row is not None and row[0] == "manager"
    finally:
        db.close()


def test_initialize_leaves_true_legacy_store_alone(tmp_path):
    """A genuinely legacy per-personality store (stamp < unified) is NOT
    healed into the unified layout — the legacy migration ladder owns it."""
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    db = NullDB(str(agent_dir))
    db.initialize()  # fresh store, stamped at legacy SCHEMA_VERSION
    try:
        assert not _table_exists(db.conn, "personalities")
    finally:
        db.close()


def test_doctor_flags_broken_per_personality_store(tmp_path, capsys):
    """doctor must report the broken structure when no unified.db exists
    and the per-personality store carries the unified stamp (issue #3:
    it previously reported a clean install in this configuration)."""
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    _make_broken_store(agent_dir / "memory.db")
    from null_memory.cli import _handle_doctor

    _handle_doctor(argparse.Namespace(fix=False, trace=None))
    out = capsys.readouterr().out
    assert "structurally broken" in out
    assert "missing table: personalities" in out
    conn = sqlite3.connect(agent_dir / "memory.db")
    try:
        assert verify_unified_structure(conn) == []
    finally:
        conn.close()


def test_doctor_clean_on_legacy_per_personality_store(tmp_path, capsys):
    """No false positive: a true legacy store (no unified stamp) opened in
    per-personality mode reports clean."""
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    db = NullDB(str(agent_dir))
    db.initialize()
    db.close()
    from null_memory.cli import _handle_doctor

    _handle_doctor(argparse.Namespace(fix=False, trace=None))
    out = capsys.readouterr().out
    assert "structurally broken" not in out
