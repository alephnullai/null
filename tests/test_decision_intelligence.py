"""Tests for decision outcome intelligence in briefings."""

import pytest
from null_memory.agent import AgentMemory


class TestDecisionOutcomes:
    def test_briefing_includes_decisions(self, mem):
        """Briefing shows recent decisions."""
        mem.decide("Use SQLite for storage", "portable and embedded")
        briefing = mem.briefing()
        assert "Use SQLite" in briefing

    def test_briefing_shows_track_record(self, mem):
        """Briefing shows success rate when enough outcomes exist."""
        # Create decisions and record outcomes
        mem.decide("Use async/await", "better performance")
        mem.decide("Switch to REST", "simpler than gRPC")
        mem.decide("Add caching layer", "reduce latency")

        # Record outcomes
        mem.record_outcome("async", "worked great", success=True)
        mem.record_outcome("REST", "clients happy", success=True)
        mem.record_outcome("caching", "cache invalidation bugs", success=False)

        briefing = mem.briefing()
        assert "Track record" in briefing
        assert "2/3" in briefing

    def test_briefing_marks_successful_decisions(self, mem):
        """Successful decisions marked with + prefix."""
        mem.decide("Use TypeScript", "type safety")
        mem.record_outcome("TypeScript", "caught many bugs", success=True)
        briefing = mem.briefing()
        assert "+ Use TypeScript" in briefing

    def test_briefing_marks_failed_decisions(self, mem):
        """Failed decisions marked with x prefix."""
        mem.decide("Skip unit tests", "move faster")
        mem.record_outcome("Skip unit tests", "regression shipped", success=False)
        briefing = mem.briefing()
        assert "x Skip unit tests" in briefing

    def test_no_track_record_without_outcomes(self, mem):
        """Don't show track record if fewer than 3 outcomes."""
        mem.decide("Some decision", "some reason")
        briefing = mem.briefing()
        assert "Track record" not in briefing
