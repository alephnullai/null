"""Tests for temporal recall (Phase 4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from null_memory.agent import AgentMemory


class TestParseSince:
    def test_days_ago(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        dt = mem._parse_since("7d")
        assert dt is not None
        now = datetime.now(timezone.utc)
        assert (now - dt).days == 7 or (now - dt).days == 6  # rounding

    def test_this_week(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        dt = mem._parse_since("this_week")
        assert dt is not None
        assert dt.weekday() == 0  # Monday

    def test_iso8601(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        dt = mem._parse_since("2026-01-15T10:00:00+00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 1

    def test_last_session(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        # No sessions yet
        dt = mem._parse_since("last_session")
        assert dt is None

        # With a session
        session = mem.start_session(project="test")
        mem.close(summary="test")
        mem2 = AgentMemory.load(str(tmp_path))
        dt2 = mem2._parse_since("last_session")
        assert dt2 is not None

    def test_empty_string(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        assert mem._parse_since("") is None

    def test_invalid(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        assert mem._parse_since("not_a_date") is None


class TestTemporalRecall:
    def test_since_filters_old_facts(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        # Manually create an old fact
        old_entry = {
            "fact": "old fact about elephants from last year",
            "confidence": 0.9,
            "project": "global",
            "ts": (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(),
            "created_at": (datetime.now(timezone.utc) - timedelta(days=365)).isoformat(),
        }
        mem.knowledge.append(old_entry)
        # Create a new fact
        mem.learn("new fact about elephants from today", 0.9)

        # Recall with since=30d should only find the new one
        results = mem.recall("elephants", since="30d")
        facts = [r["fact"] for r in results]
        assert any("today" in f for f in facts)
        assert not any("last year" in f for f in facts)

    def test_since_none_returns_all(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        # Use DB inserts to bypass semantic dedup — these facts are intentionally
        # similar (both mention giraffes) but must remain as separate entries
        from tests.conftest import insert_n_facts
        now = datetime.now(timezone.utc).isoformat()
        mem.db.insert_fact({
            "id": "giraffe_fact_1",
            "fact": "giraffes are the tallest living terrestrial animals",
            "confidence": 0.9, "base_confidence": 0.9,
            "project": "global", "created_at": now, "access_count": 0,
        })
        mem.db.insert_fact({
            "id": "giraffe_fact_2",
            "fact": "giraffes have long necks for reaching high tree branches",
            "confidence": 0.9, "base_confidence": 0.9,
            "project": "global", "created_at": now, "access_count": 0,
        })
        mem.db.conn.commit()
        mem._reload_knowledge()
        results = mem.recall("giraffes", since=None)
        assert len(results) == 2

    def test_session_id_filter(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        s1 = mem.start_session(project="test")
        # Use genuinely distinct facts so semantic dedup won't merge them
        mem.learn("whales migrate thousands of miles across ocean basins each year", 0.9)
        mem.close(summary="s1")

        s2 = mem.start_session(project="test")
        mem.learn("the platypus is a venomous egg-laying mammal native to Australia", 0.9)

        # Filter by session 2
        results = mem.recall("platypus venomous mammal", session_id=s2.session_id)
        facts = [r["fact"] for r in results]
        assert any("platypus" in f for f in facts)
        assert not any("migrate" in f for f in facts)

    def test_combined_since_and_project(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.learn("recent aleph fact about parsing", 0.9, project="aleph")
        mem.learn("recent null fact about parsing", 0.9, project="null")

        results = mem.recall("parsing", project="aleph", since="1d")
        facts = [r["fact"] for r in results]
        assert any("aleph" in f for f in facts)
        assert not any("null" in f for f in facts)
