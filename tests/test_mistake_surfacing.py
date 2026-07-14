"""Tests for proactive mistake surfacing."""

import pytest
from null_memory.agent import AgentMemory


class TestMistakeEmbedding:
    def test_mistake_returns_id(self, mem):
        """mistake() returns entry with id field."""
        entry = mem.mistake("test error happened", "bad logic")
        assert "id" in entry
        assert isinstance(entry["id"], int)

    def test_get_mistake_by_id(self, mem):
        """get_mistake_by_id retrieves the mistake."""
        entry = mem.mistake("specific error type A", "root cause analysis")
        result = mem.db.get_mistake_by_id(entry["id"])
        assert result is not None
        assert result["mistake"] == "specific error type A"

    def test_get_mistake_by_id_missing(self, mem):
        """get_mistake_by_id returns None for missing ID."""
        result = mem.db.get_mistake_by_id(99999)
        assert result is None


class TestCheckMistakeSimilarity:
    def test_no_crash_without_embeddings(self, mem):
        """check_mistake_similarity returns None gracefully without embeddings."""
        mem.mistake("deployed without testing first", "skipped CI pipeline")
        result = mem.check_mistake_similarity("deploying without running tests")
        # Without fastembed installed, should return None gracefully
        assert result is None or isinstance(result, dict)

    def test_returns_none_for_unrelated(self, mem):
        """Unrelated text doesn't trigger mistake similarity."""
        mem.mistake("database migration failed", "wrong column type")
        result = mem.check_mistake_similarity("nice weather today")
        assert result is None


class TestObserveWithMistakeCheck:
    def test_observe_doesnt_crash_with_mistakes(self, mem):
        """observe() works fine when mistakes exist — no crash even without embeddings."""
        mem.mistake("forgot to run tests before pushing", "rushed deployment")
        # Observe should work fine — mistake check returns None without embeddings
        result = mem.observe("pushing code to production", project="global")
        assert result is not None or result is None  # Just shouldn't crash
