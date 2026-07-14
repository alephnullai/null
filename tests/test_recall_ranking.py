"""P1-10 (N6) — RRF recall ranking, validated against a frozen probe set.

The corpus and probes below are FROZEN: they encode the recall quality
bar in CI. If a ranking change breaks one of these, that's a regression
in the product's core promise, not a test to update casually.

All probes run lexically (embeddings forced off) so CI doesn't depend on
fastembed/model downloads and results are deterministic.
"""

from __future__ import annotations

import pytest

from null_memory.agent import AgentMemory
from null_memory.memory import recall as recall_mod
from null_memory.migrate_v3 import init_unified_db


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """Frozen corpus: facts across projects + one anchor + one mistake.

    Unified DB (anchors require it); embeddings forced off so the probe
    set runs deterministically in CI."""
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified
    mem._embeddings = False  # force lexical-only — deterministic in CI

    facts = {}
    facts["orion_signals"] = mem.learn(
        "Orion live orders use station signals only, never model forecasts",
        confidence=0.9, project="orion",
    )
    facts["deploy_bluegreen"] = mem.learn(
        "The deploy pipeline uses blue-green rollouts on Vercel",
        confidence=0.9, project="webapp",
    )
    facts["pg_pool"] = mem.learn(
        "Postgres connection pool is capped at 20 connections in production",
        confidence=0.85, project="webapp",
    )
    facts["tabs_pref"] = mem.learn(
        "User prefers explicit type annotations in all Python code",
        confidence=0.95, project="global",
    )
    facts["anchor_origin"] = mem.learn(
        "quiet persistence defines this agent across sessions",
        confidence=0.9, project="global",
    )
    mem.anchor(facts["anchor_origin"]["id"], "origin")
    facts["anchor_rival"] = mem.learn(
        "the system value is quiet persistence",
        confidence=0.9, project="global",
    )
    # Distractors
    mem.learn("The marketing site uses a static landing page",
              confidence=0.7, project="webapp")
    mem.learn("Weekly sync moved to Tuesday mornings",
              confidence=0.6, project="global")

    mem.mistake("Forgot to run database migrations before the deploy",
                "Assumed CI ran them automatically", project="webapp")

    return mem, facts


def _fact_ids(results):
    return [r["id"] for r in results if r.get("_type") == "fact"]


class TestFrozenProbes:
    def test_exact_topic_is_top_result(self, corpus):
        mem, facts = corpus
        results = mem.recall("station signals live orders", limit=5)
        assert _fact_ids(results)[0] == facts["orion_signals"]["id"]

    def test_deploy_query_finds_pipeline(self, corpus):
        mem, facts = corpus
        results = mem.recall("blue-green deploy rollout", limit=5)
        assert facts["deploy_bluegreen"]["id"] in _fact_ids(results)[:3]

    def test_thesaurus_expansion_reaches_postgres(self, corpus):
        mem, facts = corpus
        # "database" expands to postgres/sql/... via the thesaurus
        results = mem.recall("database connection limits", limit=5)
        assert facts["pg_pool"]["id"] in _fact_ids(results)[:3]

    def test_anchor_outranks_equal_lexical_match(self, corpus):
        mem, facts = corpus
        results = mem.recall("quiet persistence", limit=5)
        ids = _fact_ids(results)
        assert ids[0] == facts["anchor_origin"]["id"], (
            "anchored fact must outrank the equally-matching plain fact "
            "via the bounded ANCHOR_PRIOR (no slot-#2 hack anymore)"
        )
        assert facts["anchor_rival"]["id"] in ids

    def test_project_filter_respected(self, corpus):
        mem, facts = corpus
        results = mem.recall("station signals live orders",
                             project="webapp", limit=5)
        assert facts["orion_signals"]["id"] not in _fact_ids(results)

    def test_mistake_surfaces_for_relevant_query(self, corpus):
        mem, facts = corpus
        results = mem.recall("database migrations deploy", limit=10)
        mistakes = [r for r in results if r.get("_type") == "mistake"]
        assert mistakes, "the migrations mistake must surface"

    def test_irrelevant_query_returns_no_strong_match(self, corpus):
        mem, facts = corpus
        results = mem.recall("quantum chromodynamics lattice", limit=5)
        assert results == [] or all(
            r.get("_type") != "fact" for r in results
        ) or len(results) <= 2


class TestRRFMechanics:
    def test_multi_list_confirmation_wins(self, mem):
        """A fact found by both BM25 and trigram outranks single-list hits."""
        mem._embeddings = False
        both = mem.learn("kubernetes operator reconciles the cluster state",
                         confidence=0.8)
        mem.learn("the cluster has nine nodes", confidence=0.8)
        results = mem.recall("kubernetes cluster", limit=5)
        assert _fact_ids(results)[0] == both["id"]

    def test_priors_are_bounded(self, mem):
        """A zero-confidence fact still surfaces when it's the only
        relevant hit — priors shade, they don't bury."""
        mem._embeddings = False
        f = mem.learn("the legacy billing cron runs at 3am UTC",
                      confidence=0.05)
        results = mem.recall("legacy billing cron", limit=5)
        assert f["id"] in _fact_ids(results)

    def test_old_hacks_are_gone(self):
        """Slot-#2 promotion + 2.0x flat anchor multiplier must not come back."""
        import inspect
        src = inspect.getsource(recall_mod.RecallMixin.recall)
        assert "Anchor representation guarantee" not in src
        assert "best_anchor" not in src
        assert "score * 2.0" not in src

    def test_rrf_constants_sane(self):
        assert recall_mod.RRF_K == 60.0
        assert recall_mod.ANCHOR_PRIOR > 1.0
        for w in recall_mod.RRF_LIST_WEIGHTS.values():
            assert 0 < w <= 1.0
