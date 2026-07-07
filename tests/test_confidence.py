"""Tests for enriched timestamps, dynamic confidence, fact fingerprinting, and supersession (Phase 2)."""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pytest

from null_memory.agent import AgentMemory


# ── Effective Confidence ──


class TestEffectiveConfidence:
    def test_fresh_fact_high_confidence(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = mem.learn("test fact", 0.9)
        eff = mem.effective_confidence(entry)
        # Fresh fact with 0.9 base should be close to 0.9
        assert 0.7 < eff <= 1.0

    def test_old_fact_decays(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = {
            "fact": "old fact",
            "confidence": 0.9,
            "base_confidence": 0.9,
            "ts": (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),
            "created_at": (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(),
            "access_count": 0,
        }
        eff = mem.effective_confidence(entry)
        # 200 days old with decay rate 0.003: exp(-0.6) ≈ 0.55, * 0.9 ≈ 0.49
        assert eff < 0.6

    def test_accessed_fact_resists_decay(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()

        # Same base, same age, different access counts
        entry_unused = {
            "fact": "unused", "confidence": 0.8, "ts": old_ts,
            "access_count": 0,
        }
        entry_used = {
            "fact": "used", "confidence": 0.8, "ts": old_ts,
            "access_count": 10,
        }
        eff_unused = mem.effective_confidence(entry_unused)
        eff_used = mem.effective_confidence(entry_used)
        assert eff_used > eff_unused

    def test_verified_fact_gets_boost(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        now = datetime.now(timezone.utc).isoformat()
        entry_unverified = {
            "fact": "unverified", "confidence": 0.7, "ts": now,
            "access_count": 0,
        }
        entry_verified = {
            "fact": "verified", "confidence": 0.7, "ts": now,
            "access_count": 0,
            "last_verified": now,
        }
        eff_unv = mem.effective_confidence(entry_unverified)
        eff_v = mem.effective_confidence(entry_verified)
        assert eff_v > eff_unv

    def test_lesson_provenance_gets_boost(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        now = datetime.now(timezone.utc).isoformat()
        entry_obs = {
            "fact": "observed", "confidence": 0.8, "ts": now,
            "provenance": "observation", "access_count": 0,
        }
        entry_lesson = {
            "fact": "lesson", "confidence": 0.8, "ts": now,
            "provenance": "lesson", "access_count": 0,
        }
        assert mem.effective_confidence(entry_lesson) > mem.effective_confidence(entry_obs)

    def test_reconstructed_provenance_penalized(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        now = datetime.now(timezone.utc).isoformat()
        entry_explicit = {
            "fact": "explicit", "confidence": 0.8, "ts": now,
            "provenance": "explicit", "access_count": 0,
        }
        entry_reconstructed = {
            "fact": "reconstructed", "confidence": 0.8, "ts": now,
            "provenance": "reconstructed", "access_count": 0,
        }
        assert mem.effective_confidence(entry_explicit) > mem.effective_confidence(entry_reconstructed)

    def test_legacy_fact_without_new_fields(self, tmp_path):
        """v0.2.5 facts should still work."""
        mem = AgentMemory.load(str(tmp_path))
        legacy = {
            "fact": "old format fact",
            "confidence": 0.85,
            "project": "global",
            "source": "bootstrap",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        eff = mem.effective_confidence(legacy)
        assert 0.5 < eff <= 1.0

    def test_confidence_capped_at_1(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = {
            "fact": "super fact", "confidence": 0.99, "ts": datetime.now(timezone.utc).isoformat(),
            "access_count": 20, "last_verified": datetime.now(timezone.utc).isoformat(),
            "provenance": "lesson",
        }
        assert mem.effective_confidence(entry) <= 1.0


# ── Content Hash & Dedup ──


class TestContentHash:
    def test_hash_deterministic(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        h1 = mem._content_hash("hello world")
        h2 = mem._content_hash("hello world")
        assert h1 == h2

    def test_hash_case_insensitive(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        h1 = mem._content_hash("Hello World")
        h2 = mem._content_hash("hello world")
        assert h1 == h2

    def test_hash_strips_whitespace(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        h1 = mem._content_hash("  hello world  ")
        h2 = mem._content_hash("hello world")
        assert h1 == h2

    def test_hash_length(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        h = mem._content_hash("test")
        assert len(h) == 12


class TestDedupAtIngestion:
    def test_duplicate_fact_updates_existing(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        e1 = mem.learn("test fact about dedup", 0.7)
        e2 = mem.learn("test fact about dedup", 0.9)
        # Should have updated e1, not created a new entry
        assert len(mem.knowledge) == 1
        assert mem.knowledge[0]["confidence"] == 0.9  # max of 0.7 and 0.9

    def test_different_facts_not_deduped(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.learn("fact one about cats", 0.7)
        mem.learn("fact two about dogs", 0.8)
        assert len(mem.knowledge) == 2

    def test_dedup_bumps_access_count(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.learn("repeated fact about testing", 0.7)
        mem.learn("repeated fact about testing", 0.8)
        assert mem.knowledge[0].get("access_count", 0) >= 1


# ── Enriched Schema ──


class TestEnrichedSchema:
    def test_learn_creates_enriched_entry(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = mem.learn("enriched fact", 0.85, source="explicit")
        assert "id" in entry
        assert "created_at" in entry
        assert "last_accessed" in entry
        assert "access_count" in entry
        assert "base_confidence" in entry
        assert "provenance" in entry
        assert entry["provenance"] == "explicit"
        assert entry["base_confidence"] == 0.85

    def test_provenance_from_source(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        assert mem.learn("a", source="lesson")["provenance"] == "lesson"
        assert mem.learn("b", source="debrief")["provenance"] == "debrief"
        assert mem.learn("c", source="reconstructed")["provenance"] == "reconstructed"
        assert mem.learn("d", source="observation")["provenance"] == "observation"
        assert mem.learn("e", source="")["provenance"] == "observed"
        assert mem.learn("f", source="something_custom")["provenance"] == "observed"


# ── Supersession ──


class TestSupersession:
    def test_supersede_by_hash(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        old = mem.learn("the aleph codebase currently indexes 22 unique parser grammars", 0.9)
        old_id = old["id"]

        new = mem.learn("kubernetes cluster runs 32 pods across three availability zones", 0.95, replaces=old_id)
        assert new.get("supersedes") == old_id
        # Verify the old fact is marked as superseded in the DB
        row = mem.db.conn.execute(
            "SELECT superseded_by FROM facts WHERE id=?", (old_id,)
        ).fetchone()
        assert row[0] == new["id"]

    def test_supersede_by_text(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        old = mem.learn("the aleph codebase currently indexes 22 unique parser grammars", 0.9)
        new = mem.learn("kubernetes cluster runs 32 pods across three availability zones", 0.95, replaces="22 unique parser")
        assert new.get("supersedes") == old["id"]

    def test_superseded_excluded_from_recall(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        old = mem.learn("the aleph codebase currently indexes 22 unique parser grammars", 0.9)
        mem.learn("kubernetes cluster runs 32 pods across three availability zones", 0.95, replaces="22 unique parser")
        results = mem.recall("parser grammars pods zones")
        # Only the new fact should appear
        facts = [r["fact"] for r in results if r.get("_type") != "mistake"]
        assert any("32" in f for f in facts)
        assert not any("22" in f for f in facts)

    def test_supersede_nonexistent_is_noop(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = mem.learn("new fact", 0.9, replaces="nonexistent_hash")
        assert entry.get("supersedes") is None or entry.get("supersedes") == ""


# ── Access Tracking in Recall ──


class TestAccessTracking:
    def test_recall_updates_access_count(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.learn("unique trackable fact about zebras", 0.9)
        assert mem.knowledge[0].get("access_count", 0) == 0

        mem.recall("zebras")
        assert mem.knowledge[0].get("access_count", 0) >= 1

    def test_recall_updates_last_accessed(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = mem.learn("another trackable fact about lions", 0.9)
        original_accessed = entry.get("last_accessed")

        time.sleep(0.01)
        mem.recall("lions")
        assert mem.knowledge[0].get("last_accessed") >= original_accessed

    def test_access_tracking_persists_to_db(self, tmp_path):
        """After recall, access count should be persisted in SQLite."""
        mem = AgentMemory.load(str(tmp_path))
        entry = mem.learn("dirty tracking test for elephants", 0.9)
        mem.recall("elephants")
        # Verify the access count was persisted to SQLite
        row = mem.db.conn.execute(
            "SELECT access_count FROM facts WHERE id=?", (entry["id"],)
        ).fetchone()
        assert row[0] >= 1


# ── Migration / Backward Compat ──


class TestMigrationCompat:
    def test_legacy_fact_works_in_recall(self, tmp_path):
        """Facts inserted directly into SQLite should still be searchable."""
        mem = AgentMemory.load(str(tmp_path))
        # Insert a minimal fact directly into SQLite (simulating legacy data)
        mem.db.insert_fact({
            "id": "legacy_pandas1",
            "fact": "legacy fact about pandas",
            "confidence": 0.85,
            "base_confidence": 0.85,
            "project": "global",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        mem.db.conn.commit()
        mem._reload_knowledge()

        results = mem.recall("pandas")
        assert len(results) > 0
        assert "pandas" in results[0]["fact"]

    def test_legacy_fact_survives_gc(self, tmp_path):
        """GC should handle facts inserted directly into SQLite."""
        mem = AgentMemory.load(str(tmp_path))
        mem.db.insert_fact({
            "id": "legacy_gc_test",
            "fact": "gc compat test",
            "confidence": 0.85,
            "base_confidence": 0.85,
            "project": "global",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        mem.db.conn.commit()
        mem._reload_knowledge()
        result = mem.gc(max_facts=5000)
        assert result["remaining"] >= 1

    def test_effective_confidence_on_legacy(self, tmp_path):
        """effective_confidence should work on old-format entries."""
        mem = AgentMemory.load(str(tmp_path))
        legacy = {
            "fact": "old",
            "confidence": 0.75,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        eff = mem.effective_confidence(legacy)
        assert 0.4 < eff <= 1.0
