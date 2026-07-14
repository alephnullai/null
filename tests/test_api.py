"""Tests for the zero-discipline Python API."""

from __future__ import annotations

import pytest

from null_memory.api import NullAPI


class TestNullAPI:
    def test_start_and_identity(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        identity = api.start(project="test")
        assert api.is_started
        assert api.name  # Has a name

    def test_start_idempotent(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        api.start(project="test")  # Should not error
        assert api.is_started

    def test_auto_start_on_learn(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        assert not api.is_started
        api.learn("auto-started fact", 0.9, project="test")
        assert api.is_started
        assert api.fact_count >= 1

    def test_learn_and_recall(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        api.learn("python is a great language for scripting", 0.9)
        results = api.recall("python language")
        assert len(results) > 0

    def test_decide(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        entry = api.decide("use postgres", "proven at scale")
        assert entry["decision"] == "use postgres"

    def test_mistake(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        entry = api.mistake("forgot tests", "was in a rush")
        assert entry["mistake"] == "forgot tests"

    def test_before_turn(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        api.learn("the database uses postgres for primary storage", 0.9)
        context = api.before_turn("tell me about the database")
        assert "postgres" in context.lower()

    def test_before_turn_empty(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        context = api.before_turn("completely unrelated xyzzy topic")
        assert context == ""

    def test_after_turn_observes(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        initial_count = api.fact_count
        api.after_turn("implemented the login feature with OAuth")
        assert api.fact_count > initial_count

    def test_after_turn_empty_is_noop(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        initial_count = api.fact_count
        api.after_turn("")
        assert api.fact_count == initial_count

    def test_end_session(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        api.learn("session fact", 0.9)
        result = api.end(summary="test session complete", project="test")
        assert not api.is_started
        assert result["committed"] is True

    def test_end_with_full_debrief(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        result = api.end(
            summary="shipped the feature",
            went_well="clean implementation",
            missed="no error handling",
            do_differently="add error handling first",
            decisions_made=["use git for memory"],
            lessons=["git gives timestamps for free"],
        )
        assert result["reflected"] is True
        assert result["debrief"]["facts"] >= 1

    def test_briefing(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        briefing = api.briefing()
        assert "ready to work" in briefing.lower() or api.name in briefing

    def test_verify(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        api.start(project="test")
        api.learn("verified test fact about memory systems", 0.8)
        entry = api.verify("memory systems")
        assert entry is not None
        assert entry.get("last_verified") is not None

    def test_status(self, tmp_path):
        api = NullAPI(agent_dir=str(tmp_path))
        status = api.status()
        assert "Facts" in status
