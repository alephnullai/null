"""P0-6 — batched access updates + surfaced embedding failures."""

from __future__ import annotations


class TestBatchAccess:
    def test_batch_updates_all_rows_once(self, mem):
        a = mem.learn("alpha fact about caching layers", confidence=0.9)
        b = mem.learn("beta fact about cache invalidation", confidence=0.9)
        mem.db.update_facts_access_batch([a["id"], b["id"]])
        mem.db.conn.commit()
        ra = mem.db.get_fact_by_id(a["id"])
        rb = mem.db.get_fact_by_id(b["id"])
        assert ra["access_count"] == 1
        assert rb["access_count"] == 1
        assert ra["last_accessed"] is not None

    def test_batch_with_empty_list_is_noop(self, mem):
        mem.db.update_facts_access_batch([])  # must not raise

    def test_recall_tracks_access(self, mem):
        f = mem.learn("the deploy pipeline uses blue-green rollouts", confidence=0.9)
        mem.recall("deploy pipeline rollouts")
        row = mem.db.get_fact_by_id(f["id"])
        assert row["access_count"] >= 1


class TestMetaCounters:
    def test_bump_and_read(self, mem):
        assert mem.db.get_meta("test_counter") is None
        mem.db.bump_meta_counter("test_counter")
        mem.db.bump_meta_counter("test_counter")
        mem.db.conn.commit()
        assert mem.db.get_meta("test_counter") == "2"

    def test_set_meta_upserts(self, mem):
        mem.db.set_meta("k", "v1")
        mem.db.set_meta("k", "v2")
        mem.db.conn.commit()
        assert mem.db.get_meta("k") == "v2"


class TestEmbedFailureSurfacing:
    def test_note_embed_failure_counts(self, mem):
        mem._note_embed_failure("recall.semantic", RuntimeError("onnx exploded"))
        mem._note_embed_failure("learn.auto_embed", RuntimeError("again"))
        findings = mem.diagnose()
        assert findings["embed_failures"] == 2
        assert "learn.auto_embed" in findings["embed_failures_last"]

    def test_status_shows_failures(self, mem):
        mem._note_embed_failure("recall.semantic", RuntimeError("boom"))
        out = mem.status()
        assert "Embedding failures: 1" in out

    def test_status_silent_when_healthy(self, mem):
        assert "Embedding failures" not in mem.status()

    def test_doctor_handler_reports_failures(self, tmp_path):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=str(tmp_path))
        handlers.memory._note_embed_failure("recall.semantic", RuntimeError("boom"))
        out = handlers.handle_doctor()
        assert "swallowed embedding failures" in out
