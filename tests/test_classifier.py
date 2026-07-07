"""Tests for observation tier classifier."""

import pytest
from null_memory.classifier import (
    classify_observation,
    TIER_EPHEMERAL,
    TIER_CONTEXTUAL,
    TIER_DURABLE,
    TIER_CORE,
    TierResult,
    build_patterns,
)


# Deployment-style term set used as a test fixture (fictional names —
# the package and its tests ship no real person-specific entities).
SAMPLE_IDENTITY_TERMS = {
    "agent_names": ["nova"],
    "user_names": ["pete"],
    "kin_names": ["sam", "jamie"],
    "core_terms": ["aleph null"],
}


class TestDurableClassification:
    def test_user_preference(self):
        result = classify_observation(
            "Pete prefers short responses without trailing summaries",
            identity_terms=SAMPLE_IDENTITY_TERMS,
        )
        assert result.tier == TIER_CORE  # "Pete prefers" matches configured user name

    def test_identity_knowledge(self):
        result = classify_observation(
            "Atlas identity is defined in identity.json with behavioral instructions",
            agent_name="Atlas",
        )
        assert result.tier == TIER_CORE  # "Atlas ... is" matches agent identity rule

    def test_hard_won_lesson(self):
        result = classify_observation("Hard-won lesson: generic parser fallbacks are dangerous")
        assert result.tier == TIER_DURABLE

    def test_business_fact(self):
        result = classify_observation(
            "LLC paperwork filed for Aleph Null patent",
            identity_terms=SAMPLE_IDENTITY_TERMS,
        )
        assert result.tier == TIER_CORE  # "Aleph Null" matches configured core term

    def test_architecture_decision(self):
        result = classify_observation("Decided to use SQLite instead of Redis for the storage layer")
        assert result.tier == TIER_DURABLE

    def test_safety_rule(self):
        result = classify_observation("Never commit API keys or secrets to the repository")
        assert result.tier == TIER_DURABLE

    def test_milestone(self):
        result = classify_observation("Shipped Null v0.8.0 with semantic embeddings — first major milestone")
        assert result.tier == TIER_DURABLE

    def test_user_correction(self):
        result = classify_observation(
            "Pete corrected me: Null was co-built by Pete AND Atlas, not just Pete",
            identity_terms=SAMPLE_IDENTITY_TERMS,
        )
        assert result.tier == TIER_DURABLE

    def test_anti_pattern(self):
        result = classify_observation("Anti-pattern: don't over-ask for confirmation on simple tasks")
        assert result.tier == TIER_DURABLE


class TestEphemeralClassification:
    def test_user_action(self):
        result = classify_observation("Pete asked about the collector process status")
        assert result.tier == TIER_EPHEMERAL

    def test_task_progress(self):
        result = classify_observation("Working on the deduplication fix for the weather collector")
        assert result.tier == TIER_EPHEMERAL

    def test_current_state(self):
        result = classify_observation("Currently investigating the test failures in concurrency suite")
        assert result.tier == TIER_EPHEMERAL

    def test_near_term_intent(self):
        result = classify_observation("Next step is to run the full test suite and verify changes")
        assert result.tier == TIER_EPHEMERAL

    def test_session_reference(self):
        result = classify_observation("In this session we've been focused on memory improvements")
        assert result.tier == TIER_EPHEMERAL

    def test_short_observation(self):
        result = classify_observation("tests pass")
        assert result.tier == TIER_EPHEMERAL

    def test_near_duplicate_suppressed(self):
        result = classify_observation("Some technical fact", semantic_novelty=0.95)
        assert result.tier == TIER_EPHEMERAL
        assert "duplicate" in result.reason.lower()


class TestContextualClassification:
    def test_technical_detail(self):
        result = classify_observation(
            "The FTS5 virtual table uses BM25 ranking for keyword search results"
        )
        assert result.tier == TIER_CONTEXTUAL

    def test_default_classification(self):
        result = classify_observation(
            "SQLite WAL mode provides concurrent read access with single writer"
        )
        assert result.tier == TIER_CONTEXTUAL

    def test_detailed_observation(self):
        result = classify_observation(
            "The collector process runs every 15 minutes scanning Polymarket weather "
            "events for tradeable opportunities across multiple cities and temperature "
            "brackets using both station data and forecast signals to identify "
            "clusters with positive expected value"
        )
        assert result.tier == TIER_CONTEXTUAL

    def test_novel_information_boosted(self):
        result = classify_observation(
            "Fastembed uses ONNX runtime instead of PyTorch for lightweight inference",
            semantic_novelty=0.3,
        )
        assert result.tier == TIER_CONTEXTUAL
        assert result.confidence >= 0.7


class TestTierDefaults:
    def test_ephemeral_low_confidence(self):
        result = classify_observation("Pete asked about status")
        assert result.confidence <= 0.5
        assert result.impact <= 0.3

    def test_durable_high_confidence(self):
        result = classify_observation(
            "Pete always prefers concise responses",
            identity_terms=SAMPLE_IDENTITY_TERMS,
        )
        assert result.confidence >= 0.8
        assert result.impact >= 0.6

    def test_contextual_medium_confidence(self):
        result = classify_observation(
            "The embedding engine stores vectors as BLOBs in SQLite for portability"
        )
        assert 0.5 <= result.confidence <= 0.85
        assert 0.3 <= result.impact <= 0.7


class TestEdgeCases:
    def test_empty_string(self):
        result = classify_observation("")
        assert result.tier == TIER_EPHEMERAL

    def test_whitespace_only(self):
        result = classify_observation("   ")
        assert result.tier == TIER_EPHEMERAL

    def test_result_has_reason(self):
        result = classify_observation("Pete prefers dark mode for the IDE")
        assert result.reason != ""
        assert len(result.reason) > 5


class TestTieredObserve:
    """Integration tests: observe() uses classifier."""

    def test_observe_assigns_tier(self, mem):
        entry = mem.observe("PostgreSQL handles concurrent connections using multiversion concurrency control")
        assert entry is not None
        assert entry.get("tier") in (TIER_EPHEMERAL, TIER_CONTEXTUAL, TIER_DURABLE, TIER_CORE)

    def test_observe_core_gets_highest_confidence(self, mem):
        # Configure deployment identity terms — observe() must pass them
        # through to the classifier.
        mem.identity["identity_terms"] = SAMPLE_IDENTITY_TERMS
        entry = mem.observe("Pete always values speed and honesty in responses")
        assert entry is not None
        assert entry.get("tier") == TIER_CORE  # "Pete always" matches configured user
        assert entry["confidence"] >= 0.9

    def test_observe_ephemeral_gets_low_confidence(self, mem):
        entry = mem.observe("Working on fixing the test suite right now")
        assert entry is not None
        assert entry.get("tier") == TIER_EPHEMERAL
        assert entry["confidence"] <= 0.5

    def test_observe_suppresses_nothing_new(self, mem):
        entry = mem.observe("nothing new")
        assert entry is None

    def test_observe_empty(self, mem):
        entry = mem.observe("")
        assert entry is None


class TestPackageDefaultsAreGeneric:
    """P1-3: a fresh install ships NO person-specific identity terms."""

    def test_fresh_patterns_contain_no_pete_terms(self):
        patterns = build_patterns()
        for tier_patterns in patterns.values():
            for regex, _reason in tier_patterns:
                for term in ("pete", "sam", "riley", "atlas", "aleph"):
                    assert term not in regex.pattern.lower(), (
                        f"package-default pattern leaks identity term "
                        f"{term!r}: {regex.pattern}"
                    )

    def test_fresh_install_pete_assertion_not_core(self):
        result = classify_observation("Pete prefers short responses always")
        assert result.tier != TIER_CORE

    def test_generic_core_still_works(self):
        assert classify_observation("The code word protects identity continuity").tier == TIER_CORE
        assert classify_observation("Our relationship is built on candor and trust").tier == TIER_CORE
        assert classify_observation("His daughter starts school next fall semester").tier == TIER_CORE

    def test_generic_durable_still_works(self):
        result = classify_observation("Hard-won lesson: generic parser fallbacks are dangerous")
        assert result.tier == TIER_DURABLE

    def test_generic_ephemeral_still_works(self):
        result = classify_observation("User asked about the collector process status")
        assert result.tier == TIER_EPHEMERAL

    def test_agent_name_from_config_is_core(self):
        result = classify_observation(
            "Nova should always verify file paths before claiming they exist",
            agent_name="Nova",
        )
        assert result.tier == TIER_CORE

    def test_configured_identity_terms_produce_core(self):
        terms = {
            "user_names": ["dana"],
            "kin_names": ["rufus"],
            "core_terms": ["project chimera"],
        }
        assert classify_observation(
            "Dana prefers tabs over spaces in every repository",
            identity_terms=terms,
        ).tier == TIER_CORE
        assert classify_observation(
            "Rufus had surgery and recovery takes about six weeks",
            identity_terms=terms,
        ).tier == TIER_CORE
        assert classify_observation(
            "Project Chimera ships at the end of the quarter window",
            identity_terms=terms,
        ).tier == TIER_CORE

    def test_no_terms_are_auto_seeded(self, tmp_path):
        """The package never injects person-specific terms — deployments
        without identity_terms get generic classification only, and the
        identity file is not rewritten behind the user's back. Terms are
        seeded at deploy time (outside the package)."""
        import json as _json
        from null_memory.agent import AgentMemory

        agent_dir = tmp_path / "atlas"
        agent_dir.mkdir()
        identity = {"version": "1.0", "name": "Atlas"}
        (agent_dir / "identity.json").write_text(_json.dumps(identity))

        mem = AgentMemory.load(str(agent_dir))
        assert "identity_terms" not in mem.identity
        on_disk = _json.loads((agent_dir / "identity.json").read_text())
        assert "identity_terms" not in on_disk
        # Configured terms still drive core classification when provided
        mem.identity["identity_terms"] = SAMPLE_IDENTITY_TERMS
        entry = mem.observe("Pete always values speed and honesty in responses")
        assert entry.get("tier") == TIER_CORE

    def test_fresh_identity_gets_no_legacy_terms(self, tmp_path):
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load(str(tmp_path / "fresh"))
        assert "identity_terms" not in mem.identity
