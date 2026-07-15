"""Tests for memory consolidation and fact verification (Phase 4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from null_memory.agent import AgentMemory
from null_memory.mcp.handlers import NullHandlers


# ── Verification ──


class TestVerification:
    def test_verify_fact(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        mem.learn("postgres is the primary database", 0.9)
        entry = mem.verify_fact("postgres database")
        assert entry is not None
        assert entry.get("last_verified") is not None

    def test_verify_no_match(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        result = mem.verify_fact("nonexistent topic xyzzy")
        assert result is None

    def test_verified_fact_higher_confidence(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        # Use an older fact so age decay is noticeable and verification boost matters
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        entry = {
            "fact": "verified fact about testing with age",
            "confidence": 0.7, "base_confidence": 0.7,
            "project": "global", "ts": old_ts, "created_at": old_ts,
            "access_count": 0,
        }
        mem.knowledge.append(entry)
        eff_before = mem.effective_confidence(entry)
        entry["last_verified"] = datetime.now(timezone.utc).isoformat()
        eff_after = mem.effective_confidence(entry)
        assert eff_after > eff_before

    def test_verify_handler(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        h.memory.learn("handler verification test fact", 0.8)
        result = h.handle_verify("handler verification")
        assert "Verified" in result

    def test_verify_handler_no_match(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        result = h.handle_verify("nonexistent xyzzy")
        assert "No fact" in result


# ── Consolidation ──


class TestConsolidation:
    def test_strengthen_accessed_facts(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = mem.learn("strengthened fact about dogs and cats", 0.5)
        # Update access_count in SQLite (in-memory dict mutation won't persist)
        mem.db.conn.execute(
            "UPDATE facts SET access_count = 10 WHERE id = ?", (entry["id"],)
        )
        mem.db.conn.commit()
        mem._reload_knowledge()
        result = mem.consolidate()
        assert result["strengthened"] >= 1
        assert mem.knowledge[0].get("base_confidence", mem.knowledge[0]["confidence"]) >= 0.7

    def test_fade_untouched_old_facts(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        mem.db.insert_fact({
            "id": "old_fade_test",
            "fact": "old untouched fact about forgotten things",
            "confidence": 0.8,
            "base_confidence": 0.8,
            "project": "global",
            "created_at": old_ts,
            "access_count": 0,
        })
        mem.db.conn.commit()
        mem._reload_knowledge()
        result = mem.consolidate()
        assert result["faded"] >= 1
        assert mem.knowledge[0]["base_confidence"] < 0.8

    def test_fade_writes_hypnos_journal_entry(self, tmp_path):
        """Confidence fades are destructive — each one must be journaled
        (fact id + old/new confidence + reason) so it's auditable."""
        mem = AgentMemory.load(str(tmp_path))
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        mem.db.insert_fact({
            "id": "old_fade_journal",
            "fact": "old untouched fact that will fade and be journaled",
            "confidence": 0.8,
            "base_confidence": 0.8,
            "project": "global",
            "created_at": old_ts,
            "access_count": 0,
        })
        mem.db.conn.commit()
        result = mem.consolidate()
        assert result["faded"] >= 1

        rows = mem.db.conn.execute(
            """SELECT fact_id, detail FROM hypnos_journal
               WHERE stage = 'consolidate' AND action = 'faded'"""
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "old_fade_journal"
        assert "old_conf=0.800" in rows[0][1]
        assert "new_conf=0.640" in rows[0][1]

    def test_dont_fade_recent_untouched(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        entry = mem.learn("recent untouched fact about new things", 0.8)
        original_base = entry["base_confidence"]
        mem.consolidate()
        assert entry["base_confidence"] == original_base  # Not faded

    def test_consolidate_similar_facts(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        # Two facts that are similar but not identical (40-65% Jaccard)
        # Insert directly via DB to bypass semantic dedup at learn() time
        now = datetime.now(timezone.utc).isoformat()
        mem.db.insert_fact({
            "id": "consol_fact_1",
            "fact": "aleph uses tree-sitter for parsing source code into abstract syntax trees",
            "confidence": 0.8, "base_confidence": 0.8,
            "project": "global", "created_at": now, "access_count": 0,
        })
        mem.db.insert_fact({
            "id": "consol_fact_2",
            "fact": "aleph uses tree-sitter for parsing source code with fast incremental updates",
            "confidence": 0.9, "base_confidence": 0.9,
            "project": "global", "created_at": now, "access_count": 0,
        })
        mem.db.conn.commit()
        mem._reload_knowledge()
        result = mem.consolidate()
        # At least one should be consolidated (superseded)
        assert result["consolidated"] >= 1
        # mem.knowledge filters out superseded facts, so check DB directly
        rows = mem.db.conn.execute(
            "SELECT id FROM facts WHERE superseded_by IS NOT NULL"
        ).fetchall()
        assert len(rows) >= 1

    def test_consolidate_keeps_best(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.learn("null memory system stores persistent agent knowledge across sessions", 0.7)
        mem.learn("null memory system stores persistent agent facts and decisions across sessions", 0.95)
        mem.consolidate()
        # The 0.95 one should survive; mem.knowledge already filters out superseded
        assert any(e.get("base_confidence", e.get("confidence", 0)) >= 0.9 for e in mem.knowledge)

    def test_consolidate_respects_project_boundaries(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.learn("project alpha uses postgres for data storage and caching", 0.8, project="alpha")
        mem.learn("project beta uses postgres for data storage and retrieval", 0.8, project="beta")
        result = mem.consolidate()
        # Different projects should not be consolidated
        assert result["consolidated"] == 0
        # Verify nothing was superseded in DB either
        rows = mem.db.conn.execute(
            "SELECT id FROM facts WHERE superseded_by IS NOT NULL"
        ).fetchall()
        assert len(rows) == 0

    def test_consolidate_idempotent(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.learn("idempotent test fact about repeated consolidation runs", 0.8)
        r1 = mem.consolidate()
        r2 = mem.consolidate()
        # Second run should not change anything
        assert r2["strengthened"] == 0
        assert r2["faded"] == 0

    def test_consolidate_handler(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        h.memory.learn("consolidation handler test fact", 0.8)
        result = h.handle_consolidate()
        assert "Consolidation complete" in result
