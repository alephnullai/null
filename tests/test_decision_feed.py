"""Tests for cross-instance decision feed (multi-Atlas coordination)."""

import pytest
from datetime import datetime, timezone

from null_memory.agent import AgentMemory


def _make_decision_feed_entry(mem, decision, reasoning="", project="global",
                               session_id="other-session", status="provisional"):
    """Insert a decision feed entry simulating another instance."""
    now = datetime.now(timezone.utc).isoformat()
    return mem.db.insert_decision_feed({
        "session_id": session_id,
        "decision": decision,
        "reasoning": reasoning,
        "project": project,
        "status": status,
        "created_at": now,
    })


class TestDecisionFeedDB:
    def test_insert_and_retrieve(self, mem):
        """Decision feed entries can be stored and retrieved."""
        _make_decision_feed_entry(
            mem, "Use SQLite for storage", "portable and embedded",
            session_id="session-A",
        )
        mem.db.conn.commit()
        feed = mem.db.get_decision_feed()
        assert len(feed) == 1
        assert feed[0]["decision"] == "Use SQLite for storage"

    def test_exclude_current_session(self, mem):
        """Can exclude decisions from the current session."""
        _make_decision_feed_entry(mem, "Decision A", session_id="session-A")
        _make_decision_feed_entry(mem, "Decision B", session_id="session-B")
        mem.db.conn.commit()

        feed = mem.db.get_decision_feed(exclude_session="session-A")
        assert len(feed) == 1
        assert feed[0]["decision"] == "Decision B"

    def test_project_filter(self, mem):
        """Feed can be filtered by project."""
        _make_decision_feed_entry(mem, "Orion choice", project="orion",
                                   session_id="s1")
        _make_decision_feed_entry(mem, "Aleph choice", project="aleph",
                                   session_id="s2")
        _make_decision_feed_entry(mem, "Global choice", project="global",
                                   session_id="s3")
        mem.db.conn.commit()

        orion = mem.db.get_decision_feed(project="orion")
        # Should get orion + global
        decisions = {d["decision"] for d in orion}
        assert "Orion choice" in decisions
        assert "Global choice" in decisions
        assert "Aleph choice" not in decisions

    def test_search_by_keyword(self, mem):
        """Can search decision feed by keyword."""
        _make_decision_feed_entry(
            mem, "Bond harvest is non-viable on Polymarket",
            reasoning="Thin books, 1-4c spread at 95c+",
            session_id="s1",
        )
        _make_decision_feed_entry(
            mem, "Use weather station data for signals",
            reasoning="High confidence when temp is dropping",
            session_id="s2",
        )
        mem.db.conn.commit()

        results = mem.db.search_decision_feed("bond harvest")
        assert len(results) == 1
        assert "non-viable" in results[0]["decision"]

    def test_status_field(self, mem):
        """Decision feed entries have status (provisional/concluded)."""
        _make_decision_feed_entry(
            mem, "Use Rust for Oracle",
            status="concluded",
            session_id="s1",
        )
        mem.db.conn.commit()
        feed = mem.db.get_decision_feed()
        assert feed[0]["status"] == "concluded"


class TestDecideWritesToFeed:
    def test_decide_populates_feed(self, mem):
        """decide() automatically writes to the decision feed."""
        mem.decide("Switch to async executor", "better throughput", project="orion")
        feed = mem.db.get_decision_feed()
        assert len(feed) == 1
        assert "async executor" in feed[0]["decision"]
        assert feed[0]["status"] == "provisional"


class TestCheckPriorDecisions:
    def test_keyword_fallback(self, mem):
        """Finds prior decisions by keyword when no embeddings."""
        _make_decision_feed_entry(
            mem, "Bond harvest is dead on Polymarket weather",
            reasoning="Thin books at 95c+, Arbe4 already failed",
            project="orion",
            session_id="other-session",
        )
        mem.db.conn.commit()

        result = mem.check_prior_decisions("bond harvest strategy", project="orion")
        assert result is not None
        assert "dead" in result["decision"]

    def test_no_match(self, mem):
        """Returns None when no relevant prior decisions."""
        result = mem.check_prior_decisions("completely unrelated topic")
        assert result is None

    def test_excludes_current_session(self, mem):
        """Doesn't return decisions from the current session."""
        # Start a real session so we have a session_id
        mem.start_session(project="orion")
        mem.decide("My own decision about weather", "testing", project="orion")
        # The decide() wrote to feed with current session_id
        result = mem.check_prior_decisions("weather decision", project="orion")
        # Should be None because it's our own session's decision
        assert result is None


class TestBriefingCrossInstance:
    def test_briefing_shows_other_session_decisions(self, mem):
        """Briefing includes decisions from other sessions."""
        _make_decision_feed_entry(
            mem, "Weather station signals are the primary edge",
            reasoning="Positive EV confirmed on 15 sim bets",
            project="orion",
            session_id="other-atlas-session",
        )
        mem.db.conn.commit()

        briefing = mem.briefing(project="orion")
        assert "other sessions" in briefing
        assert "Weather station signals" in briefing

    def test_briefing_empty_feed(self, mem):
        """Briefing works fine with empty decision feed."""
        briefing = mem.briefing()
        assert "ready to work" in briefing
