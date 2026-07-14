"""Tests for decision outcome tracking."""

import pytest
from null_memory.agent import AgentMemory


@pytest.fixture
def mem(tmp_path):
    m = AgentMemory.load(str(tmp_path / "test_agent"))
    m.start_session(project="test")
    return m


class TestOutcomeDB:
    def test_insert_and_get_outcome(self, mem):
        mem.decide("Use SQLite for storage", "Lightweight, no server needed", project="test")
        decisions = mem.db.get_decisions()
        assert len(decisions) >= 1
        d = decisions[-1]

        result = mem.db.insert_outcome(d["id"], "Works perfectly, fast queries", success=True)
        assert result["success"] is True
        assert result["outcome"] == "Works perfectly, fast queries"

        outcomes = mem.db.get_outcomes(d["id"])
        assert len(outcomes) == 1
        assert outcomes[0]["success"] == 1

    def test_multiple_outcomes_per_decision(self, mem):
        mem.decide("Deploy to production", "Ready for launch", project="test")
        d = mem.db.get_decisions()[-1]

        mem.db.insert_outcome(d["id"], "Initial deploy failed", success=False)
        mem.db.insert_outcome(d["id"], "Fixed config, second deploy succeeded", success=True)

        outcomes = mem.db.get_outcomes(d["id"])
        assert len(outcomes) == 2
        assert outcomes[0]["success"] == 0
        assert outcomes[1]["success"] == 1

    def test_outcome_with_unknown_success(self, mem):
        mem.decide("Try new approach", "Experimental", project="test")
        d = mem.db.get_decisions()[-1]

        result = mem.db.insert_outcome(d["id"], "Results are ambiguous", success=None)
        assert result["success"] is None

        outcomes = mem.db.get_outcomes(d["id"])
        assert outcomes[0]["success"] is None

    def test_find_decision(self, mem):
        mem.decide("Use embeddings for semantic search", "Better recall quality", project="test")
        found = mem.db.find_decision("embeddings", project="test")
        assert found is not None
        assert "embeddings" in found["decision"].lower()

    def test_find_decision_not_found(self, mem):
        found = mem.db.find_decision("nonexistent_xyz_12345")
        assert found is None

    def test_count_outcomes(self, mem):
        assert mem.db.count_outcomes() == 0
        mem.decide("Test decision", "Testing", project="test")
        d = mem.db.get_decisions()[-1]
        mem.db.insert_outcome(d["id"], "It worked", success=True)
        assert mem.db.count_outcomes() == 1


class TestRecordOutcome:
    def test_record_outcome_by_query(self, mem):
        mem.decide("Switch to fastembed", "Lighter than sentence-transformers", project="test")

        result = mem.record_outcome(
            decision_query="fastembed",
            outcome="200MB install, fast inference, good quality",
            success=True,
            project="test",
        )
        assert result is not None
        assert result["success"] is True

    def test_record_outcome_learns_lesson(self, mem):
        mem.decide("Add semantic dedup", "Reduce noise in observations", project="test")
        initial_count = mem.db.count_facts()

        mem.record_outcome(
            decision_query="semantic dedup",
            outcome="Reduced duplicate facts by 30%",
            success=True,
        )
        # Should have created a lesson fact
        assert mem.db.count_facts() > initial_count

    def test_record_outcome_not_found(self, mem):
        result = mem.record_outcome(
            decision_query="nonexistent_decision_xyz",
            outcome="whatever",
        )
        assert result is None

    def test_decisions_with_outcomes(self, mem):
        mem.decide("Choice A", "Reason A", project="test")
        mem.decide("Choice B", "Reason B", project="test")
        decisions = mem.db.get_decisions()
        mem.db.insert_outcome(decisions[-2]["id"], "A worked", success=True)
        mem.db.insert_outcome(decisions[-1]["id"], "B failed", success=False)

        results = mem.db.get_decisions_with_outcomes(project="test")
        assert len(results) >= 2


class TestFuzzyDecisionLookup:
    """P1-4: null_outcome decision lookup survives operator characters
    ('+', '-'), out-of-order words, and scattered phrases — with near-miss
    candidates surfaced when nothing clears the match threshold."""

    AUDIT_DECISION = (
        "Adopted the Aleph+Null public-launch audit plan: ship Week-1 P0s "
        "first (learn() data-loss fix, Nebula CORS allowlist, pyproject "
        "AGPL metadata) with output caps on report length."
    )

    @pytest.fixture
    def audit_mem(self, mem):
        mem.decide(
            self.AUDIT_DECISION,
            "Highest-severity findings gate the public launch",
            project="test",
        )
        return mem

    def test_query_with_plus_and_hyphen_operators(self, audit_mem):
        found = audit_mem.db.find_decision(
            "Aleph+Null audit plan Week-1 P0s", project="test"
        )
        assert found is not None
        assert "audit plan" in found["decision"]

    def test_query_with_scattered_words(self, audit_mem):
        found = audit_mem.db.find_decision(
            "audit plan output caps", project="test"
        )
        assert found is not None
        assert "output caps" in found["decision"]

    def test_query_spanning_separated_phrases(self, audit_mem):
        found = audit_mem.db.find_decision(
            "Week-1 P0s Nebula CORS pyproject AGPL", project="test"
        )
        assert found is not None
        assert "Nebula CORS" in found["decision"]

    def test_record_outcome_via_fuzzy_query(self, audit_mem):
        result = audit_mem.record_outcome(
            decision_query="Aleph+Null audit plan Week-1 P0s",
            outcome="P0s shipped; launch unblocked",
            success=True,
        )
        assert result is not None

    def test_exact_substring_still_wins(self, audit_mem):
        found = audit_mem.db.find_decision("public-launch audit", project="test")
        assert found is not None

    def test_unrelated_query_still_not_found(self, audit_mem):
        assert audit_mem.db.find_decision(
            "kubernetes ingress timeout zzz", project="test"
        ) is None

    def test_candidates_surface_near_misses(self, audit_mem):
        audit_mem.decide("Use SQLite WAL for storage", "Concurrency", project="test")
        # Query overlaps the audit decision on a couple of words but not
        # enough for find_decision's >= 0.5 acceptance.
        candidates = audit_mem.db.find_decision_candidates(
            "audit nebula zebra quagga wombat xylophone", project="test", limit=3
        )
        assert len(candidates) >= 1
        assert any("audit plan" in c["decision"] for c in candidates)

    def test_candidates_empty_for_no_overlap(self, audit_mem):
        assert audit_mem.db.find_decision_candidates(
            "zebra quagga wombat xylophone", project="test"
        ) == []


class TestOutcomeHandlerNearMiss:
    """The null_outcome MCP handler lists top candidates when no decision
    clears the match threshold."""

    def test_not_found_message_lists_candidates(self, tmp_path):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=str(tmp_path / "h"))
        handlers.memory.decide(
            "Adopted the Aleph+Null public-launch audit plan with output caps",
            "Gate the launch on P0s",
            project="test",
        )
        msg = handlers.handle_outcome(
            decision_query="audit nebula zebra quagga wombat xylophone",
            outcome="irrelevant",
        )
        assert "Closest candidates" in msg
        assert "audit plan" in msg

    def test_not_found_message_without_candidates(self, tmp_path):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=str(tmp_path / "h2"))
        msg = handlers.handle_outcome(
            decision_query="zebra quagga wombat",
            outcome="irrelevant",
        )
        assert "No decision matching" in msg
