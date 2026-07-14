"""Tests for recall context window — session neighbor expansion."""

import hashlib
import pytest
from datetime import datetime, timedelta, timezone

from null_memory.agent import AgentMemory


def _make_session_facts(mem, session_id, texts, project="global"):
    """Insert multiple facts in the same session with sequential timestamps."""
    now = datetime.now(timezone.utc)
    for i, text in enumerate(texts):
        ts = (now - timedelta(minutes=len(texts) - i)).isoformat()
        fid = hashlib.sha256(f"{text}:{project}".encode()).hexdigest()[:16]
        mem.db.insert_fact({
            "id": fid,
            "fact": text,
            "confidence": 0.8,
            "base_confidence": 0.8,
            "project": project,
            "source": "test",
            "session_id": session_id,
            "created_at": ts,
        })
    mem.db.conn.commit()


class TestSessionNeighbors:
    def test_returns_neighbors(self, mem):
        """get_session_neighbors returns facts from the same session."""
        _make_session_facts(mem, "sess-1", [
            "First fact about architecture decisions",
            "Second fact about database schema changes",
            "Third fact about API endpoint design",
        ])
        facts = mem.db.get_active_facts()
        mid_fact = facts[1]
        neighbors = mem.db.get_session_neighbors(mid_fact["id"], "sess-1", n=2)
        assert len(neighbors) == 2

    def test_no_neighbors_without_session(self, mem):
        """Returns empty if session_id is empty."""
        neighbors = mem.db.get_session_neighbors("abc", "", n=2)
        assert neighbors == []

    def test_no_self_inclusion(self, mem):
        """Fact itself is not included in neighbors."""
        _make_session_facts(mem, "sess-2", [
            "Alpha fact about caching strategy",
            "Beta fact about rate limiting design",
        ])
        facts = mem.db.get_active_facts()
        fid = facts[0]["id"]
        neighbors = mem.db.get_session_neighbors(fid, "sess-2", n=5)
        neighbor_ids = {n["id"] for n in neighbors}
        assert fid not in neighbor_ids


class TestRecallContextExpansion:
    def test_recall_includes_context(self, mem):
        """Recall results include context entries from same session."""
        _make_session_facts(mem, "ctx-session", [
            "Python uses indentation for code structure blocks",
            "Kubernetes orchestrates container workloads across clusters",
            "Redis provides in-memory caching with persistence options",
        ])
        # Only the first fact matches; the other two should come as context
        results = mem.recall("Python indentation code structure", limit=3)
        types = [r.get("_type") for r in results]
        assert "context" in types

    def test_context_marked_correctly(self, mem):
        """Context entries have _type='context' and _context_of set."""
        _make_session_facts(mem, "ctx-session-2", [
            "Rust borrow checker prevents data races memory safety",
            "Rust ownership model enables zero cost abstractions compiler",
            "Rust cargo build system manages packages efficiently well",
        ])
        results = mem.recall("Rust borrow checker memory safety", limit=5)
        context_entries = [r for r in results if r.get("_type") == "context"]
        for ctx in context_entries:
            assert "_context_of" in ctx
