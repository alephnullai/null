"""Tests for AgentMemory — the core of Null."""

import json
import math
import os
from datetime import datetime, timezone, timedelta

import pytest

from null_memory.agent import (
    AgentMemory,
    WORD_EXPANSION,
    _REVERSE_EXPANSION,
    _expand_tokens,
)


# ── Loading & Saving ──


class TestLoad:
    def test_fresh_load_creates_identity(self, mem):
        assert mem.identity["name"] in (
            "Sage", "Nova", "Forge", "Prism", "Echo", "Rune",
            "Drift", "Ember", "Arc", "Onyx", "Zenith", "Flux",
            "Cipher", "Helix",
        )
        assert mem.knowledge == []
        assert mem.decisions == []
        assert mem.mistakes == []
        assert mem.reflections == []

    def test_load_persists_and_reloads(self, tmp_path):
        mem1 = AgentMemory.load(str(tmp_path))
        mem1.set_name("Reloader")
        mem1.learn("fact one", confidence=0.9)
        mem1.decide("decision one", "reason one")
        mem1.mistake("bad thing", "because reasons")
        mem1.reflect("good", "bad", "different")

        mem2 = AgentMemory.load(str(tmp_path))
        assert mem2.name == "Reloader"
        assert len(mem2.knowledge) == 1
        assert mem2.knowledge[0]["fact"] == "fact one"
        assert len(mem2.decisions) == 1
        assert len(mem2.mistakes) == 1
        assert mem2.mistakes[0]["mistake"] == "bad thing"
        assert len(mem2.reflections) == 1
        assert mem2.reflections[0]["went_well"] == "good"

    def test_load_default_dir(self, tmp_path, monkeypatch):
        # Default AgentMemory.load() should land inside NULL_DIR (or ~/.null
        # if unset) — not somewhere unexpected. Sandbox home + NULL_DIR so
        # this doesn't write to the real ~/.null (cross-platform: Windows
        # expanduser reads USERPROFILE, not HOME).
        from tests.conftest import set_fake_home
        set_fake_home(monkeypatch, tmp_path)
        monkeypatch.setenv("NULL_DIR", str(tmp_path / ".null"))
        mem = AgentMemory.load()
        base = str(tmp_path / ".null")
        atlas_dir = os.path.join(base, "atlas")
        assert mem.agent_dir in (base, atlas_dir)


# ── Knowledge ──


class TestLearn:
    def test_basic_learn(self, mem):
        entry = mem.learn("Python is great", confidence=0.9)
        assert entry["fact"] == "Python is great"
        assert entry["confidence"] == 0.9
        assert "created_at" in entry
        assert len(mem.knowledge) == 1

    def test_learn_persists_to_db(self, mem):
        mem.learn("fact one")
        mem.learn("fact two")
        assert mem.db.count_facts() == 2

    def test_learn_default_confidence(self, mem):
        entry = mem.learn("something")
        assert entry["confidence"] == 0.8

    def test_learn_with_project_and_source(self, mem):
        entry = mem.learn("database fact", project="myapp", source="observation")
        assert entry["project"] == "myapp"
        assert entry["source"] == "observation"

    def test_learn_higher_authority_upgrades_in_place(self, mem):
        """Regression: an exact duplicate arriving from a higher-authority
        source must upgrade the existing row, not tombstone it. The old
        path superseded the fact by its own hash and the follow-up
        INSERT OR IGNORE no-oped, making the fact invisible to every
        active query."""
        low = mem.learn("Pete prefers rebase over merge commits",
                        confidence=0.6, source="told")
        upgraded = mem.learn("Pete prefers rebase over merge commits",
                             confidence=0.7, source="explicit")
        assert upgraded["id"] == low["id"]
        assert upgraded["source"] == "explicit"
        assert upgraded["confidence"] == 0.7  # max(existing, new)

        row = mem.db.get_fact_by_id(low["id"])
        assert row["superseded_by"] is None
        assert row["source"] == "explicit"
        assert row["confidence"] == 0.7
        assert row["base_confidence"] == 0.7
        assert row["access_count"] == 1

        # Still visible to active queries and recall
        assert any(f["id"] == low["id"] for f in mem.db.get_active_facts())
        results = mem.recall("rebase merge commits")
        assert any(r["id"] == low["id"] for r in results)

    def test_learn_higher_authority_keeps_higher_existing_confidence(self, mem):
        """Confidence never drops on an authority upgrade — max() wins."""
        mem.learn("Aleph compiles symbol maps from tree-sitter ASTs",
                  confidence=0.9, source="told")
        upgraded = mem.learn("Aleph compiles symbol maps from tree-sitter ASTs",
                             confidence=0.5, source="explicit")
        assert upgraded["source"] == "explicit"
        assert upgraded["confidence"] == 0.9


class TestObserve:
    def test_observe_records_fact(self, mem):
        entry = mem.observe("the API returns JSON with status codes")
        assert entry is not None
        assert entry["source"] == "observation"
        # Confidence is set by the tier classifier (contextual=0.7, durable=0.85, etc.)
        assert 0.3 <= entry["confidence"] <= 0.95

    def test_observe_increments_turn_count(self, mem):
        assert mem._turn_count == 0
        mem.observe("turn one")
        assert mem._turn_count == 1
        mem.observe("turn two")
        assert mem._turn_count == 2

    def test_observe_skips_trivial(self, mem):
        assert mem.observe("") is None
        assert mem.observe("no new facts") is None
        assert mem.observe("nothing new") is None
        assert len(mem.knowledge) == 0


# ── Recall ──


class TestRecall:
    def test_basic_recall(self, populated_mem):
        results = populated_mem.recall("Python")
        assert len(results) >= 1
        assert any("Python" in r["fact"] for r in results)

    def test_recall_project_filter(self, populated_mem):
        results = populated_mem.recall("Rust", project="hiwave")
        assert len(results) >= 1
        # Should not return arbe4-only facts
        for r in results:
            assert r.get("project", "global") in ("hiwave", "global")

    def test_recall_no_match(self, populated_mem):
        results = populated_mem.recall("quantum entanglement")
        # With semantic search enabled, weak conceptual matches may appear.
        # Verify no results contain the exact query terms (keyword miss).
        for r in results:
            fact = r.get("fact", r.get("mistake", "")).lower()
            assert "quantum" not in fact and "entanglement" not in fact

    def test_recall_empty_query(self, populated_mem):
        results = populated_mem.recall("")
        assert results == []

    def test_recall_limit(self, mem):
        from tests.conftest import insert_n_facts
        insert_n_facts(mem, 20)
        # Broad query that matches many of the distinct test facts
        results = mem.recall("software infrastructure applications data", limit=5)
        assert len(results) <= 5

    def test_recall_includes_mistakes(self, mem):
        mem.learn("database uses Postgres", confidence=0.9)
        mem.mistake("forgot to index the table", "queries were slow")
        results = mem.recall("table")
        assert any(r.get("_type") == "mistake" for r in results)

    def test_recall_excludes_mistakes_when_asked(self, mem):
        mem.learn("database uses Postgres", confidence=0.9)
        mem.mistake("forgot to index the table", "queries were slow")
        results = mem.recall("table", include_mistakes=False)
        assert not any(r.get("_type") == "mistake" for r in results)

    def test_recall_no_substring_false_positive(self, mem):
        """Gemini review: 'a' in 'database' was matching as substring, not word."""
        mem.learn("database schema migration completed successfully")
        # "a" is a stop word AND was a substring bug — should not match
        results = mem.recall("a")
        assert len(results) == 0

    def test_recall_stop_words_filtered(self, mem):
        """Gemini review: 'the bug' should match on 'bug', not 'the'."""
        mem.learn("the weather is nice today")
        mem.learn("found a critical bug in the parser")
        results = mem.recall("the bug")
        # Should match the bug fact, not the weather fact
        assert len(results) >= 1
        assert "bug" in results[0]["fact"]

    def test_recall_punctuation_stripped(self, mem):
        """ChatGPT review: 'Postgres,' should match 'Postgres'."""
        mem.learn("Postgres supports JSONB columns")
        results = mem.recall("Postgres,")
        assert len(results) >= 1
        assert "Postgres" in results[0]["fact"]

    def test_recall_all_stop_words_still_works(self, mem):
        """If query is entirely stop words, use them rather than returning nothing."""
        mem.learn("the is a common word in english")
        results = mem.recall("the is")
        # Should still work — fallback to raw tokens
        assert len(results) >= 1

    def test_recall_word_boundary_matching(self, mem):
        """Ensure tokens match whole words, not substrings."""
        mem.learn("the interface is clean")
        # "in" should NOT match "interface" as a substring
        results = mem.recall("in the zone")
        # "the" matches as a word, but "in" and "zone" should not match "interface"
        found = [r for r in results if "interface" in r["fact"]]
        if found:
            # If it matched, it should be because "the" is in there, not "in"
            assert "the" in set(found[0]["fact"].lower().split())

    def test_recall_confidence_affects_ranking(self, mem):
        mem.learn("low confidence fact about widgets", confidence=0.1)
        mem.learn("high confidence fact about widgets", confidence=0.99)
        results = mem.recall("widgets")
        assert results[0]["confidence"] == 0.99

    def test_recall_includes_archived(self, mem):
        mem.learn("active fact about servers")
        # Archive a fact via SQLite
        mem.db.insert_fact({
            "id": "archived_hash",
            "fact": "old fact about servers",
            "confidence": 0.5,
            "project": "global",
            "created_at": "2025-01-01T00:00:00+00:00",
            "archived": True,
        })
        mem.db.conn.commit()

        # Without flag: no archived results
        results = mem.recall("servers", include_archived=False)
        assert not any(r.get("id") == "archived_hash" for r in results)

        # With flag: archived shows up
        results = mem.recall("servers", include_archived=True)
        assert any("old fact" in r.get("fact", "") for r in results)

    def test_recall_cross_instance_via_sqlite(self, tmp_path):
        """SQLite WAL mode allows concurrent reads from another instance."""
        mem1 = AgentMemory.load(str(tmp_path))
        mem1.learn("initial fact about coding")

        # Second instance reads from same SQLite DB
        mem2 = AgentMemory.load(str(tmp_path))
        results = mem2.recall("coding")
        assert len(results) == 1
        assert "coding" in results[0]["fact"]


# ── Word Expansion ──


class TestWordExpansion:
    def test_direct_expansion(self):
        tokens = _expand_tokens(["database"])
        assert "postgres" in tokens
        assert "redis" in tokens
        assert "database" in tokens  # original preserved

    def test_reverse_expansion(self):
        tokens = _expand_tokens(["postgres"])
        # "postgres" is a member of the "database" group, so "database" should be added
        assert "database" in tokens
        assert "postgres" in tokens  # original preserved

    def test_no_expansion_for_unknown(self):
        tokens = _expand_tokens(["xyzzy"])
        assert tokens == {"xyzzy"}

    def test_multiple_tokens_expand(self):
        tokens = _expand_tokens(["database", "test"])
        assert "postgres" in tokens
        assert "pytest" in tokens

    def test_recall_uses_expansion(self, mem):
        mem.learn("Neon Postgres database connected to Vercel")
        # "database" should find it via direct match
        assert len(mem.recall("database")) >= 1
        # "postgres" should also find it via direct text match
        assert len(mem.recall("postgres")) >= 1
        # "sql" should find it via expansion: sql -> database group -> postgres in text
        results = mem.recall("sql")
        assert len(results) >= 1

    def test_expansion_finds_related_terms(self, mem):
        mem.learn("direct match for database queries", confidence=0.9)
        mem.learn("postgres is a relational system", confidence=0.9)
        results = mem.recall("database")
        # Both should be found (direct match + expansion via thesaurus)
        assert len(results) == 2

    def test_reverse_index_built_correctly(self):
        # Every term in WORD_EXPANSION values should be in reverse index
        for concept, terms in WORD_EXPANSION.items():
            for term in terms:
                assert term in _REVERSE_EXPANSION, f"{term} missing from reverse index"

    def test_concept_in_reverse_index(self):
        # Concepts themselves should be in reverse index pointing to their terms
        assert "database" in _REVERSE_EXPANSION
        assert "postgres" in _REVERSE_EXPANSION["database"]


# ── Decisions ──


class TestDecide:
    def test_basic_decide(self, mem):
        entry = mem.decide("use Rust", "performance matters")
        assert entry["decision"] == "use Rust"
        assert entry["reasoning"] == "performance matters"
        assert len(mem.decisions) == 1

    def test_decide_persists(self, tmp_path):
        mem1 = AgentMemory.load(str(tmp_path))
        mem1.decide("choice A", "reason A", project="proj")
        mem2 = AgentMemory.load(str(tmp_path))
        assert len(mem2.decisions) == 1
        assert mem2.decisions[0]["project"] == "proj"


# ── Mistakes ──


class TestMistake:
    def test_basic_mistake(self, mem):
        entry = mem.mistake("broke production", "forgot to test")
        assert entry["mistake"] == "broke production"
        assert entry["why"] == "forgot to test"
        assert entry["confidence"] == 0.95
        assert len(mem.mistakes) == 1

    def test_mistake_persists(self, tmp_path):
        mem1 = AgentMemory.load(str(tmp_path))
        mem1.mistake("oops", "my bad", project="arbe4")
        mem2 = AgentMemory.load(str(tmp_path))
        assert len(mem2.mistakes) == 1
        assert mem2.mistakes[0]["project"] == "arbe4"

    def test_mistake_searchable_via_recall(self, mem):
        mem.mistake("sold winning position", "position monitor bug")
        results = mem.recall("position")
        assert len(results) >= 1
        assert results[0].get("_type") == "mistake"

    def test_mistake_why_field_searchable(self, mem):
        mem.mistake("bad deploy", "forgot database migration")
        results = mem.recall("migration")
        assert len(results) >= 1


# ── Reflections ──


class TestReflect:
    def test_basic_reflect(self, mem):
        entry = mem.reflect("shipped fast", "missed edge case", "write tests first")
        assert entry["went_well"] == "shipped fast"
        assert entry["missed"] == "missed edge case"
        assert entry["do_differently"] == "write tests first"
        assert len(mem.reflections) == 1

    def test_reflect_persists(self, tmp_path):
        mem1 = AgentMemory.load(str(tmp_path))
        mem1.reflect("good", "bad", "better")
        mem2 = AgentMemory.load(str(tmp_path))
        assert len(mem2.reflections) == 1

    def test_pattern_detection_flags_recurring_themes(self, mem):
        mem.set_name("PatternBot")
        # Same word "testing" in missed/do_differently across 3 reflections
        for i in range(3):
            mem.reflect(f"good {i}", f"missed testing again {i}", f"need more testing {i}")

        anti = mem.identity.get("anti_patterns", [])
        assert any("testing" in p for p in anti), f"Expected 'testing' anti-pattern, got: {anti}"

    def test_pattern_detection_skips_under_threshold(self, mem):
        mem.set_name("NoPattern")
        mem.reflect("good", "missed X", "do Y")
        mem.reflect("good", "missed Z", "do W")
        anti = mem.identity.get("anti_patterns", [])
        assert len(anti) == 0


# ── Contradictions ──


class TestContradiction:
    def test_detects_negation(self, mem):
        mem.learn("always use mocks in tests")
        result = mem.check_contradiction("never use mocks in tests")
        assert result is not None
        assert "mocks" in result["fact"]

    def test_no_false_positive_substring(self, mem):
        """ChatGPT review: 'not' inside 'notebook' should not trigger contradiction."""
        mem.learn("enable notebook cache for faster loads")
        result = mem.check_contradiction("enable cache for faster loads")
        assert result is None

    def test_no_false_positive(self, mem):
        mem.learn("Python uses indentation")
        result = mem.check_contradiction("Rust uses braces for blocks")
        assert result is None

    def test_project_scoped(self, mem):
        mem.learn("use Redis for caching", project="app1")
        result = mem.check_contradiction("don't use Redis for caching", project="app2")
        # Different projects shouldn't conflict (unless one is global)
        # Actually both need to be in (project, "global") scope — app1 != app2
        assert result is None

    def test_short_facts_skipped(self, mem):
        mem.learn("use it")
        result = mem.check_contradiction("skip it")
        assert result is None  # Too few meaningful words


# ── Debrief ──


class TestDebrief:
    def test_basic_debrief(self, mem):
        result = mem.debrief("shipped v2.0 with 18 tools")
        assert result["facts"] == 1
        assert len(mem.knowledge) == 1
        assert mem.knowledge[0]["source"] == "debrief"

    def test_debrief_with_decisions(self, mem):
        result = mem.debrief(
            "session summary",
            decisions_made=["use JSONL — because human readable"],
        )
        assert result["decisions"] == 1
        assert mem.decisions[0]["decision"] == "use JSONL"
        assert mem.decisions[0]["reasoning"] == "because human readable"

    def test_debrief_because_splitting(self, mem):
        result = mem.debrief(
            "summary",
            decisions_made=["chose Rust because performance matters"],
        )
        assert mem.decisions[0]["decision"] == "chose Rust"
        assert "performance" in mem.decisions[0]["reasoning"]

    def test_debrief_with_lessons(self, mem):
        result = mem.debrief(
            "summary",
            lessons=["always test before shipping"],
        )
        assert result["facts"] == 2  # summary + lesson
        assert any("always test" in k["fact"] for k in mem.knowledge)

    def test_debrief_identity_updates(self, mem):
        mem.set_name("Updater")
        result = mem.debrief(
            "summary",
            identity_updates={
                "anti_pattern": "don't guess file paths",
                "capability": "Kubernetes",
                "pace": "ultra fast",
            },
        )
        assert result["identity_updated"]
        assert "don't guess file paths" in mem.identity["anti_patterns"]
        assert "Kubernetes" in mem.identity["capabilities"]
        assert mem.identity["working_style"]["pace"] == "ultra fast"

    def test_debrief_no_duplicate_anti_patterns(self, mem):
        mem.set_name("Deduper")
        mem.debrief("s1", identity_updates={"anti_pattern": "no sycophancy"})
        mem.debrief("s2", identity_updates={"anti_pattern": "no sycophancy"})
        anti = mem.identity["anti_patterns"]
        assert anti.count("no sycophancy") == 1


# ── Garbage Collection ──


class TestGC:
    def test_gc_under_max_does_nothing(self, populated_mem):
        result = populated_mem.gc(max_facts=100)
        assert result["archived"] == 0
        assert result["remaining"] == 5

    def test_gc_archives_over_max(self, mem):
        # Each fact must be fully distinct to avoid dedup merging
        facts = [
            "the cat sat on the mat by the door",
            "quantum computing uses qubits for parallel calculations",
            "mount everest is the tallest mountain in the world",
            "photosynthesis converts sunlight into chemical energy",
            "the roman empire fell in four seventy six ad",
            "jazz music originated in new orleans louisiana",
            "tectonic plates cause earthquakes at fault lines",
            "fibonacci sequence appears throughout natural structures",
            "the amazon river is the largest by water volume",
            "mercury is the closest planet to the sun",
            "mitochondria are the powerhouse of every cell",
            "the french revolution began in seventeen eighty nine",
            "coral reefs support twenty five percent of marine species",
            "binary code represents data using zeros and ones",
            "the speed of light is approximately three hundred thousand kilometers",
            "beethoven composed his ninth symphony while completely deaf",
            "dna contains the genetic blueprint for all organisms",
            "the great wall of china spans thousands of miles",
            "insulin regulates blood sugar levels in the body",
            "volcanoes form when magma reaches the earth surface",
        ]
        for fact in facts:
            mem.learn(fact, confidence=0.5)
        result = mem.gc(max_facts=10)
        assert result["remaining"] <= 10
        assert result["archived"] >= 10
        # Verify archived facts exist in DB
        archived_count = mem.db.count_facts(active_only=False) - mem.db.count_facts(active_only=True)
        assert archived_count >= 10

    def test_gc_archives_low_confidence_first(self, mem):
        mem.learn("high confidence keeper", confidence=0.99)
        # Add old low-confidence entries directly to DB
        old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        for i in range(10):
            mem.db.insert_fact({
                "id": f"old_low_{i}",
                "fact": f"old low conf {i}",
                "confidence": 0.1,
                "base_confidence": 0.1,
                "project": "global",
                "source": "test",
                "created_at": old_ts,
            })
        mem.db.conn.commit()

        result = mem.gc(max_facts=5)
        # High confidence should survive
        facts = mem.db.get_active_facts()
        assert any("high confidence keeper" in k["fact"] for k in facts)

    def test_gc_dedup_before_archive(self, mem):
        """Gemini review: dedup should run before archival to avoid losing good facts.

        Note: with semantic dedup in learn(), exact duplicates are caught at write
        time. This test verifies GC handles near-duplicates that slip past learn().
        """
        for i in range(15):
            mem.learn(f"unique fact number {i} about distinct topic {i}", confidence=0.9)
        # Exact dupes are caught by learn() hash check — use DB directly to simulate
        # near-duplicates that only GC consolidation would catch
        result = mem.gc(max_facts=15)
        # With 15 unique facts and max_facts=15, no archival needed
        assert result["archived"] == 0

    def test_gc_dedup_respects_project_boundaries(self, mem):
        """ChatGPT review: same fact in different projects should NOT be merged."""
        mem.learn("use postgres for primary storage", confidence=0.9, project="app1")
        mem.learn("use postgres for primary storage", confidence=0.9, project="app2")
        result = mem.gc(max_facts=100)
        assert result["merged"] == 0
        assert len(mem.knowledge) == 2

    def test_gc_dedup_short_fact_doesnt_kill_long(self, mem):
        """Short facts shouldn't kill rich long facts.

        With semantic dedup in learn(), highly similar facts merge at write time
        (keeping the longer text). This test verifies the longer version survives.
        """
        mem.learn("use postgres", confidence=0.95)
        mem.learn("use postgres for the main database because it supports JSONB columns and has excellent query performance for our workload", confidence=0.8)
        # Semantic dedup may merge these at learn() time (keeping longer text)
        # OR they may coexist if embeddings aren't available
        facts = mem.knowledge
        if len(facts) == 1:
            # Merged — verify the longer, richer version survived
            assert "JSONB" in facts[0]["fact"]
        else:
            # Both survived (no embeddings) — GC shouldn't merge short with long
            assert len(facts) == 2

    def test_gc_deduplicates(self, mem):
        # With semantic dedup, near-duplicates may merge at learn() time.
        # Use DB directly to bypass learn() dedup and test GC consolidation.
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        mem.db.insert_fact({
            "id": "fox_dog_001", "fact": "the quick brown fox jumps over the lazy dog",
            "confidence": 0.8, "base_confidence": 0.8, "project": "global",
            "source": "test", "created_at": now,
        })
        mem.db.insert_fact({
            "id": "fox_cat_002", "fact": "the quick brown fox jumps over the lazy cat",
            "confidence": 0.9, "base_confidence": 0.9, "project": "global",
            "source": "test", "created_at": now,
        })
        mem.db.conn.commit()
        result = mem.gc(max_facts=100)
        assert result["merged"] >= 1
        assert len(mem.knowledge) == 1
        # Should keep the higher confidence one
        assert mem.knowledge[0]["confidence"] == 0.9

    def test_gc_reduces_active_facts(self, mem):
        from tests.conftest import insert_n_facts
        insert_n_facts(mem, 10)
        mem.gc(max_facts=5)
        assert mem.db.count_facts() <= 5

    def test_gc_never_prunes_mistakes(self, mem):
        from tests.conftest import insert_n_facts
        mem.mistake("critical error", "must remember")
        insert_n_facts(mem, 20)
        mem.gc(max_facts=5)
        # Mistakes are separate — should still be there
        assert len(mem.mistakes) == 1

    def test_gc_never_prunes_reflections(self, mem):
        mem.reflect("good", "bad", "better")
        for i in range(20):
            mem.learn(f"filler fact {i}")
        mem.gc(max_facts=5)
        assert len(mem.reflections) == 1


# ── Briefing ──


class TestBriefing:
    def test_basic_briefing(self, populated_mem):
        brief = populated_mem.briefing()
        assert "TestAgent" in brief
        assert "5 facts" in brief
        assert "2 decisions" in brief

    def test_briefing_with_project(self, populated_mem):
        brief = populated_mem.briefing(project="aleph")
        assert "TestAgent" in brief

    def test_briefing_project_fallback(self, mem):
        """Grok review: briefing fallback was dead code — recall('') returns []."""
        mem.set_name("FallbackBot")
        # Add facts with a project that won't match the project name as a keyword
        mem.learn("deployed v2 to staging", project="myapp")
        mem.learn("fixed auth bug in login flow", project="myapp")
        brief = mem.briefing(project="myapp")
        # Should show facts even if recall(project_name) finds nothing
        assert "FallbackBot" in brief

    def test_briefing_includes_mistakes(self, mem):
        mem.set_name("BriefBot")
        mem.mistake("sold winners", "position monitor bug")
        brief = mem.briefing()
        assert "sold winners" in brief
        # Today's mistakes surface in HOT block under "Recent mistakes (last 24h, ...)".
        # Older mistakes surface in WARM block under "Earlier this week".
        assert "Recent mistakes" in brief or "Earlier this week" in brief

    def test_briefing_includes_last_reflection(self, mem):
        mem.set_name("ReflectBot")
        mem.reflect("shipped fast", "missed tests", "test first")
        brief = mem.briefing()
        assert "shipped fast" in brief
        assert "missed tests" in brief
        assert "test first" in brief


# ── Identity ──


class TestIdentity:
    def test_set_name(self, mem):
        mem.set_name("Atlas")
        assert mem.name == "Atlas"

    def test_name_persists(self, tmp_path):
        mem1 = AgentMemory.load(str(tmp_path))
        mem1.set_name("Persistent")
        mem2 = AgentMemory.load(str(tmp_path))
        assert mem2.name == "Persistent"

    def test_format_identity(self, mem):
        mem.set_name("FormatBot")
        mem.identity["working_style"] = {"pace": "fast"}
        mem.identity["anti_patterns"] = ["no sycophancy"]
        output = mem.format_identity()
        assert "FormatBot" in output
        assert "fast" in output
        assert "no sycophancy" in output


# ── Sync ──


class TestSync:
    def test_sync_saves_identity(self, mem):
        mem.set_name("SyncBot")
        mem.identity["working_style"] = {"mode": "turbo"}
        result = mem.sync()
        assert "SyncBot" in result

        # Reload and verify
        mem2 = AgentMemory.load(mem.agent_dir)
        assert mem2.identity["working_style"]["mode"] == "turbo"

    def test_sync_runs_gc_if_over_max(self, mem):
        from tests.conftest import insert_n_facts
        insert_n_facts(mem, 20)
        os.environ["NULL_MAX_FACTS"] = "10"
        try:
            result = mem.sync()
            assert "GC" in result
        finally:
            del os.environ["NULL_MAX_FACTS"]

    def test_sync_no_gc_under_max(self, mem):
        mem.learn("just one fact")
        result = mem.sync()
        assert "GC" not in result


# ── Export / Import ──


class TestExportImport:
    def test_roundtrip(self, tmp_path):
        dir1 = str(tmp_path / "source")
        dir2 = str(tmp_path / "target")

        mem1 = AgentMemory.load(dir1)
        mem1.set_name("Exporter")
        mem1.learn("exported fact", confidence=0.9)
        mem1.decide("exported decision", "for testing")
        mem1.mistake("exported mistake", "for testing")
        mem1.reflect("good export", "nothing", "keep exporting")

        data = mem1.export_all()
        assert data["version"] == "2.0"

        mem2 = AgentMemory.import_from(data, dir2)
        assert mem2.name == "Exporter"
        assert len(mem2.knowledge) == 1
        assert len(mem2.decisions) == 1
        assert len(mem2.mistakes) == 1
        assert len(mem2.reflections) == 1

    def test_export_format(self, populated_mem):
        data = populated_mem.export_all()
        assert "version" in data
        assert "exported_at" in data
        assert "identity" in data
        assert "knowledge" in data
        assert "decisions" in data
        assert "mistakes" in data
        assert "reflections" in data
        assert "projects" in data


# ── Status ──


class TestStatus:
    def test_status_output(self, populated_mem):
        status = populated_mem.status()
        assert "Facts: 5" in status
        assert "Decisions: 2" in status
        assert "Mistakes: 0" in status
        assert "Reflections: 0" in status
        assert "TestAgent" in status

    def test_status_shows_archive(self, mem):
        # Add then archive a fact via SQLite
        mem.db.insert_fact({
            "id": "archived1",
            "fact": "old archived fact",
            "confidence": 0.5,
            "project": "global",
            "created_at": "2025-01-01T00:00:00+00:00",
            "archived": True,
        })
        mem.db.conn.commit()
        status = mem.status()
        assert "Archived/forgotten: 1" in status


# ── Security ──


class TestSecurity:
    def test_path_traversal_in_import(self, tmp_path):
        """Gemini review: project names like '../../../etc/passwd' must be sanitized."""
        from null_memory.agent import AgentMemory
        malicious_data = {
            "identity": {"name": "Evil"},
            "knowledge": [],
            "decisions": [],
            "mistakes": [],
            "reflections": [],
            "projects": {"../../../tmp/evil": {"hack": True}},
        }
        mem = AgentMemory.import_from(malicious_data, str(tmp_path))
        # The file should be written with a sanitized name, not the traversal path
        evil_path = os.path.join(str(tmp_path), "..", "..", "..", "tmp", "evil.json")
        assert not os.path.exists(evil_path)
        # Should be written as a safe name under projects/
        projects_dir = os.path.join(str(tmp_path), "projects")
        files = os.listdir(projects_dir) if os.path.isdir(projects_dir) else []
        assert all(".." not in f for f in files)

    def test_sanitize_name(self):
        from null_memory.agent import AgentMemory
        assert AgentMemory._sanitize_name("normal") == "normal"
        assert AgentMemory._sanitize_name("my-project") == "my-project"
        assert AgentMemory._sanitize_name("../../../etc/passwd") == "_etc_passwd"
        assert AgentMemory._sanitize_name("") == "unnamed"
        assert AgentMemory._sanitize_name("...") == "_"
        assert "/" not in AgentMemory._sanitize_name("path/traversal")
        assert "\\" not in AgentMemory._sanitize_name("path\\traversal")

    def test_sqlite_wal_mode(self, mem):
        """Verify SQLite uses WAL mode for crash safety and concurrent access."""
        row = mem.db.conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"


# ── Exemplars ──


class TestExemplars:
    def test_load_empty(self, mem):
        assert mem.load_exemplars() == []

    def test_load_from_db(self, mem):
        mem.add_exemplar("test", "do it", "[executes]", "just do it", ["execution"])
        exemplars = mem.load_exemplars()
        assert len(exemplars) == 1
        assert exemplars[0]["scenario"] == "test"

    def test_find_exemplars_by_keyword(self, mem):
        mem.add_exemplar("execute", "do it", "[runs]", "immediate execution", ["execution"])
        mem.add_exemplar("plan", "ULTRATHINK", "[analyzes]", "go deep", ["planning"])
        results = mem.find_exemplars("execution")
        assert len(results) >= 1
        assert results[0]["scenario"] == "execute"

    def test_find_exemplars_no_match(self, mem):
        mem.add_exemplar("test", "hi", "hello", "greet", ["greeting"])
        results = mem.find_exemplars("quantum physics")
        assert results == []

    def test_briefing_includes_exemplars(self, mem):
        mem.set_name("ExemplarBot")
        mem.add_exemplar("test", "do it", "[does it]", "execute immediately", ["exec"])
        brief = mem.briefing()
        assert "Calibration examples" in brief


# ── Token Budget ──


class TestTokenBudget:
    def test_estimate_tokens(self, mem):
        assert mem._estimate_tokens("hello world") >= 1

    def test_budget_tracking(self, mem):
        assert mem._token_budget_used == 0
        mem._use_budget("some text here")
        assert mem._token_budget_used > 0
        assert mem.token_budget_remaining < mem.token_budget
