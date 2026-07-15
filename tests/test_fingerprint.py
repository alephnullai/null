"""Tests for conversation fingerprinting."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from null_memory.agent import AgentMemory
from null_memory.fingerprint import (
    SessionFingerprint,
    compute_fingerprint,
    find_similar_sessions,
    format_similar_sessions,
    _extract_tags,
    _compute_similarity,
)


class TestExtractTags:
    def test_basic_extraction(self):
        tags = _extract_tags([
            "Python uses SQLite for database storage",
            "SQLite supports full text search indexing",
            "Database migration from JSONL to SQLite",
        ])
        assert "sqlite" in tags
        assert len(tags) <= 5

    def test_empty_input(self):
        tags = _extract_tags([])
        assert tags == []

    def test_filters_stop_words(self):
        tags = _extract_tags(["the is a an of to in for on with"])
        assert tags == []


class TestComputeSimilarity:
    def test_identical_counts(self):
        fp = SessionFingerprint(session_id="a", facts_count=5, decisions_count=2)
        past = {"session_id": "b", "facts_count": 5, "decisions_count": 2,
                "mistakes_count": 0, "tags": [], "topic_vector": None}
        sim = _compute_similarity(fp, past)
        assert sim > 0.8

    def test_different_counts(self):
        fp = SessionFingerprint(session_id="a", facts_count=20, decisions_count=5)
        past = {"session_id": "b", "facts_count": 1, "decisions_count": 0,
                "mistakes_count": 0, "tags": [], "topic_vector": None}
        sim = _compute_similarity(fp, past)
        assert sim < 0.5

    def test_tag_overlap(self):
        fp = SessionFingerprint(session_id="a", tags=["python", "sqlite", "testing"])
        past = {"session_id": "b", "facts_count": 0, "decisions_count": 0,
                "mistakes_count": 0, "tags": ["python", "sqlite", "deploy"],
                "topic_vector": None}
        sim = _compute_similarity(fp, past)
        assert sim > 0.3  # Some tag overlap


class TestFingerprint:
    def test_insert_and_retrieve(self, mem):
        """Fingerprints can be stored and retrieved."""
        now = datetime.now(timezone.utc).isoformat()
        mem.db.insert_fingerprint({
            "session_id": "test-session-1",
            "project": "test-project",
            "duration_minutes": 45.0,
            "facts_count": 8,
            "decisions_count": 3,
            "mistakes_count": 1,
            "tier_dist": {"contextual": 5, "durable": 3},
            "topic_vector": None,
            "outcome": "positive",
            "tags": ["python", "testing"],
            "created_at": now,
        })
        mem.db.conn.commit()

        fps = mem.db.get_fingerprints()
        assert len(fps) == 1
        fp = fps[0]
        assert fp["session_id"] == "test-session-1"
        assert fp["outcome"] == "positive"
        assert fp["tier_dist"] == {"contextual": 5, "durable": 3}
        assert fp["tags"] == ["python", "testing"]

    def test_project_filter(self, mem):
        """Fingerprints can be filtered by project."""
        now = datetime.now(timezone.utc).isoformat()
        for i, proj in enumerate(["alpha", "beta", "alpha"]):
            mem.db.insert_fingerprint({
                "session_id": f"sess-{proj}-{i}",
                "project": proj,
                "created_at": now,
            })
        mem.db.conn.commit()

        alpha = mem.db.get_fingerprints(project="alpha")
        assert len(alpha) == 2

    def test_empty(self, mem):
        """Empty fingerprint list returned when none exist."""
        fps = mem.db.get_fingerprints()
        assert fps == []


class TestFindSimilarSessions:
    def test_finds_similar(self, mem):
        """find_similar_sessions returns matches above threshold."""
        now = datetime.now(timezone.utc).isoformat()
        # Insert a past session
        mem.db.insert_fingerprint({
            "session_id": "past-1",
            "project": "myproject",
            "facts_count": 10,
            "decisions_count": 3,
            "mistakes_count": 0,
            "tags": ["python", "database"],
            "outcome": "positive",
            "created_at": now,
        })
        mem.db.conn.commit()

        # Compare with similar current session
        current = SessionFingerprint(
            session_id="current",
            project="myproject",
            facts_count=10,
            decisions_count=3,
            mistakes_count=0,
            tags=["python", "database"],
        )
        matches = find_similar_sessions(mem, current)
        assert len(matches) >= 1
        assert matches[0]["similarity"] > 0.5

    def test_no_self_match(self, mem):
        """Current session is excluded from results."""
        now = datetime.now(timezone.utc).isoformat()
        mem.db.insert_fingerprint({
            "session_id": "same-session",
            "project": "global",
            "facts_count": 5,
            "created_at": now,
        })
        mem.db.conn.commit()

        current = SessionFingerprint(session_id="same-session")
        matches = find_similar_sessions(mem, current)
        assert all(m["fingerprint"]["session_id"] != "same-session" for m in matches)


class TestFormatSimilarSessions:
    def test_format_output(self):
        matches = [{
            "fingerprint": {
                "session_id": "abc123",
                "created_at": "2026-03-15T10:00:00+00:00",
                "project": "myproject",
                "outcome": "positive",
                "tags": ["python", "refactor"],
            },
            "similarity": 0.82,
        }]
        lines = format_similar_sessions(matches)
        assert len(lines) == 2  # header + 1 entry
        assert "Similar past sessions" in lines[0]
        assert "positive" in lines[1]
        assert "82%" in lines[1]

    def test_empty(self):
        lines = format_similar_sessions([])
        assert lines == []
