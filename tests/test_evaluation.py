"""Tests for the evaluation/diagnostics system."""

import pytest
from null_memory.agent import AgentMemory


@pytest.fixture
def mem(tmp_path):
    """Fresh AgentMemory with isolated temp directory."""
    m = AgentMemory.load(str(tmp_path))
    m.start_session(project="global")
    return m


@pytest.fixture
def rich_mem(tmp_path):
    """AgentMemory with enough data to produce meaningful metrics."""
    m = AgentMemory.load(str(tmp_path))
    m.start_session(project="global")

    # Add facts with specific details (will auto-generate probes)
    m.learn("Agent TK1 wears jersey number 42 in basketball",
            confidence=0.9, project="global")
    m.learn("Project launched on March 15, 2026",
            confidence=0.85, project="global")
    m.learn("System has 850 tests passing",
            confidence=0.95, project="global")
    m.learn("Current version is v2.1.0",
            confidence=0.9, project="global")
    m.learn("Budget deployed is $5.64",
            confidence=0.8, project="global")

    # Add some generic facts too
    m.learn("Python uses indentation for blocks", confidence=0.7)
    m.learn("Rust has zero-cost abstractions", confidence=0.6)

    # Add a user probe
    m.add_probe("TK1 jersey number?", "42")

    # Record a decision and mistake
    m.decide("Use SQLite for storage", "single-file, portable", project="global")
    m.mistake("Misquoted jersey number", "Paraphrased from truncated recall")

    return m


class TestEvaluationEngine:
    """Test the core evaluation engine."""

    def test_evaluation_returns_all_categories(self, mem):
        result = mem.run_evaluation()
        assert "score" in result
        assert "metrics" in result
        metrics = result["metrics"]
        assert "recall" in metrics
        assert "knowledge" in metrics
        assert "probes" in metrics
        assert "sessions" in metrics
        assert "overall_score" in metrics

    def test_evaluation_score_in_range(self, mem):
        result = mem.run_evaluation()
        assert 0 <= result["score"] <= 100

    def test_evaluation_stores_snapshot(self, mem):
        mem.run_evaluation(notes="test run")
        evals = mem.db.get_evaluations(limit=5)
        assert len(evals) >= 1
        assert evals[0]["notes"] == "test run"
        assert "recall" in evals[0]["metrics"]

    def test_evaluation_comparison_on_second_run(self, mem):
        mem.run_evaluation(notes="first")
        result = mem.run_evaluation(notes="second")
        assert result["comparison"] is not None
        assert "delta" in result["comparison"]
        assert "direction" in result["comparison"]
        assert "category_deltas" in result["comparison"]

    def test_no_comparison_on_first_run(self, mem):
        result = mem.run_evaluation()
        assert result["comparison"] is None


class TestRecallQualityMetrics:
    """Test recall quality evaluation."""

    def test_recall_quality_with_probes(self, rich_mem):
        result = rich_mem.run_evaluation()
        recall = result["metrics"]["recall"]
        assert recall["probe_count"] > 0
        assert "hit_rate" in recall
        assert "avg_rank" in recall or recall["avg_rank"] is None
        assert "miss_rate" in recall

    def test_recall_quality_without_probes(self, mem):
        result = mem.run_evaluation()
        recall = result["metrics"]["recall"]
        assert recall["probe_count"] == 0
        assert recall["subscore"] == 50  # Default when no data

    def test_perfect_recall_scores_high(self, rich_mem):
        result = rich_mem.run_evaluation()
        recall = result["metrics"]["recall"]
        # With facts present that match probes, hit rate should be > 0
        if recall["probe_count"] > 0 and recall["hit_rate"] is not None:
            assert recall["hit_rate"] > 0


class TestKnowledgeHealthMetrics:
    """Test knowledge health evaluation."""

    def test_knowledge_health_counts(self, rich_mem):
        result = rich_mem.run_evaluation()
        k = result["metrics"]["knowledge"]
        assert k["active_facts"] >= 7  # We added 7 facts
        assert k["avg_confidence"] > 0
        assert "stale_facts" in k
        assert "tiers" in k

    def test_knowledge_health_confidence_range(self, rich_mem):
        result = rich_mem.run_evaluation()
        k = result["metrics"]["knowledge"]
        assert 0 <= k["avg_confidence"] <= 1

    def test_knowledge_health_recent_activity(self, rich_mem):
        result = rich_mem.run_evaluation()
        k = result["metrics"]["knowledge"]
        assert k["recent_7d_created"] >= 7  # All facts are recent


class TestProbeTrending:
    """Test probe trending evaluation."""

    def test_probe_trending_counts(self, rich_mem):
        result = rich_mem.run_evaluation()
        p = result["metrics"]["probes"]
        assert p["total_probes"] > 0
        assert "by_type" in p
        assert "auto" in p["by_type"]
        assert "user" in p["by_type"]

    def test_probe_trending_after_calibration(self, rich_mem):
        # Run probes first so they have run history
        rich_mem.run_probes()
        result = rich_mem.run_evaluation()
        p = result["metrics"]["probes"]
        assert p["ever_run"] > 0
        assert "current_pass_rate" in p

    def test_regressed_probe_detection(self, rich_mem):
        # Create a probe, make it pass once, then fail
        probe = rich_mem.add_probe("Test regression?", "NONEXISTENT_VALUE_XYZ")
        rich_mem.db.update_probe_result(probe["id"], True)  # Fake a pass
        rich_mem.db.update_probe_result(probe["id"], False)  # Now it fails
        result = rich_mem.run_evaluation()
        p = result["metrics"]["probes"]
        assert p["regressed"] >= 1


class TestSessionQuality:
    """Test session quality evaluation."""

    def test_session_metrics(self, rich_mem):
        result = rich_mem.run_evaluation()
        s = result["metrics"]["sessions"]
        assert s["total_sessions"] >= 1
        assert "crash_rate" in s
        assert "avg_facts_per_session" in s
        assert s["total_mistakes"] >= 1
        assert s["total_decisions"] >= 1


class TestEvaluationHandler:
    """Test the MCP handler formatting."""

    def test_handler_returns_report(self, rich_mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=rich_mem.agent_dir)
        handlers._memory = rich_mem
        handlers._session_started = True

        output = handlers.handle_evaluate("test evaluation")
        assert "Evaluation:" in output
        assert "/100" in output
        assert "Recall Quality:" in output
        assert "Knowledge Health:" in output
        assert "Probe Trending:" in output
        assert "Session Quality:" in output

    def test_handler_shows_grade(self, rich_mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=rich_mem.agent_dir)
        handlers._memory = rich_mem
        handlers._session_started = True

        output = handlers.handle_evaluate()
        assert any(g in output for g in ("HEALTHY", "FAIR", "DEGRADED", "CRITICAL"))

    def test_handler_shows_comparison(self, rich_mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=rich_mem.agent_dir)
        handlers._memory = rich_mem
        handlers._session_started = True

        handlers.handle_evaluate("first")
        output = handlers.handle_evaluate("second")
        assert "vs Previous" in output

    def test_handler_comparison_shows_direction(self, rich_mem):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=rich_mem.agent_dir)
        handlers._memory = rich_mem
        handlers._session_started = True

        handlers.handle_evaluate("first")
        output = handlers.handle_evaluate("second")
        assert any(d in output for d in ("improving", "degrading", "stable"))


class TestEvaluationDB:
    """Test evaluation storage and retrieval."""

    def test_insert_and_retrieve(self, mem):
        mem.db.insert_evaluation(75.0, {"test": True}, "test notes")
        evals = mem.db.get_evaluations()
        assert len(evals) == 1
        assert evals[0]["score"] == 75.0
        assert evals[0]["metrics"]["test"] is True
        assert evals[0]["notes"] == "test notes"

    def test_get_last_evaluation(self, mem):
        mem.db.insert_evaluation(60.0, {"run": 1})
        mem.db.insert_evaluation(80.0, {"run": 2})
        last = mem.db.get_last_evaluation()
        assert last is not None
        assert last["score"] == 80.0

    def test_get_last_evaluation_empty(self, mem):
        last = mem.db.get_last_evaluation()
        assert last is None

    def test_evaluations_ordered_by_recency(self, mem):
        for i in range(5):
            mem.db.insert_evaluation(float(i * 20), {"run": i})
        evals = mem.db.get_evaluations(limit=3)
        assert len(evals) == 3
        # Most recent first
        assert evals[0]["score"] == 80.0
        assert evals[1]["score"] == 60.0
        assert evals[2]["score"] == 40.0
