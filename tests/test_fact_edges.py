"""P2-16 — relationship edges in a real table instead of related_to JSON."""

from __future__ import annotations

import json

from null_memory.db import _create_and_backfill_fact_edges


class TestEdgeTable:
    def test_add_and_get_roundtrip(self, mem):
        a = mem.learn("fact alpha about the scheduler", confidence=0.9)
        b = mem.learn("fact beta about the worker pool", confidence=0.9)
        mem.db.add_relationship(a["id"], b["id"])
        assert mem.db.get_related_ids(a["id"]) == [b["id"]]
        assert mem.db.get_related_ids(b["id"]) == []

    def test_double_add_is_idempotent(self, mem):
        a = mem.learn("fact alpha about retries", confidence=0.9)
        b = mem.learn("fact beta about backoff", confidence=0.9)
        mem.db.add_relationship(a["id"], b["id"])
        mem.db.add_relationship(a["id"], b["id"])
        assert mem.db.get_related_ids(a["id"]) == [b["id"]]

    def test_missing_source_fact_is_noop(self, mem):
        mem.db.add_relationship("nonexistent", "also-nonexistent")
        assert mem.db.get_related_ids("nonexistent") == []

    def test_backfill_from_json_column(self, mem):
        a = mem.learn("legacy fact with json relations", confidence=0.9)
        b = mem.learn("legacy neighbor fact", confidence=0.9)
        # Simulate a pre-v14 DB state: JSON column populated, no edges
        mem.db.conn.execute(
            "UPDATE facts SET related_to = ? WHERE id = ?",
            (json.dumps([b["id"]]), a["id"]),
        )
        mem.db.conn.execute("DELETE FROM fact_edges")
        mem.db.conn.commit()
        assert mem.db.get_related_ids(a["id"]) == []

        _create_and_backfill_fact_edges(mem.db.conn)
        mem.db.conn.commit()
        assert mem.db.get_related_ids(a["id"]) == [b["id"]]

    def test_backfill_is_idempotent(self, mem):
        a = mem.learn("fact with one neighbor", confidence=0.9)
        b = mem.learn("the neighbor in question", confidence=0.9)
        mem.db.conn.execute(
            "UPDATE facts SET related_to = ? WHERE id = ?",
            (json.dumps([b["id"]]), a["id"]),
        )
        _create_and_backfill_fact_edges(mem.db.conn)
        _create_and_backfill_fact_edges(mem.db.conn)
        mem.db.conn.commit()
        assert mem.db.get_related_ids(a["id"]) == [b["id"]]

    def test_recall_pulls_related_facts(self, mem):
        mem._embeddings = False
        a = mem.learn("the ingestion job parses vendor csv exports nightly",
                      confidence=0.9)
        b = mem.learn("vendor exports arrive via sftp at midnight",
                      confidence=0.9)
        c = mem.learn("completely unrelated topic about office plants",
                      confidence=0.9)
        mem.db.add_relationship(a["id"], c["id"])
        results = mem.recall("ingestion vendor csv", limit=10)
        ids = [r["id"] for r in results]
        assert a["id"] in ids
        # c is associatively pulled in through the edge, despite no
        # lexical overlap with the query
        assert c["id"] in ids
