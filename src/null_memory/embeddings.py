"""Semantic embedding engine for Null Memory.

Optional module — requires `fastembed` package.
Install via: pip install null-memory[embeddings]

Uses ONNX runtime (not PyTorch) for lightweight local inference.
Stores embeddings as BLOB in SQLite alongside facts.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Default model — 384 dimensions, ~23MB ONNX, fast and accurate enough
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

# Process-wide cache of loaded models, keyed by model_name. A fastembed model
# (ONNX session + weights) is tens of MB; without sharing, every EmbeddingEngine
# — and a multi-store process (or the 93-file test suite) constructs many —
# lazy-loads its OWN copy, accumulating hundreds of MB until the process is
# OOM-killed (CI ran to ~67% then exited 137). One loaded copy per model.
_MODEL_CACHE: dict[str, Any] = {}

# Schema for embeddings table
_EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_embeddings (
    fact_id TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON fact_embeddings(model);
"""


def _check_fastembed() -> bool:
    """Check if fastembed is installed."""
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _embedding_to_blob(vec: np.ndarray) -> bytes:
    """Convert numpy array to compact bytes for SQLite BLOB storage."""
    return vec.astype(np.float32).tobytes()


def _blob_to_embedding(blob: bytes) -> np.ndarray:
    """Convert SQLite BLOB back to numpy array."""
    return np.frombuffer(blob, dtype=np.float32)


class EmbeddingEngine:
    """Semantic embedding engine with SQLite vector storage.

    Lazy-loads the model on first use. Falls back gracefully
    when fastembed is not installed.
    """

    def __init__(self, conn: Any, model_name: str = DEFAULT_MODEL):
        # `conn` may be a raw sqlite3.Connection OR a NullDB-like object
        # exposing a thread-local `.conn` property. NullDB hands each
        # thread its own connection, so caching a single Connection here
        # would pin every embedding call to the constructing thread's
        # connection (cross-thread commit cross-talk). Storing the SOURCE
        # and resolving `.conn` per access keeps the engine thread-correct
        # while remaining backward compatible with raw connections.
        self._conn_source = conn
        self.model_name = model_name
        self._model: Any = None
        self._available: bool | None = None
        self._initialize_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        source = self._conn_source
        if isinstance(source, sqlite3.Connection):
            return source
        return source.conn  # NullDB-like: thread-local connection

    def _initialize_schema(self) -> None:
        """Create embeddings table if it doesn't exist."""
        self.conn.executescript(_EMBEDDINGS_SCHEMA)

    @property
    def available(self) -> bool:
        """Check if embedding engine is available (fastembed installed)."""
        if self._available is None:
            self._available = _check_fastembed()
            if not self._available:
                logger.info("fastembed not installed — semantic features disabled. "
                            "Install with: pip install null-memory[embeddings]")
        return self._available

    def _ensure_model(self) -> Any:
        """Lazy-load the embedding model."""
        if self._model is None:
            if not self.available:
                raise RuntimeError("fastembed not installed")
            model = _MODEL_CACHE.get(self.model_name)
            if model is None:
                from fastembed import TextEmbedding
                model = TextEmbedding(model_name=self.model_name)
                _MODEL_CACHE[self.model_name] = model
            self._model = model
        return self._model

    def embed(self, text: str) -> np.ndarray:
        """Embed a single text. Returns 384-dim float32 array."""
        model = self._ensure_model()
        # fastembed returns a generator
        embeddings = list(model.embed([text]))
        return np.array(embeddings[0], dtype=np.float32)

    def embed_batch(self, texts: list[str], batch_size: int = 64) -> list[np.ndarray]:
        """Embed multiple texts efficiently. Returns list of arrays."""
        if not texts:
            return []
        model = self._ensure_model()
        embeddings = list(model.embed(texts, batch_size=batch_size))
        return [np.array(e, dtype=np.float32) for e in embeddings]

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two vectors."""
        dot = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(dot / (norm_a * norm_b))

    @staticmethod
    def cosine_similarity_batch(query: np.ndarray, candidates: np.ndarray) -> np.ndarray:
        """Compute cosine similarity between query and multiple candidates.

        Args:
            query: (dim,) array
            candidates: (n, dim) array

        Returns:
            (n,) array of similarities
        """
        if candidates.shape[0] == 0:
            return np.array([])
        # Normalize
        query_norm = query / (np.linalg.norm(query) + 1e-10)
        norms = np.linalg.norm(candidates, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-10)
        candidates_norm = candidates / norms
        return candidates_norm @ query_norm

    # ── Storage ──

    def store_embedding(self, fact_id: str, embedding: np.ndarray,
                        created_at: str = "") -> None:
        """Store a fact's embedding in SQLite."""
        from datetime import datetime, timezone
        if not created_at:
            created_at = datetime.now(timezone.utc).isoformat()
        blob = _embedding_to_blob(embedding)
        self.conn.execute(
            """INSERT OR REPLACE INTO fact_embeddings (fact_id, embedding, model, created_at)
               VALUES (?, ?, ?, ?)""",
            (fact_id, blob, self.model_name, created_at),
        )

    def get_embedding(self, fact_id: str) -> np.ndarray | None:
        """Retrieve a stored embedding. Returns None if not found."""
        row = self.conn.execute(
            "SELECT embedding FROM fact_embeddings WHERE fact_id = ?",
            (fact_id,),
        ).fetchone()
        if row is None:
            return None
        return _blob_to_embedding(row[0])

    def get_embeddings_batch(self, fact_ids: list[str]) -> dict[str, np.ndarray]:
        """Retrieve embeddings for multiple fact IDs."""
        if not fact_ids:
            return {}
        placeholders = ",".join("?" * len(fact_ids))
        rows = self.conn.execute(
            f"SELECT fact_id, embedding FROM fact_embeddings WHERE fact_id IN ({placeholders})",
            fact_ids,
        ).fetchall()
        return {row[0]: _blob_to_embedding(row[1]) for row in rows}

    def get_all_embeddings(self) -> tuple[list[str], np.ndarray]:
        """Get all stored embeddings as (fact_ids, matrix).

        Returns:
            (fact_ids, embeddings) where embeddings is (n, dim) array
        """
        rows = self.conn.execute(
            "SELECT fact_id, embedding FROM fact_embeddings"
        ).fetchall()
        if not rows:
            return [], np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        ids = [row[0] for row in rows]
        vecs = np.array([_blob_to_embedding(row[1]) for row in rows], dtype=np.float32)
        return ids, vecs

    def delete_embedding(self, fact_id: str) -> None:
        """Delete a fact's embedding."""
        self.conn.execute(
            "DELETE FROM fact_embeddings WHERE fact_id = ?",
            (fact_id,),
        )

    def count(self) -> int:
        """Count stored embeddings."""
        row = self.conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()
        return row[0] if row else 0

    def has_embedding(self, fact_id: str) -> bool:
        """Check if a fact has a stored embedding."""
        row = self.conn.execute(
            "SELECT 1 FROM fact_embeddings WHERE fact_id = ? LIMIT 1",
            (fact_id,),
        ).fetchone()
        return row is not None

    # ── Semantic Search ──

    def semantic_search(self, query: str, fact_ids: list[str] | None = None,
                        limit: int = 10) -> list[tuple[str, float]]:
        """Search facts by semantic similarity.

        Args:
            query: Search query text
            fact_ids: Optional list of fact IDs to search within (for reranking)
            limit: Max results to return

        Returns:
            List of (fact_id, similarity) tuples, sorted by similarity desc
        """
        query_vec = self.embed(query)

        if fact_ids:
            # Rerank mode — only search within given fact IDs
            embeddings = self.get_embeddings_batch(fact_ids)
            if not embeddings:
                return []
            ids = list(embeddings.keys())
            matrix = np.array(list(embeddings.values()), dtype=np.float32)
        else:
            # Full search
            ids, matrix = self.get_all_embeddings()
            if not ids:
                return []

        similarities = self.cosine_similarity_batch(query_vec, matrix)
        ranked = sorted(zip(ids, similarities), key=lambda x: -x[1])
        return ranked[:limit]

    def find_duplicate_pairs(self, fact_ids: list[str],
                             threshold: float = 0.85,
                             block_size: int = 1024) -> list[tuple[str, str, float]]:
        """Vectorized near-duplicate detection over stored embeddings (P2-15).

        Replaces the Python-loop O(n²) pairwise Jaccard sweep: similarities
        are computed as blockwise matrix products over the normalized
        embedding matrix — still exact (not approximate), but numpy-speed,
        comfortably fast to tens of thousands of facts. When the corpus
        approaches ~100k, swap this for sqlite-vec / a real ANN index; the
        call signature is already neighbor-oriented.

        Only IDs in ``fact_ids`` participate (callers pass fact ids, which
        keeps mistake `m_` / decision `d_` embeddings out). Requires no
        model — works on stored vectors only.

        Returns [(id_a, id_b, similarity), ...] for pairs at/above
        ``threshold``, sorted by similarity descending.
        """
        emb_map = self.get_embeddings_batch(fact_ids)
        ids = list(emb_map.keys())
        if len(ids) < 2:
            return []
        mat = np.array([emb_map[i] for i in ids], dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-10)
        mat = mat / norms

        pairs: list[tuple[str, str, float]] = []
        n = len(ids)
        for start in range(0, n, block_size):
            block = mat[start:start + block_size]
            sims = block @ mat.T  # (b, n)
            for bi in range(block.shape[0]):
                i = start + bi
                row = sims[bi, i + 1:]  # upper triangle only
                hits = np.nonzero(row >= threshold)[0]
                for off in hits:
                    j = i + 1 + int(off)
                    pairs.append((ids[i], ids[j], float(row[off])))
        pairs.sort(key=lambda t: -t[2])
        return pairs

    def find_similar(self, fact_id: str, limit: int = 5,
                     min_similarity: float = 0.5) -> list[tuple[str, float]]:
        """Find facts similar to a given fact.

        Args:
            fact_id: The reference fact ID
            limit: Max results
            min_similarity: Minimum cosine similarity threshold

        Returns:
            List of (fact_id, similarity) tuples
        """
        query_vec = self.get_embedding(fact_id)
        if query_vec is None:
            return []

        ids, matrix = self.get_all_embeddings()
        if not ids:
            return []

        similarities = self.cosine_similarity_batch(query_vec, matrix)
        results = []
        for fid, sim in zip(ids, similarities):
            if fid != fact_id and sim >= min_similarity:
                results.append((fid, float(sim)))
        results.sort(key=lambda x: -x[1])
        return results[:limit]

    def search_by_prefix(self, query: str, prefix: str,
                         limit: int = 5) -> list[tuple[str, float]]:
        """Search embeddings whose fact_id starts with a prefix.

        Used for mistake embeddings (prefix='m_') stored alongside fact embeddings.
        """
        query_vec = self.embed(query)
        rows = self.conn.execute(
            "SELECT fact_id, embedding FROM fact_embeddings WHERE fact_id LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        if not rows:
            return []
        ids = [row[0] for row in rows]
        matrix = np.array(
            [_blob_to_embedding(row[1]) for row in rows], dtype=np.float32,
        )
        similarities = self.cosine_similarity_batch(query_vec, matrix)
        ranked = sorted(zip(ids, similarities), key=lambda x: -x[1])
        return ranked[:limit]

    # ── Batch Operations ──

    def embed_all_facts(self, facts: list[dict], batch_size: int = 64,
                        skip_existing: bool = True) -> int:
        """Embed all facts and store in SQLite.

        Args:
            facts: List of fact dicts with 'id' and 'fact' keys
            batch_size: Batch size for embedding
            skip_existing: Skip facts that already have embeddings

        Returns:
            Number of facts embedded
        """
        if skip_existing:
            existing = set(
                row[0] for row in
                self.conn.execute("SELECT fact_id FROM fact_embeddings").fetchall()
            )
            facts = [f for f in facts if f["id"] not in existing]

        if not facts:
            return 0

        texts = [f["fact"] for f in facts]
        embeddings = self.embed_batch(texts, batch_size=batch_size)

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        for fact, vec in zip(facts, embeddings):
            self.store_embedding(fact["id"], vec, created_at=now)

        self.conn.commit()
        return len(facts)

    def stats(self) -> dict:
        """Embedding statistics."""
        total = self.count()
        model_rows = self.conn.execute(
            "SELECT model, COUNT(*) FROM fact_embeddings GROUP BY model"
        ).fetchall()
        models = {row[0]: row[1] for row in model_rows}
        return {
            "total_embeddings": total,
            "models": models,
            "available": self.available,
            "model_name": self.model_name,
        }


# ── Background backfill (issue: semantic recall starved of vectors) ────────
#
# A store can accumulate hundreds of facts with almost no embeddings (e.g.
# 1/490 observed live) — semantic recall silently degrades to lexical and
# the identity-coherence machinery has no vectors to compare. Embedding only
# happens on writes or a manual `null embed-all`, so relocated/imported
# stores never catch up. This backfills idle, off the request path.

BACKFILL_ENV = "NULL_EMBED_BACKFILL"          # set to "0" to disable
BACKFILL_COVERAGE_THRESHOLD = 0.9             # skip if >=90% already embedded
BACKFILL_START_DELAY = 15.0                   # let the server finish booting
BACKFILL_CHUNK = 128                          # facts per commit
BACKFILL_CHUNK_PAUSE = 0.25                   # be polite between chunks
# N MCP instances may share one store — only the leader backfills, claiming
# the SAME key HypnosLiveWorker uses (see null_memory.memory.leader for the
# only-one-live-worker-per-DB invariant).
BACKFILL_LEADER_KEY = "hypnos_live_leader"
BACKFILL_LEADER_TTL_SECONDS = 90.0            # matches hypnos_live.LEADER_TTL_SECONDS


def run_embed_backfill(memory, *, coverage_threshold: float = BACKFILL_COVERAGE_THRESHOLD,
                       chunk: int = BACKFILL_CHUNK, pause: float = BACKFILL_CHUNK_PAUSE,
                       _sleep=None) -> int:
    """Embed un-embedded active facts. Returns count embedded (0 = no-op).

    Synchronous core, called from the daemon thread; separated for testing.
    Never raises — semantic search is an enhancement, not a dependency.
    """
    import time as _time
    sleep = _sleep or _time.sleep
    try:
        emb = memory.embeddings
        if emb is None or not emb.available:
            return 0
        facts = memory.db.get_active_facts()
        if not facts:
            return 0
        embedded = emb.count()
        if embedded / max(1, len(facts)) >= coverage_threshold:
            return 0
        logger.info("[embed-backfill] %d facts, %d embedded — backfilling",
                    len(facts), embedded)
        total = 0
        for i in range(0, len(facts), chunk):
            total += emb.embed_all_facts(facts[i:i + chunk], skip_existing=True)
            if i + chunk < len(facts):
                sleep(pause)  # keep write locks short, CPU polite
        logger.info("[embed-backfill] done — %d facts embedded", total)
        return total
    except Exception as e:  # noqa: BLE001
        logger.exception("[embed-backfill] failed (non-fatal)")
        try:
            memory._note_embed_failure("embed_backfill", e)
        except Exception:  # noqa: BLE001 — the counter must never crash the worker
            pass
        return 0


def start_background_backfill(memory, *, delay: float = BACKFILL_START_DELAY,
                              leader_instance_id: str | None = None):
    """Start the idle embed backfill on a daemon thread (or None if disabled).

    Called by the MCP server after boot. NullDB uses per-thread connections,
    so the worker thread gets its own SQLite connection automatically.

    Leader gating: with N instances sharing a store, all of them would
    otherwise embed the same facts concurrently (duplicate CPU, WAL
    contention). The worker claims the shared ``hypnos_live_leader`` key
    before doing anything; pass the in-process HypnosLiveWorker's
    ``instance_id`` as ``leader_instance_id`` so the claim refreshes that
    worker's heartbeat instead of fighting it. Without one (Hypnos
    disabled), the worker claims under its own id.
    """
    import os as _os
    import threading as _threading
    import time as _time
    if _os.environ.get(BACKFILL_ENV, "1") == "0":
        return None

    def _worker():
        _time.sleep(delay)
        # Lazy import — keeps the product-surface import graph light
        # (test_no_heavy_imports) and means a broken leader module can
        # only ever skip the backfill, never break the server.
        lock = None
        try:
            import random as _random

            from null_memory.memory.leader import LeaderLock

            instance_id = leader_instance_id or (
                f"{_os.getpid()}:{_random.randint(1000, 9999)}"
            )
            lock = LeaderLock(memory.db.db_path, BACKFILL_LEADER_KEY, instance_id)
            if not lock.claim_or_refresh(BACKFILL_LEADER_TTL_SECONDS):
                logger.info(
                    "[embed-backfill] not leader for this store — skipping "
                    "(the leader instance backfills)"
                )
                return
        except Exception:  # noqa: BLE001
            logger.exception("[embed-backfill] leader claim failed — skipping")
            return
        finally:
            if lock is not None:
                lock.close()
        run_embed_backfill(memory)

    t = _threading.Thread(target=_worker, name="embed-backfill", daemon=True)
    t.start()
    return t
