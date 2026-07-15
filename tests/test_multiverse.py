"""Tests for Null Multiverse — multi-personality memory architecture."""

import json
import os
import shutil

import pytest

from null_memory.agent import AgentMemory
from null_memory.multiverse import MultiverseManager, MultiverseDB


@pytest.fixture
def base_dir(tmp_path):
    """Create a base directory simulating ~/.null/ with flat layout."""
    d = str(tmp_path / "null")
    os.makedirs(d)
    return d


@pytest.fixture
def mv(base_dir):
    """Fresh MultiverseManager."""
    manager = MultiverseManager(base_dir=base_dir)
    yield manager
    manager.close()


@pytest.fixture
def flat_null(tmp_path):
    """Simulate a pre-migration flat ~/.null/ directory with real data."""
    d = str(tmp_path / "null_flat")
    os.makedirs(d)

    # Create a real AgentMemory in the flat layout
    mem = AgentMemory.load(d)
    mem.set_name("Atlas")
    mem.learn("Test fact alpha", confidence=0.9, project="global")
    mem.learn("Test fact beta", confidence=0.8, project="arbe4")
    mem.decide("Use SQLite", "Better than JSONL for concurrent access")
    mem.db.close()

    # Create state/momentum files
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump({"energy": "high"}, f)
    with open(os.path.join(d, "momentum.json"), "w") as f:
        json.dump({"active_project": "null"}, f)
    with open(os.path.join(d, "simmering.jsonl"), "w") as f:
        f.write("")

    os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    os.makedirs(os.path.join(d, "projects"), exist_ok=True)

    return d


class TestMultiverseDB:
    def test_initialize_creates_tables(self, base_dir):
        db = MultiverseDB(base_dir)
        db.initialize()
        tables = {row[0] for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "personalities" in tables
        assert "xrefs" in tables
        assert "xref_facts" in tables
        assert "broadcasts" in tables
        assert "dreams" in tables
        db.close()

    def test_wal_mode_enabled(self, base_dir):
        db = MultiverseDB(base_dir)
        db.initialize()
        mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        db.close()


class TestPersonalityRegistration:
    def test_register_personality(self, mv):
        info = mv.register("logos", role="worker", description="Logic brain", focus="analysis")
        assert info["name"] == "logos"
        assert info["role"] == "worker"
        assert info["focus"] == "analysis"

    def test_list_personalities(self, mv):
        mv.register("atlas", role="manager")
        mv.register("logos", role="worker")
        personalities = mv.list_personalities()
        names = {p["name"] for p in personalities}
        assert names == {"atlas", "logos"}

    def test_get_personality_info(self, mv):
        mv.register("logos", role="worker", focus="markets")
        info = mv.get_personality_info("logos")
        assert info is not None
        assert info["focus"] == "markets"

    def test_get_nonexistent_personality(self, mv):
        assert mv.get_personality_info("nonexistent") is None

    def test_archive_personality(self, mv):
        mv.register("logos", role="worker")
        assert mv.archive("logos")
        # Should not appear in active list
        personalities = mv.list_personalities()
        assert len(personalities) == 0
        # Should appear with include_inactive
        all_p = mv.list_personalities(include_inactive=True)
        assert len(all_p) == 1

    def test_cannot_archive_atlas(self, mv):
        mv.register("atlas", role="manager")
        with pytest.raises(ValueError, match="Cannot archive"):
            mv.archive("atlas")

    def test_delete_personality(self, mv, base_dir):
        mv.register("logos", role="worker",
                     directory=os.path.join(base_dir, "personalities", "logos"))
        assert mv.delete("logos")
        assert mv.get_personality_info("logos") is None

    def test_cannot_delete_atlas(self, mv):
        mv.register("atlas", role="manager")
        with pytest.raises(ValueError, match="Cannot delete"):
            mv.delete("atlas")


class TestPersonalityCreation:
    def test_create_basic_personality(self, mv):
        info = mv.create("logos", role="worker", focus="logic and markets")
        assert info["name"] == "logos"
        assert os.path.isdir(info["dir"])
        assert os.path.isfile(os.path.join(info["dir"], "identity.json"))
        assert os.path.isfile(os.path.join(info["dir"], "memory.db"))
        assert os.path.isfile(os.path.join(info["dir"], "state.json"))

    def test_create_personality_registers_it(self, mv):
        mv.create("logos", role="worker")
        info = mv.get_personality_info("logos")
        assert info is not None
        assert info["role"] == "worker"

    def test_cannot_create_atlas(self, mv):
        with pytest.raises(ValueError, match="reserved"):
            mv.create("atlas")

    def test_cannot_create_duplicate(self, mv):
        mv.create("logos")
        with pytest.raises(ValueError, match="already exists"):
            mv.create("logos")

    def test_create_with_bootstrap(self, mv, base_dir):
        # Create atlas with data first
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        atlas_mem = AgentMemory.load(atlas_dir)
        atlas_mem.learn("Fact from atlas", confidence=0.9, project="global")
        atlas_mem.learn("Arbe4 uses Polymarket", confidence=0.85, project="arbe4")
        atlas_mem.db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)

        # Bootstrap logos from atlas
        info = mv.create("logos", bootstrap_from="atlas")
        assert info["bootstrapped_facts"] == 2

        # Verify bootstrapped facts have reduced confidence
        logos_mem = AgentMemory.load(info["dir"])
        facts = logos_mem.knowledge
        assert len(facts) == 2
        for f in facts:
            assert f["confidence"] <= 0.6
            assert f["provenance"] == "bootstrap"
        logos_mem.db.close()

    def test_create_with_seed_filter(self, mv, base_dir):
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        atlas_mem = AgentMemory.load(atlas_dir)
        atlas_mem.learn("Global fact", confidence=0.9, project="global")
        atlas_mem.learn("Arbe4 fact", confidence=0.85, project="arbe4")
        atlas_mem.learn("Aleph fact", confidence=0.8, project="aleph")
        atlas_mem.db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)

        info = mv.create("logos", bootstrap_from="atlas", seed_filter="project:arbe4")
        assert info["bootstrapped_facts"] == 1

    def test_identity_json_created(self, mv):
        mv.create("mnemosyne", role="worker", focus="narrative memory",
                   description="Episodic personality")
        info = mv.get_personality_info("mnemosyne")
        with open(os.path.join(info["dir"], "identity.json")) as f:
            identity = json.load(f)
        assert identity["name"] == "mnemosyne"
        assert identity["focus"] == "narrative memory"


class TestBroadcast:
    def test_broadcast_to_workers(self, mv, base_dir):
        # Setup atlas and logos
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        AgentMemory.load(atlas_dir).db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)
        mv.create("logos", role="worker")

        result = mv.broadcast("Market crashed today", source="atlas")
        assert "logos" in result["targets"]
        assert result["xref_id"] is not None
        assert len(result["fact_ids"]) > 0

    def test_broadcast_to_specific_targets(self, mv, base_dir):
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        AgentMemory.load(atlas_dir).db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)
        mv.create("logos")
        mv.create("mnemosyne")

        result = mv.broadcast("Test event", source="atlas", targets=["logos"])
        assert result["targets"] == ["logos"]

    def test_broadcast_creates_xref(self, mv, base_dir):
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        AgentMemory.load(atlas_dir).db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)
        mv.create("logos")

        result = mv.broadcast("Test event", source="atlas")
        xref_id = result["xref_id"]

        # Verify xref exists in DB
        xref = mv.db.conn.execute(
            "SELECT * FROM xrefs WHERE id = ?", (xref_id,)
        ).fetchone()
        assert xref is not None
        assert xref["event"] == "Test event"

    def test_broadcast_logged(self, mv, base_dir):
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        AgentMemory.load(atlas_dir).db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)
        mv.create("logos")

        mv.broadcast("Test event", source="atlas")
        broadcasts = mv.db.conn.execute("SELECT * FROM broadcasts").fetchall()
        assert len(broadcasts) == 1
        assert broadcasts[0]["source"] == "atlas"


class TestCrossRecall:
    def test_recall_across_personalities(self, mv, base_dir):
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        atlas_mem = AgentMemory.load(atlas_dir)
        atlas_mem.learn("Atlas knows about Python", confidence=0.9, project="global")
        atlas_mem.db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)

        mv.create("logos")
        logos_mem = mv.get_personality("logos")
        logos_mem.learn("Logos analyzes Python performance", confidence=0.85, project="global")

        results = mv.recall("Python")
        assert len(results) >= 2
        personalities_found = {r["_personality"] for r in results}
        assert "atlas" in personalities_found
        assert "logos" in personalities_found

    def test_recall_from_specific_personality(self, mv, base_dir):
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        atlas_mem = AgentMemory.load(atlas_dir)
        atlas_mem.learn("Atlas fact about Rust", confidence=0.9, project="global")
        atlas_mem.db.close()
        mv.register("atlas", role="manager", directory=atlas_dir)

        results = mv.recall("Rust", personalities=["atlas"])
        assert all(r["_personality"] == "atlas" for r in results)

    def test_recall_empty_query(self, mv):
        results = mv.recall("")
        assert results == []


class TestMigration:
    def test_migrate_flat_layout(self, flat_null):
        mv = MultiverseManager(base_dir=flat_null)
        result = mv.migrate_flat_to_multiverse()

        assert not result["already_migrated"]
        assert "memory.db" in result["files_moved"]
        assert "identity.json" in result["files_moved"]
        assert result["backup_dir"] is not None

        # Verify files moved to atlas/
        atlas_dir = os.path.join(flat_null, "atlas")
        assert os.path.isfile(os.path.join(atlas_dir, "memory.db"))
        assert os.path.isfile(os.path.join(atlas_dir, "identity.json"))
        assert os.path.isfile(os.path.join(atlas_dir, "state.json"))

        # Verify atlas registered
        info = mv.get_personality_info("atlas")
        assert info is not None
        assert info["role"] == "manager"
        mv.close()

    def test_migrate_idempotent(self, flat_null):
        mv = MultiverseManager(base_dir=flat_null)
        mv.migrate_flat_to_multiverse()
        result2 = mv.migrate_flat_to_multiverse()
        assert result2["already_migrated"]
        mv.close()

    def test_migrate_dry_run(self, flat_null):
        mv = MultiverseManager(base_dir=flat_null)
        result = mv.migrate_flat_to_multiverse(dry_run=True)
        assert "memory.db" in result["files_moved"]
        # Verify nothing actually moved
        assert os.path.isfile(os.path.join(flat_null, "memory.db"))
        mv.close()

    def test_migrate_preserves_data(self, flat_null):
        mv = MultiverseManager(base_dir=flat_null)
        mv.migrate_flat_to_multiverse()

        # Load atlas from new location and verify data
        atlas_dir = os.path.join(flat_null, "atlas")
        mem = AgentMemory.load(atlas_dir)
        assert mem.name == "Atlas"
        facts = mem.knowledge
        fact_texts = {f["fact"] for f in facts}
        assert "Test fact alpha" in fact_texts
        assert "Test fact beta" in fact_texts
        mem.db.close()
        mv.close()

    def test_agent_memory_load_fallback(self, flat_null):
        """AgentMemory.load() with personality='atlas' should fall back to flat layout."""
        # Before migration, flat layout should still work
        mem = AgentMemory.load(agent_dir=flat_null)
        assert mem.name == "Atlas"
        mem.db.close()


class TestWakeup:
    def test_multiverse_wakeup(self, mv, base_dir):
        atlas_dir = os.path.join(base_dir, "atlas")
        os.makedirs(atlas_dir, exist_ok=True)
        AgentMemory.load(atlas_dir).db.close()
        with open(os.path.join(atlas_dir, "state.json"), "w") as f:
            json.dump({"energy": "high"}, f)
        with open(os.path.join(atlas_dir, "momentum.json"), "w") as f:
            json.dump({"active_project": "null"}, f)
        mv.register("atlas", role="manager", directory=atlas_dir)

        summaries = mv.wakeup()
        assert "atlas" in summaries
        assert summaries["atlas"]["state"]["energy"] == "high"


class TestDreams:
    def test_record_dream(self, mv):
        dream_id = mv.record_dream(
            "Atlas and Logos disagree on market direction",
            source_facts=[{"personality": "atlas", "fact_id": "abc"},
                          {"personality": "logos", "fact_id": "def"}],
        )
        assert dream_id > 0

    def test_get_pending_dreams(self, mv):
        mv.record_dream("Dream 1")
        mv.record_dream("Dream 2")
        dreams = mv.get_pending_dreams()
        assert len(dreams) == 2

    def test_promote_dream(self, mv):
        dream_id = mv.record_dream("Promotable dream")
        assert mv.promote_dream(dream_id)
        dreams = mv.get_pending_dreams()
        assert len(dreams) == 0

    def test_dismiss_dream(self, mv):
        dream_id = mv.record_dream("Bad dream")
        assert mv.dismiss_dream(dream_id)
        dreams = mv.get_pending_dreams()
        assert len(dreams) == 0
