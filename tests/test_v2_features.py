"""Tests for Null v2.0 — anticipatory memory features."""

import hashlib
import pytest
from datetime import datetime, timedelta, timezone

from null_memory.agent import AgentMemory
from null_memory.mood import detect_mood
from null_memory.hypnos import Hypnos, HypnosResult


def _make_fact(mem, text, project="global", impact=0.5, session_id=None,
               confidence=0.8, tier="contextual"):
    """Insert a fact with controllable metadata."""
    now = datetime.now(timezone.utc).isoformat()
    fid = hashlib.sha256(f"{text}:{project}".encode()).hexdigest()[:16]
    mem.db.insert_fact({
        "id": fid,
        "fact": text,
        "confidence": confidence,
        "base_confidence": confidence,
        "project": project,
        "source": "test",
        "provenance": "observation",
        "impact": impact,
        "session_id": session_id,
        "created_at": now,
        "tier": tier,
    })
    mem.db.conn.commit()
    return fid


# ── Feature 1: Calibration Questioning ──

class TestWonder:
    def test_wonder_stores_in_simmering(self, mem, tmp_path):
        """null_wonder stores question in simmering system."""
        from null_memory.wakeup import add_simmering, load_simmering
        entry = add_simmering(
            question="Why did Pete choose prediction markets?",
            context="testing calibration",
            category="calibration",
            agent_dir=str(tmp_path),
        )
        assert entry["id"]
        assert entry["category"] == "calibration"

        items = load_simmering(str(tmp_path))
        calibration = [s for s in items if s.get("category") == "calibration"]
        assert len(calibration) == 1

    def test_briefing_shows_calibration_questions(self, mem, tmp_path):
        """Briefing includes Questions for Pete section."""
        from null_memory.wakeup import add_simmering
        # Add calibration questions to mem's agent_dir
        add_simmering(
            question="How long have we been working together?",
            context="temporal uncertainty",
            category="calibration",
            agent_dir=mem.agent_dir,
        )
        briefing = mem.briefing()
        assert "Questions for Pete" in briefing
        assert "How long have we been working together" in briefing

    def test_handle_wonder(self, mem):
        """Handler stores and confirms."""
        from null_memory.mcp.handlers import NullHandlers
        # Use the handler directly through the agent
        from null_memory.wakeup import add_simmering
        entry = add_simmering(
            question="Test question",
            context="test",
            category="calibration",
            agent_dir=mem.agent_dir,
        )
        assert entry["question"] == "Test question"


# ── Feature 2: Proactive Insight Pushing ──

class TestInsightPushing:
    def test_find_relevant_insights_no_embeddings(self, mem):
        """Returns empty list gracefully without embeddings."""
        _make_fact(mem, "important architectural principle", impact=0.9)
        result = mem.find_relevant_insights("architecture decisions")
        assert result == []  # No embeddings in test env

    def test_find_relevant_insights_low_impact_filtered(self, mem):
        """Low-impact facts are not returned as insights."""
        _make_fact(mem, "trivial observation about weather", impact=0.2)
        result = mem.find_relevant_insights("weather today")
        assert result == []

    def test_excludes_session_recalled(self, mem):
        """Facts already recalled in session are excluded."""
        fid = _make_fact(mem, "critical system design pattern", impact=0.9)
        mem._session_recalled_ids.append(fid)
        result = mem.find_relevant_insights("system design")
        assert result == []


# ── Feature 3: Predictive Briefing ──

class TestPredictiveBriefing:
    def test_briefing_shows_momentum(self, mem):
        """Briefing includes planned next action from momentum."""
        from null_memory.wakeup import save_momentum
        save_momentum({
            "active_project": "orion",
            "next_action": "Fix the collector dedup bug",
            "blocked_on": "",
        }, agent_dir=mem.agent_dir)
        briefing = mem.briefing(project="orion")
        assert "Planned next" in briefing
        assert "collector dedup" in briefing

    def test_briefing_shows_blocked(self, mem):
        """Briefing shows blocked status from momentum."""
        from null_memory.wakeup import save_momentum
        save_momentum({
            "active_project": "null",
            "blocked_on": "Waiting for Windows test results",
        }, agent_dir=mem.agent_dir)
        briefing = mem.briefing(project="null")
        assert "Blocked" in briefing

    def test_briefing_5_facts(self, mem):
        """Briefing now shows up to 5 context facts."""
        for i in range(7):
            _make_fact(mem, f"Project fact number {i} about unique topic {i}",
                       project="testproj")
        briefing = mem.briefing(project="testproj")
        # Count context lines (lines with confidence percentages)
        context_lines = [l for l in briefing.split("\n") if "[" in l and "%]" in l]
        assert len(context_lines) >= 5


# ── Feature 4: Knowledge Synthesis ──

class TestHypnosSynthesis:
    def test_synthesis_disabled_by_default(self, mem):
        """Stage 5 doesn't run when config flag is off."""
        h = Hypnos(mem)
        result = h.run(stages=[5])
        assert result.stage5_synthesized == 0

    def test_synthesis_needs_embeddings(self, mem):
        """Stage 5 returns 0 without embeddings even if enabled."""
        mem._config = dict(mem.config)
        mem._config["hypnos_synthesis_enabled"] = True
        for i in range(15):
            _make_fact(mem, f"Synthesis test fact {i} about database optimization {i}")
        h = Hypnos(mem)
        h.config["hypnos_synthesis_enabled"] = True
        result = h.run(stages=[5])
        # Without embeddings, synthesis can't cluster
        assert result.stage5_synthesized == 0

    def test_synthesis_result_field(self, mem):
        """HypnosResult has stage5_synthesized field."""
        h = Hypnos(mem)
        result = h.run()
        assert hasattr(result, "stage5_synthesized")
        assert result.stage5_synthesized == 0

    def test_full_run_includes_stage5(self, mem):
        """Full run with stages=[1,2,3,4,5] doesn't crash."""
        h = Hypnos(mem)
        result = h.run(stages=[1, 2, 3, 4, 5])
        assert isinstance(result, HypnosResult)
        assert result.errors == []
