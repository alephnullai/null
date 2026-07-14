"""P2-15 — vectorized cosine dedup over stored embeddings.

No model required: these tests store synthetic vectors directly, so they
run without fastembed installed.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from null_memory.embeddings import EmbeddingEngine


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def engine(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "emb.db"))
    return EmbeddingEngine(conn)


class TestFindDuplicatePairs:
    def test_finds_near_duplicates(self, engine):
        base = np.random.default_rng(7).normal(size=384)
        near = base + np.random.default_rng(8).normal(size=384) * 0.05
        far = np.random.default_rng(9).normal(size=384)
        engine.store_embedding("a", _unit(base))
        engine.store_embedding("b", _unit(near))
        engine.store_embedding("c", _unit(far))

        pairs = engine.find_duplicate_pairs(["a", "b", "c"], threshold=0.85)
        assert len(pairs) == 1
        id_a, id_b, sim = pairs[0]
        assert {id_a, id_b} == {"a", "b"}
        assert sim >= 0.85

    def test_respects_candidate_set(self, engine):
        v = _unit(np.ones(384))
        engine.store_embedding("a", v)
        engine.store_embedding("b", v)
        engine.store_embedding("m_1", v)  # mistake embedding — excluded by caller
        pairs = engine.find_duplicate_pairs(["a", "b"], threshold=0.99)
        assert all(not p[0].startswith("m_") and not p[1].startswith("m_")
                   for p in pairs)
        assert len(pairs) == 1

    def test_empty_and_single(self, engine):
        assert engine.find_duplicate_pairs([]) == []
        engine.store_embedding("solo", _unit(np.ones(384)))
        assert engine.find_duplicate_pairs(["solo"]) == []

    def test_blockwise_matches_naive(self, engine):
        rng = np.random.default_rng(42)
        ids = [f"f{i}" for i in range(50)]
        vecs = {}
        for fid in ids:
            v = _unit(rng.normal(size=384))
            vecs[fid] = v
            engine.store_embedding(fid, v)
        # tiny block size forces multiple blocks
        pairs_blocked = engine.find_duplicate_pairs(ids, threshold=0.2,
                                                    block_size=7)
        pairs_one = engine.find_duplicate_pairs(ids, threshold=0.2,
                                                block_size=4096)
        assert ({(a, b) for a, b, _ in pairs_blocked}
                == {(a, b) for a, b, _ in pairs_one})


class TestGcUsesCosinePath:
    def test_dedup_merges_via_stored_vectors(self, mem):
        # Lexically distinct (so learn()'s own dedup doesn't merge them);
        # the synthetic vectors below are what make them "duplicates".
        f1 = mem.learn("the cache invalidation strategy uses write-through",
                       confidence=0.9)
        f2 = mem.learn("stale entries get rewritten on every store operation",
                       confidence=0.5)
        f3 = mem.learn("the frontend uses server-side rendering",
                       confidence=0.8)
        assert len({f1["id"], f2["id"], f3["id"]}) == 3

        engine = EmbeddingEngine(mem.db)
        mem._embeddings = engine
        base = _unit(np.random.default_rng(1).normal(size=384))
        near = _unit(base + np.random.default_rng(2).normal(size=384) * 0.002)
        other = _unit(np.random.default_rng(3).normal(size=384))
        engine.store_embedding(f1["id"], base)
        engine.store_embedding(f2["id"], near)
        engine.store_embedding(f3["id"], other)
        mem.db.conn.commit()

        merged = mem._deduplicate_knowledge_sql(mem.db.get_active_facts())
        assert merged == 1
        # Higher-confidence f1 wins; f2 superseded
        row = mem.db.get_fact_by_id(f2["id"])
        assert row["superseded_by"] == f1["id"]
        assert mem.db.get_fact_by_id(f3["id"])["superseded_by"] is None

    def test_falls_back_to_jaccard_without_embeddings(self, mem):
        mem._embeddings = False  # unavailable
        a = mem.learn("deploy uses blue green rollout strategy on vercel cloud",
                      confidence=0.9)
        b = mem.learn("deploy uses blue green rollout strategy on vercel cloud today",
                      confidence=0.4)
        merged = mem._deduplicate_knowledge_sql(mem.db.get_active_facts())
        assert merged == 1
        assert mem.db.get_fact_by_id(b["id"])["superseded_by"] == a["id"]
