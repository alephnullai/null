"""Tests for the embedding engine and semantic features."""

import os
import sqlite3
import pytest
import numpy as np

from null_memory.embeddings import (
    EmbeddingEngine,
    _embedding_to_blob,
    _blob_to_embedding,
    _check_fastembed,
    EMBEDDING_DIM,
)


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def engine(conn):
    return EmbeddingEngine(conn)


class TestBlobConversion:
    def test_roundtrip(self):
        vec = np.random.randn(384).astype(np.float32)
        blob = _embedding_to_blob(vec)
        recovered = _blob_to_embedding(blob)
        np.testing.assert_array_almost_equal(vec, recovered)

    def test_blob_is_compact(self):
        vec = np.zeros(384, dtype=np.float32)
        blob = _embedding_to_blob(vec)
        assert len(blob) == 384 * 4  # float32 = 4 bytes

    def test_different_vectors_different_blobs(self):
        a = np.ones(384, dtype=np.float32)
        b = np.zeros(384, dtype=np.float32)
        assert _embedding_to_blob(a) != _embedding_to_blob(b)


class TestEmbeddingEngine:
    def test_schema_created(self, engine, conn):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fact_embeddings'"
        ).fetchone()
        assert row is not None

    def test_store_and_retrieve(self, engine, conn):
        vec = np.random.randn(384).astype(np.float32)
        engine.store_embedding("fact_1", vec, created_at="2026-01-01T00:00:00Z")
        conn.commit()

        retrieved = engine.get_embedding("fact_1")
        assert retrieved is not None
        np.testing.assert_array_almost_equal(vec, retrieved)

    def test_get_nonexistent(self, engine):
        assert engine.get_embedding("nonexistent") is None

    def test_has_embedding(self, engine, conn):
        assert not engine.has_embedding("fact_1")
        vec = np.random.randn(384).astype(np.float32)
        engine.store_embedding("fact_1", vec)
        conn.commit()
        assert engine.has_embedding("fact_1")

    def test_delete_embedding(self, engine, conn):
        vec = np.random.randn(384).astype(np.float32)
        engine.store_embedding("fact_1", vec)
        conn.commit()
        assert engine.count() == 1
        engine.delete_embedding("fact_1")
        conn.commit()
        assert engine.count() == 0

    def test_count(self, engine, conn):
        assert engine.count() == 0
        for i in range(5):
            engine.store_embedding(f"fact_{i}", np.random.randn(384).astype(np.float32))
        conn.commit()
        assert engine.count() == 5

    def test_get_embeddings_batch(self, engine, conn):
        vecs = {}
        for i in range(3):
            v = np.random.randn(384).astype(np.float32)
            engine.store_embedding(f"fact_{i}", v)
            vecs[f"fact_{i}"] = v
        conn.commit()

        batch = engine.get_embeddings_batch(["fact_0", "fact_1", "fact_2", "nonexistent"])
        assert len(batch) == 3
        for fid, vec in batch.items():
            np.testing.assert_array_almost_equal(vec, vecs[fid])

    def test_get_all_embeddings(self, engine, conn):
        for i in range(3):
            engine.store_embedding(f"fact_{i}", np.random.randn(384).astype(np.float32))
        conn.commit()

        ids, matrix = engine.get_all_embeddings()
        assert len(ids) == 3
        assert matrix.shape == (3, 384)

    def test_get_all_embeddings_empty(self, engine):
        ids, matrix = engine.get_all_embeddings()
        assert len(ids) == 0
        assert matrix.shape == (0, EMBEDDING_DIM)

    def test_store_replaces_existing(self, engine, conn):
        vec1 = np.ones(384, dtype=np.float32)
        vec2 = np.zeros(384, dtype=np.float32)
        engine.store_embedding("fact_1", vec1)
        conn.commit()
        engine.store_embedding("fact_1", vec2)
        conn.commit()
        retrieved = engine.get_embedding("fact_1")
        np.testing.assert_array_almost_equal(retrieved, vec2)
        assert engine.count() == 1

    def test_stats(self, engine, conn):
        for i in range(3):
            engine.store_embedding(f"fact_{i}", np.random.randn(384).astype(np.float32))
        conn.commit()
        stats = engine.stats()
        assert stats["total_embeddings"] == 3
        assert stats["available"] == _check_fastembed()


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = np.random.randn(384).astype(np.float32)
        assert EmbeddingEngine.cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-5)

    def test_opposite_vectors(self):
        v = np.random.randn(384).astype(np.float32)
        assert EmbeddingEngine.cosine_similarity(v, -v) == pytest.approx(-1.0, abs=1e-5)

    def test_orthogonal_vectors(self):
        a = np.zeros(384, dtype=np.float32)
        a[0] = 1.0
        b = np.zeros(384, dtype=np.float32)
        b[1] = 1.0
        assert EmbeddingEngine.cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-5)

    def test_zero_vector(self):
        v = np.random.randn(384).astype(np.float32)
        zero = np.zeros(384, dtype=np.float32)
        assert EmbeddingEngine.cosine_similarity(v, zero) == 0.0

    def test_batch_similarity(self):
        query = np.random.randn(384).astype(np.float32)
        candidates = np.random.randn(5, 384).astype(np.float32)
        sims = EmbeddingEngine.cosine_similarity_batch(query, candidates)
        assert sims.shape == (5,)
        # All similarities should be between -1 and 1
        assert all(-1.01 <= s <= 1.01 for s in sims)

    def test_batch_empty(self):
        query = np.random.randn(384).astype(np.float32)
        empty = np.empty((0, 384), dtype=np.float32)
        sims = EmbeddingEngine.cosine_similarity_batch(query, empty)
        assert len(sims) == 0
