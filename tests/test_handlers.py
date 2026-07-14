"""Tests for MCP handler layer."""

import json
import pytest

from null_memory.mcp.handlers import NullHandlers


@pytest.fixture
def handlers(tmp_path):
    return NullHandlers(agent_dir=str(tmp_path))


class TestHandlerIdentity:
    def test_identity_returns_name(self, handlers):
        result = handlers.handle_identity()
        assert "[Null]" in result

    def test_name_change(self, handlers):
        handlers.handle_name("TestBot")
        result = handlers.handle_identity()
        assert "TestBot" in result


class TestHandlerObserve:
    def test_observe_records(self, handlers):
        result = handlers.handle_observe("user wants dark mode")
        assert "Observed" in result

    def test_observe_trivial_skipped(self, handlers):
        result = handlers.handle_observe("")
        assert "Nothing new" in result

    def test_observe_catches_contradiction(self, handlers):
        # Semantic dedup merges near-duplicate facts (> 0.85 cosine similarity),
        # so we insert the first fact directly via DB to bypass dedup, then
        # observe a contradictory statement that shares keywords but differs via negation.
        import hashlib
        from datetime import datetime, timezone
        mem = handlers.memory
        if not hasattr(mem, 'db'):
            handlers.handle_identity()
            mem = handlers.memory
        fact_text = "always use mocks in unit tests for external services"
        fid = hashlib.sha256(f"{fact_text}:global".encode()).hexdigest()[:16]
        mem.db.insert_fact({
            "id": fid,
            "fact": fact_text,
            "confidence": 0.9,
            "base_confidence": 0.9,
            "project": "global",
            "source": "explicit",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        mem.db.conn.commit()
        mem._reload_knowledge()
        result = handlers.handle_observe("never use mocks in unit tests for external services")
        assert "WARNING" in result or "contradict" in result.lower()


class TestHandlerRecall:
    def test_recall_finds_learned(self, handlers):
        handlers.handle_learn("Postgres runs on port 5432")
        result = handlers.handle_recall("Postgres")
        assert "5432" in result

    def test_recall_no_match(self, handlers):
        result = handlers.handle_recall("nonexistent topic")
        assert "No knowledge" in result

    def test_recall_word_expansion(self, handlers):
        handlers.handle_learn("Neon Postgres handles our database")
        result = handlers.handle_recall("sql")
        # "sql" expands to database group, which includes postgres
        assert "Recall" in result


class TestHandlerLearn:
    def test_learn_confirms(self, handlers):
        result = handlers.handle_learn("fact one", confidence=0.9)
        assert "Learned" in result
        assert "90%" in result

    def test_learn_warns_contradiction(self, handlers):
        handlers.handle_learn("always deploy on Fridays")
        result = handlers.handle_learn("never deploy on Fridays")
        assert "WARNING" in result


class TestHandlerDecide:
    def test_decide_confirms(self, handlers):
        result = handlers.handle_decide("use Rust", "speed")
        assert "Decision logged" in result


class TestHandlerMistake:
    def test_mistake_records(self, handlers):
        result = handlers.handle_mistake("broke prod", "no tests")
        assert "Mistake recorded" in result
        assert "broke prod" in result
        assert "no tests" in result


class TestHandlerReflect:
    def test_reflect_saves(self, handlers):
        result = handlers.handle_reflect("shipped fast", "missed edge", "test more")
        assert "Reflection saved" in result
        assert "shipped fast" in result
        assert "missed edge" in result
        assert "test more" in result


class TestHandlerDebrief:
    def test_debrief_basic(self, handlers):
        result = handlers.handle_debrief("great session")
        assert "Debrief saved" in result
        assert "1 facts" in result

    def test_debrief_with_lists(self, handlers):
        result = handlers.handle_debrief(
            "summary",
            decisions_made=["chose A — reason A", "chose B — reason B"],
            lessons=["lesson one", "lesson two"],
        )
        assert "2 decisions" in result
        # summary + 2 lessons = 3 facts
        assert "3 facts" in result

    def test_debrief_identity_updates(self, handlers):
        result = handlers.handle_debrief(
            "summary",
            identity_updates={"pace": "fast", "anti_pattern": "no guessing"},
        )
        assert "Identity updated" in result


class TestHandlerGC:
    def test_gc_runs(self, handlers):
        for i in range(5):
            handlers.handle_learn(f"fact {i}")
        result = handlers.handle_gc()
        assert "GC complete" in result


class TestHandlerSync:
    def test_sync_returns_signoff(self, handlers):
        handlers.handle_name("SyncBot")
        result = handlers.handle_sync()
        assert "SyncBot" in result
        assert "signing off" in result


class TestHandlerExportImport:
    def test_export_returns_json(self, handlers):
        handlers.handle_learn("test fact")
        result = handlers.handle_export()
        data = json.loads(result)
        assert data["version"] == "2.0"
        assert len(data["knowledge"]) == 1

    def test_import_restores(self, handlers):
        handlers.handle_learn("original fact")
        exported = handlers.handle_export()
        handlers.handle_name("Changed")
        result = handlers.handle_import(exported)
        assert "Imported" in result

    def test_import_invalid_json(self, handlers):
        result = handlers.handle_import("not json")
        assert "Error" in result


class TestHandlerContext:
    def test_context_no_project(self, handlers):
        result = handlers.handle_context("unknown_project")
        assert "No" in result

    def test_context_finds_relevant_knowledge(self, handlers):
        handlers.handle_learn("aleph uses tree-sitter", project="aleph")
        result = handlers.handle_context("aleph")
        assert "tree-sitter" in result


class TestHandlerContradict:
    def test_contradiction_found(self, handlers):
        handlers.handle_learn("always use mocks in tests")
        result = handlers.handle_contradict("never use mocks in tests")
        assert "Contradiction" in result

    def test_no_contradiction(self, handlers):
        handlers.handle_learn("Python is great")
        result = handlers.handle_contradict("Rust is fast")
        assert "No contradiction" in result


class TestHandlerCheckpoint:
    def test_checkpoint_returns_stats(self, handlers):
        handlers.handle_learn("checkpoint fact")
        result = handlers.handle_checkpoint()
        assert "Checkpoint" in result
        assert "1 total facts" in result
