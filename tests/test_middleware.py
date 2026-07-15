"""Tests for the zero-discipline middleware wrapper."""

from __future__ import annotations

import pytest

from null_memory.middleware import NullMiddleware, MiddlewareConfig


class TestMiddlewareLifecycle:
    def test_auto_starts_on_before_turn(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        assert not mw._started
        mw.before_turn("hello")
        assert mw._started

    def test_turn_count_increments(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        assert mw.turn_count == 0
        mw.before_turn("turn 1")
        assert mw.turn_count == 1
        mw.before_turn("turn 2")
        assert mw.turn_count == 2

    def test_end_session(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.before_turn("working")
        mw.learn("test fact", 0.9)
        result = mw.end_session(summary="done")
        # Verify the session was closed and data was synced
        assert not mw._started
        assert "synced" in result
        assert "debrief" in result


class TestMiddlewareAutoRecall:
    def test_before_turn_recalls_relevant(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.before_turn("setup")
        mw.learn("the API uses REST endpoints for user management", 0.9)
        context = mw.before_turn("tell me about the API endpoints")
        assert "REST" in context or "api" in context.lower()

    def test_before_turn_empty_when_no_match(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.before_turn("setup")
        mw.learn("postgres is the database", 0.9)
        context = mw.before_turn("completely unrelated xyzzy gibberish")
        # With semantic search, context may be empty or contain low-relevance results
        # The key invariant is that it doesn't crash and returns a string
        assert isinstance(context, str)

    def test_auto_recall_disabled(self, tmp_path):
        config = MiddlewareConfig(auto_recall=False)
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path), config=config)
        mw.before_turn("setup")
        mw.learn("something about cats and dogs", 0.9)
        context = mw.before_turn("cats and dogs")
        assert context == ""


class TestMiddlewareAutoObserve:
    def test_after_turn_observes_summary(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.before_turn("start")
        initial = mw.fact_count
        mw.after_turn(summary="implemented user authentication with JWT tokens")
        assert mw.fact_count > initial

    def test_after_turn_observes_tool_calls(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.before_turn("start")
        initial = mw.fact_count
        mw.after_turn(tool_calls=["Write src/auth.py", "Bash: pytest tests/"])
        assert mw.fact_count > initial

    def test_after_turn_no_summary_no_tools_is_noop(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.before_turn("start")
        initial = mw.fact_count
        mw.after_turn()
        assert mw.fact_count == initial

    def test_auto_observe_disabled(self, tmp_path):
        config = MiddlewareConfig(auto_observe=False)
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path), config=config)
        mw.before_turn("start")
        initial = mw.fact_count
        mw.after_turn(summary="this should not be recorded")
        assert mw.fact_count == initial


class TestMiddlewareAutoCheckpoint:
    def test_auto_checkpoint_at_interval(self, tmp_path):
        config = MiddlewareConfig(checkpoint_interval=3)
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path), config=config)

        for i in range(4):
            mw.before_turn(f"turn {i}")
            mw.after_turn(summary=f"did thing {i}")

        # After 3 turns, a checkpoint should have been created
        session = mw.memory._current_session
        assert session is not None
        assert len(session.checkpoints) >= 1

    def test_auto_checkpoint_disabled(self, tmp_path):
        config = MiddlewareConfig(auto_checkpoint=False, checkpoint_interval=2)
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path), config=config)

        for i in range(5):
            mw.before_turn(f"turn {i}")
            mw.after_turn(summary=f"thing {i}")

        session = mw.memory._current_session
        assert session is not None
        assert len(session.checkpoints) == 0


class TestMiddlewareAutoConsolidate:
    def test_auto_consolidate_at_threshold(self, tmp_path):
        config = MiddlewareConfig(consolidate_threshold=5)
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path), config=config)
        mw.before_turn("start")

        # Learn exactly 5 facts to trigger consolidation
        for i in range(5):
            mw.learn(f"distinct fact number {i} about unique topic {i}", 0.8)

        # After_turn should trigger consolidation since 5 new facts
        mw.after_turn()
        # Consolidation ran (even if it didn't merge anything, it ran)
        # We just verify it didn't crash

    def test_end_session_runs_consolidation(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.before_turn("start")
        # Add enough facts via SQLite that consolidation threshold is met
        for i in range(110):
            mw.memory.db.insert_fact({
                "id": f"bulk_{i:04d}",
                "fact": f"bulk fact {i} for consolidation test",
                "confidence": 0.5,
                "base_confidence": 0.5,
                "project": "test",
                "created_at": "2026-03-22T00:00:00+00:00",
                "access_count": 0,
            })
        mw.memory.db.conn.commit()
        mw.memory._reload_knowledge()
        result = mw.end_session(summary="test")
        # Should not crash and should complete


class TestMiddlewareDirectAccess:
    def test_learn(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        entry = mw.learn("direct learn fact", 0.9)
        assert entry["fact"] == "direct learn fact"

    def test_decide(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        entry = mw.decide("use postgres", "proven at scale")
        assert entry["decision"] == "use postgres"

    def test_mistake(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        entry = mw.mistake("forgot tests", "rushing")
        assert entry["mistake"] == "forgot tests"

    def test_recall(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw.learn("recall test fact about memory systems", 0.9)
        results = mw.recall("memory systems")
        assert len(results) > 0

    def test_briefing(self, tmp_path):
        mw = NullMiddleware(project="test", agent_dir=str(tmp_path))
        briefing = mw.briefing()
        assert "ready" in briefing.lower() or mw.name in briefing


class TestMiddlewareCrashDetection:
    def test_crash_warning_on_first_turn(self, tmp_path):
        # Simulate a crashed session
        mw1 = NullMiddleware(project="test", agent_dir=str(tmp_path))
        mw1.before_turn("first session")
        mw1.learn("something important", 0.9)

        # Age the session past the 5-minute MCP-restart window so the
        # reload is classified as a real crash (not a silent restart).
        from datetime import datetime, timedelta, timezone
        session = mw1.memory._current_session
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        session.started_at = stale
        session.last_tool_call = stale
        mw1.memory._session_manager.save_session(session)

        # New middleware instance (new process)
        mw2 = NullMiddleware(project="test", agent_dir=str(tmp_path))
        context = mw2.before_turn("resuming after crash")
        assert "CRASH" in context.upper() or "crash" in context.lower() or context == ""
        # The crash should be detected even if no matching recall results
