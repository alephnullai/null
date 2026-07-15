"""Registry authority + derived seat directories (issue #23).

multiverse.db syncs across machines inside the hub store repo, but its
``personalities.dir`` column stored machine-absolute paths — every
registered dir became a dead path on the other OS. The decision under
test:

  * the unified store's ``personalities`` table is AUTHORITATIVE for who
    exists (portable — no paths);
  * seat dirs are DERIVED at read time from the local hub base by
    convention (resolve_personality_dir);
  * multiverse.db is legacy/compat — its dir column is a hint only, new
    rows store hub-relative dirs, absolute rows are relativized when
    touched, dead paths fall back to the derived conventional location;
  * listing is the UNION of both registries.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from null_memory.migrate_v3 import init_unified_db
from null_memory.multiverse import (
    MultiverseManager,
    resolve_personality_dir,
)


@pytest.fixture
def hub(tmp_path):
    d = str(tmp_path / "hub")
    os.makedirs(d)
    return d


@pytest.fixture
def mv(hub):
    manager = MultiverseManager(base_dir=hub)
    yield manager
    manager.close()


def _seed_unified(hub: str, *names_roles) -> str:
    """Create <hub>/unified.db with the unified layout and the given
    (name, role) registry rows."""
    path = os.path.join(hub, "unified.db")
    conn = init_unified_db(path)
    try:
        for name, role in names_roles:
            conn.execute(
                "INSERT OR REPLACE INTO personalities "
                "(name, role, active, created_at) VALUES (?, ?, 1, ?)",
                (name, role, "2026-06-11T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _mv_row(hub: str, name: str):
    conn = sqlite3.connect(os.path.join(hub, "multiverse.db"))
    try:
        return conn.execute(
            "SELECT * FROM personalities WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()


# ── resolve_personality_dir (the single derivation convention) ────────────


class TestResolvePersonalityDir:
    def test_primary_layout(self, hub):
        os.makedirs(os.path.join(hub, "atlas"))
        assert resolve_personality_dir(hub, "atlas") == \
            os.path.join(hub, "atlas")

    def test_worker_layout(self, hub):
        os.makedirs(os.path.join(hub, "personalities", "logos"))
        assert resolve_personality_dir(hub, "logos") == \
            os.path.join(hub, "personalities", "logos")

    def test_primary_wins_over_worker(self, hub):
        os.makedirs(os.path.join(hub, "athena"))
        os.makedirs(os.path.join(hub, "personalities", "athena"))
        assert resolve_personality_dir(hub, "athena") == \
            os.path.join(hub, "athena")

    def test_flat_pre_migration_fallback(self, hub):
        with open(os.path.join(hub, "memory.db"), "w") as f:
            f.write("")
        assert resolve_personality_dir(hub, "atlas") == hub

    def test_nonexistent_returns_worker_convention(self, hub):
        assert resolve_personality_dir(hub, "newbie") == \
            os.path.join(hub, "personalities", "newbie")


# ── multiverse.db: relative dirs going forward ─────────────────────────────


class TestRelativeRegistration:
    def test_register_stores_hub_relative_dir(self, mv, hub):
        info = mv.register("steve", role="worker")
        # returned dir is absolute for callers...
        assert info["dir"] == os.path.join(hub, "personalities", "steve")
        # ...but the synced row is hub-relative with forward slashes
        row = _mv_row(hub, "steve")
        assert row[1] == "personalities/steve"

    def test_register_atlas_relative(self, mv, hub):
        mv.register("atlas", role="manager")
        assert _mv_row(hub, "atlas")[1] == "atlas"

    def test_relative_dir_resolves_against_local_base(self, mv, hub):
        os.makedirs(os.path.join(hub, "personalities", "steve"))
        mv.register("steve")
        info = mv.get_personality_info("steve")
        assert info["dir"] == os.path.join(hub, "personalities", "steve")


class TestDeadAbsolutePathFallback:
    """A foreign machine's multiverse.db row (macOS path on Windows or
    vice versa) must self-heal to the derived conventional path."""

    def _plant_foreign_row(self, hub: str, name: str, foreign_dir: str):
        conn = sqlite3.connect(os.path.join(hub, "multiverse.db"))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO personalities "
                "(name, dir, role, active, created_at) "
                "VALUES (?, ?, 'worker', 1, '2026-06-11T00:00:00+00:00')",
                (name, foreign_dir),
            )
            conn.commit()
        finally:
            conn.close()

    def test_dead_posix_absolute_falls_back_to_derived(self, mv, hub):
        local_dir = os.path.join(hub, "personalities", "logos")
        os.makedirs(local_dir)
        self._plant_foreign_row(
            hub, "logos", "/nonexistent-foreign-machine/.null/personalities/logos")

        info = mv.get_personality_info("logos")
        assert info is not None
        assert info["dir"] == local_dir

    def test_dead_windows_absolute_falls_back_to_derived(self, mv, hub):
        local_dir = os.path.join(hub, "personalities", "athena")
        os.makedirs(local_dir)
        # The foreign path must be Windows-shaped AND guaranteed dead on
        # every machine. The original literal was the REAL athena seat
        # path — alive on the very machine the test describes, so the
        # fallback never triggered there and the test failed (issue #42).
        self._plant_foreign_row(
            hub, "athena",
            r"C:\Users\nobody-9f3a1c\.null\personalities\athena")

        info = mv.get_personality_info("athena")
        assert info is not None
        assert info["dir"] == local_dir

    def test_touched_row_is_relativized(self, mv, hub):
        os.makedirs(os.path.join(hub, "personalities", "logos"))
        self._plant_foreign_row(
            hub, "logos", "/nonexistent-foreign-machine/.null/personalities/logos")

        mv.get_personality_info("logos")  # touch

        row = _mv_row(hub, "logos")
        assert row[1] == "personalities/logos"

    def test_live_absolute_inside_hub_is_relativized(self, mv, hub):
        local_dir = os.path.join(hub, "personalities", "mercury")
        os.makedirs(local_dir)
        self._plant_foreign_row(hub, "mercury", local_dir)  # absolute, alive

        info = mv.get_personality_info("mercury")
        assert info["dir"] == local_dir
        assert _mv_row(hub, "mercury")[1] == "personalities/mercury"

    def test_recall_follows_derived_dir(self, mv, hub):
        """The actual failure mode from the issue: multiverse recall
        loading a worker's AgentMemory through a dead stored dir."""
        from null_memory.agent import AgentMemory

        local_dir = os.path.join(hub, "personalities", "logos")
        mem = AgentMemory.load(local_dir, personality="logos")
        mem.learn("hiwave uses CMake presets", confidence=0.9)
        mem.db.close()

        self._plant_foreign_row(
            hub, "logos", "/nonexistent-foreign-machine/.null/personalities/logos")

        results = mv.recall("CMake presets", personalities=["logos"])
        assert results, "recall must follow the derived conventional dir"
        assert results[0]["_personality"] == "logos"


# ── Authoritative unified registry + union listing ─────────────────────────


class TestUnifiedRegistryAuthority:
    def test_register_writes_through_to_unified(self, hub):
        _seed_unified(hub)
        mv = MultiverseManager(base_dir=hub)
        try:
            mv.register("athena", role="worker", focus="hiwave-windows")
        finally:
            mv.close()
        conn = sqlite3.connect(os.path.join(hub, "unified.db"))
        try:
            row = conn.execute(
                "SELECT role, focus, active FROM personalities "
                "WHERE name='athena'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "worker"
        assert row[1] == "hiwave-windows"
        assert row[2] == 1

    def test_unified_table_carries_no_dir_column(self, hub):
        """The authoritative registry is portable by construction."""
        _seed_unified(hub)
        conn = sqlite3.connect(os.path.join(hub, "unified.db"))
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(personalities)")}
        finally:
            conn.close()
        assert "dir" not in cols

    def test_list_is_union_of_both_registries(self, hub):
        # athena exists ONLY in the unified table (e.g. row synced from
        # another machine's registration — the issue #22 companion case)
        _seed_unified(hub, ("athena", "worker"))
        mv = MultiverseManager(base_dir=hub)
        try:
            # logos exists ONLY in legacy multiverse.db
            mv.db.conn.execute(
                "INSERT INTO personalities (name, dir, role, active, created_at) "
                "VALUES ('logos', 'personalities/logos', 'worker', 1, "
                "'2026-06-11T00:00:00+00:00')")
            mv.db.conn.commit()

            names = {p["name"] for p in mv.list_personalities()}
            assert names == {"athena", "logos"}

            # every entry carries a locally-derived absolute dir
            for p in mv.list_personalities():
                assert os.path.isabs(p["dir"])
        finally:
            mv.close()

    def test_unified_fields_win_over_legacy(self, hub):
        _seed_unified(hub, ("logos", "manager"))
        mv = MultiverseManager(base_dir=hub)
        try:
            mv.db.conn.execute(
                "INSERT INTO personalities (name, dir, role, active, created_at) "
                "VALUES ('logos', 'personalities/logos', 'worker', 1, "
                "'2026-06-11T00:00:00+00:00')")
            mv.db.conn.commit()
            info = mv.get_personality_info("logos")
            assert info["role"] == "manager"  # authoritative
        finally:
            mv.close()

    def test_unified_inactive_wins(self, hub):
        path = _seed_unified(hub, ("logos", "worker"))
        conn = sqlite3.connect(path)
        conn.execute("UPDATE personalities SET active=0 WHERE name='logos'")
        conn.commit()
        conn.close()
        mv = MultiverseManager(base_dir=hub)
        try:
            mv.db.conn.execute(
                "INSERT INTO personalities (name, dir, role, active, created_at) "
                "VALUES ('logos', 'personalities/logos', 'worker', 1, "
                "'2026-06-11T00:00:00+00:00')")
            mv.db.conn.commit()
            assert mv.get_personality_info("logos") is None
            assert "logos" not in {
                p["name"] for p in mv.list_personalities()}
        finally:
            mv.close()


# ── handlers: null_multiverse list shows the union ─────────────────────────


def test_handler_multiverse_list_shows_union(tmp_path, monkeypatch):
    from null_memory.mcp.handlers import NullHandlers

    hub = str(tmp_path / "hub")
    os.makedirs(hub)
    monkeypatch.setenv("NULL_DIR", hub)

    _seed_unified(hub, ("athena", "worker"))
    mv = MultiverseManager(base_dir=hub)
    try:
        mv.db.conn.execute(
            "INSERT INTO personalities (name, dir, role, active, created_at) "
            "VALUES ('logos', 'personalities/logos', 'worker', 1, "
            "'2026-06-11T00:00:00+00:00')")
        mv.db.conn.commit()
    finally:
        mv.close()

    handlers = NullHandlers(agent_dir=str(tmp_path / "store"))
    out = handlers.handle_multiverse_list()
    assert "athena" in out  # visible from the unified registry alone
    assert "logos" in out   # visible from legacy multiverse.db alone
