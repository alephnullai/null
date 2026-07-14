"""Null Nebula — UMAP projection + HDBSCAN clustering.

Takes the 384-d fact embeddings (from fact_embeddings) and projects them
into 3D coordinates for the galaxy view. Caches results into facts.viz_*
columns so the frontend reads a single SELECT. Re-projection is on-demand
or triggered when the unprocessed-fact count crosses a threshold.

We deliberately DO NOT use Neo4j — the visualization needs point
coordinates + metadata + a live access signal, all of which SQLite
provides with one query per frame.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_UNIFIED_PATH = os.path.expanduser("~/.null/unified.db")
UMAP_MODEL_PATH = os.path.expanduser("~/.null/nebula_umap.pkl")

# How many facts can be unprojected before we auto-rerun on next serve
REPROJECT_THRESHOLD = 50

# Scale factor for Nebula coordinates — UMAP output is roughly [-10, 10];
# multiply to make the galaxy feel spacious in Three.js world-units.
COORD_SCALE = 5.0


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def project_all(
    unified_path: str = DEFAULT_UNIFIED_PATH,
    *,
    force: bool = False,
    min_cluster_size: int = 5,
    n_neighbors: int = 15,
    min_dist: float = 0.1,
    seed: int = 42,
) -> dict:
    """Re-run UMAP + HDBSCAN over every active fact with an embedding.

    Args:
        force: if False, only project facts that don't already have
               viz_x / viz_y / viz_z set.
        min_cluster_size: HDBSCAN parameter — small = more granular clusters.
        n_neighbors / min_dist: UMAP local-structure tuning.

    Returns stats dict: {total, projected, clusters, noise_points,
                         cluster_labels}
    """
    import numpy as np
    from hdbscan import HDBSCAN
    from umap import UMAP

    conn = _connect(unified_path)
    try:
        # Fetch every active fact with an embedding
        rows = conn.execute(
            """
            SELECT f.id, f.fact, f.anchor_type, f.tier,
                   f.viz_x, f.viz_y, f.viz_z, f.cluster_id,
                   e.embedding
            FROM facts f
            JOIN fact_embeddings e ON e.fact_id = f.id
            WHERE f.archived = 0 AND f.forgotten = 0
              AND f.superseded_by IS NULL
            """
        ).fetchall()
        if not rows:
            return {"total": 0, "projected": 0, "clusters": 0,
                    "noise_points": 0, "cluster_labels": {}}

        # Deserialize embeddings as float32[384]
        ids = [r["id"] for r in rows]
        texts = [r["fact"] for r in rows]
        vectors = np.stack([
            np.frombuffer(r["embedding"], dtype=np.float32)
            for r in rows
        ])

        if not force:
            # Only re-project when any fact is missing coords
            missing = sum(1 for r in rows if r["viz_x"] is None)
            if missing == 0:
                return {"total": len(rows), "projected": 0, "clusters": 0,
                        "noise_points": 0, "cluster_labels": {},
                        "skipped_reason": "all_already_projected"}

        # UMAP 384 → 3
        umap = UMAP(
            n_components=3,
            n_neighbors=n_neighbors,
            min_dist=min_dist,
            metric="cosine",
            random_state=seed,
        )
        raw_coords = umap.fit_transform(vectors)
        # Remember the mean so we can apply it to NEW facts later via
        # `transform_new()` — keeps incremental placements consistent with
        # the existing galaxy.
        mean_offset = raw_coords.mean(axis=0)
        coords = (raw_coords - mean_offset) * COORD_SCALE

        # Cache the fitted model + mean offset for incremental transforms
        try:
            import pickle
            with open(UMAP_MODEL_PATH, "wb") as fh:
                pickle.dump({
                    "umap": umap,
                    "mean_offset": mean_offset,
                    "coord_scale": COORD_SCALE,
                }, fh)
        except Exception as e:
            logger.warning("failed to cache UMAP model: %s", e)

        # HDBSCAN clustering in the reduced space
        clusterer = HDBSCAN(
            min_cluster_size=min_cluster_size,
            metric="euclidean",
        )
        cluster_ids = clusterer.fit_predict(coords)
        # Note: HDBSCAN returns -1 for noise points
        n_clusters = int(max(cluster_ids) + 1) if len(cluster_ids) else 0
        n_noise = int((cluster_ids == -1).sum())

        # Generate TF-IDF-style labels per cluster (simple top-keywords)
        cluster_labels = _label_clusters(texts, cluster_ids)

        # Write back — one UPDATE per fact, batched in a transaction
        now_coords = list(zip(
            [float(c[0]) for c in coords],
            [float(c[1]) for c in coords],
            [float(c[2]) for c in coords],
            [int(cid) for cid in cluster_ids],
            ids,
        ))
        with conn:
            conn.executemany(
                """UPDATE facts
                   SET viz_x = ?, viz_y = ?, viz_z = ?, cluster_id = ?
                   WHERE id = ?""",
                now_coords,
            )

        return {
            "total": len(rows),
            "projected": len(rows),
            "clusters": n_clusters,
            "noise_points": n_noise,
            "cluster_labels": cluster_labels,
        }
    finally:
        conn.close()


def _label_clusters(texts: list[str], cluster_ids: "Any") -> dict[int, list[str]]:
    """Top-3 TF-IDF-ish keywords per cluster.

    Avoids LLM dependency — v2 can upgrade to synthesized labels. For now:
    per-cluster word frequency minus corpus frequency, top 3.
    """
    import re

    def tokenize(text: str) -> list[str]:
        return [t for t in re.findall(r"[a-z][a-z0-9_]+", text.lower())
                if len(t) > 3]

    # Corpus-wide word frequency
    corpus_counts: Counter[str] = Counter()
    for t in texts:
        corpus_counts.update(tokenize(t))
    corpus_total = max(1, sum(corpus_counts.values()))

    # Per-cluster counts
    by_cluster: dict[int, Counter] = {}
    for text, cid in zip(texts, cluster_ids):
        cid_int = int(cid)
        if cid_int < 0:
            continue  # Skip noise points in label generation
        by_cluster.setdefault(cid_int, Counter()).update(tokenize(text))

    # Score: p(word|cluster) / p(word|corpus); top 3
    labels: dict[int, list[str]] = {}
    STOP = {
        "this", "that", "with", "from", "have", "been", "were", "they",
        "their", "said", "pete", "atlas", "null", "will", "when", "what",
        "just", "also", "more", "like", "some", "could", "would", "should",
        "about", "still", "into", "needs", "need", "done",
    }
    for cid, counts in by_cluster.items():
        cluster_total = max(1, sum(counts.values()))
        scored = []
        for word, n in counts.items():
            if word in STOP:
                continue
            p_cluster = n / cluster_total
            p_corpus = corpus_counts[word] / corpus_total
            # Skip words that appear only once in the cluster (noise)
            if n < 2:
                continue
            score = p_cluster / (p_corpus + 1e-6)
            scored.append((score, word, n))
        scored.sort(reverse=True)
        labels[cid] = [w for _, w, _ in scored[:3]]

    return labels


def transform_new(
    embedding,
    unified_path: str = DEFAULT_UNIFIED_PATH,
) -> tuple[float, float, float] | None:
    """Project a single new embedding into the galaxy using the cached
    UMAP model. Returns (x, y, z) or None if no cached model is available.

    Consistent with the existing layout because it reuses the SAME fitted
    UMAP + the same mean offset + the same coord scale.
    """
    import os as _os
    if not _os.path.exists(UMAP_MODEL_PATH):
        return None
    try:
        import pickle
        import numpy as np
        with open(UMAP_MODEL_PATH, "rb") as fh:
            blob = pickle.load(fh)
        umap = blob["umap"]
        mean_offset = blob["mean_offset"]
        scale = blob["coord_scale"]
        vec = np.asarray(embedding, dtype=np.float32)
        if vec.ndim == 1:
            vec = vec.reshape(1, -1)
        raw = umap.transform(vec)[0]
        centered = (raw - mean_offset) * scale
        return float(centered[0]), float(centered[1]), float(centered[2])
    except Exception as e:
        logger.warning("transform_new failed: %s", e)
        return None


def needs_reproject(unified_path: str = DEFAULT_UNIFIED_PATH) -> int:
    """Count of active facts with embeddings but no viz coords."""
    conn = _connect(unified_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM facts f
            JOIN fact_embeddings e ON e.fact_id = f.id
            WHERE f.archived = 0 AND f.forgotten = 0 AND f.superseded_by IS NULL
              AND f.viz_x IS NULL
            """
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def backfill_mistake_viz(
    unified_path: str = DEFAULT_UNIFIED_PATH,
    embedding_engine=None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Phase 5.3 — embed + project every mistake that lacks viz coords.

    For each mistake without viz_x: generate embedding from mistake+why
    (storing under fact_id ``m_<row-id>`` in fact_embeddings), project
    via cached UMAP, write viz_x/y/z. Idempotent — skips mistakes that
    already have coords.

    Requires a pre-fitted UMAP model (produced by project_all). Without
    one, the call returns zero-projected but still runs embedding step.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = _connect(unified_path)
    stats = {"embedded": 0, "projected": 0, "skipped_already": 0, "failed": 0}
    try:
        rows = conn.execute(
            "SELECT id, mistake, why, viz_x FROM mistakes"
        ).fetchall()
        if not rows:
            return stats
        # Lazy-import the engine if not provided
        if embedding_engine is None:
            try:
                from null_memory.embeddings import EmbeddingEngine
                embedding_engine = EmbeddingEngine(conn)
            except Exception as e:
                logger.warning("backfill_mistake_viz: no embedding engine: %s", e)
                return stats
        for r in rows:
            if r["viz_x"] is not None:
                stats["skipped_already"] += 1
                continue
            prefixed = f"m_{r['id']}"
            try:
                vec = embedding_engine.get_embedding(prefixed)
                if vec is None:
                    text = f"{r['mistake']} {r['why'] or ''}".strip()
                    vec = embedding_engine.embed(text)
                    # Best-effort persist; FK-enabled schemas reject
                    # non-fact ids, in which case we still project from
                    # the in-memory vec and keep going.
                    try:
                        embedding_engine.store_embedding(prefixed, vec)
                        stats["embedded"] += 1
                    except Exception:
                        pass
                xyz = transform_new(vec, unified_path=unified_path)
                if xyz is None:
                    continue
                conn.execute(
                    "UPDATE mistakes SET viz_x=?, viz_y=?, viz_z=? WHERE id=?",
                    (xyz[0], xyz[1], xyz[2], r["id"]),
                )
                stats["projected"] += 1
            except Exception as e:
                logger.warning("backfill mistake %s failed: %s", r["id"], e)
                stats["failed"] += 1
        conn.commit()
        return stats
    finally:
        if owns_conn:
            conn.close()


def cluster_centroids(
    unified_path: str = DEFAULT_UNIFIED_PATH,
) -> list[dict]:
    """Compute mean position of each cluster. Identity sphere connects
    here, not to every individual fact."""
    conn = _connect(unified_path)
    try:
        rows = conn.execute(
            """
            SELECT cluster_id,
                   AVG(viz_x) AS x, AVG(viz_y) AS y, AVG(viz_z) AS z,
                   COUNT(*) AS size
            FROM facts
            WHERE viz_x IS NOT NULL AND cluster_id IS NOT NULL
              AND cluster_id >= 0
              AND archived = 0 AND forgotten = 0 AND superseded_by IS NULL
            GROUP BY cluster_id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
