"""Nebula visualization placement + live event emission.

Extracted from agent.py (P2 god-object split). Contains:
  * the module-level UMAP projector resolution helpers (`_transform_new`,
    `_is_degenerate_projection`) and the `_VIZ_AVAILABLE` import guard
  * VizMixin — 3D coordinate projection for facts and mistakes
    (`_compute_viz_coords`, `_ensure_viz_position`,
    `_project_mistake_viz`, `_ensure_mistake_viz_position`,
    `_nearest_cluster`) and nebula_events emission
    (`_emit_nebula_event`).

Mixed into AgentMemory; methods rely on the host's db / embeddings /
personality attributes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Hoist the Nebula projector import once at module load. Keeps the viz
# helpers cheap (no repeat imports from inside try-blocks), and — more
# importantly — makes a missing dependency (umap / numba / fastembed)
# visible in a startup log line instead of silently killing placement.
#
# We import the MODULE (not the function) so tests that monkeypatch
# ``null_memory.nebula.projector.transform_new`` after import still take
# effect — a bound function reference would freeze the pre-patch behavior.
try:
    from null_memory.nebula import projector as _projector
    _VIZ_AVAILABLE = True
except ImportError as _viz_import_err:
    logger.warning(
        "Nebula projector unavailable — viz placement disabled: %s",
        _viz_import_err,
    )
    _projector = None  # type: ignore[assignment]
    _VIZ_AVAILABLE = False


def _transform_new(vec):
    """Resolve ``transform_new`` dynamically off the projector module so
    monkeypatching in tests still propagates into this module's callers."""
    if _projector is None:
        return None
    return _projector.transform_new(vec)


def _is_degenerate_projection(xyz) -> bool:
    """True if the transform result is missing or sits on/near the origin.
    Origin is a known failure mode when fastembed returns a zero vector
    in the MCP daemon context."""
    return xyz is None or abs(xyz[0]) + abs(xyz[1]) + abs(xyz[2]) < 0.01


class VizMixin:
    """Nebula projection + event emission methods for AgentMemory."""

    def _compute_viz_coords(self, text: str, vec=None):
        """Project memory text to 3D (x, y, z) via the cached UMAP model.

        Primary path: embed via self.embeddings (if ``vec`` wasn't passed
        in) and project. Fallback: if the primary projection is degenerate
        (known failure mode — fastembed occasionally returns a zero vector
        in the MCP daemon context, which projects to the UMAP mean), retry
        with a fresh EmbeddingEngine. This has always produced valid
        coords in testing.

        Returns (x, y, z) on success or None. Logs a WARN on fallback so
        primary-engine health is observable."""
        if not _VIZ_AVAILABLE:
            return None
        emb = self.embeddings
        primary_vec = vec
        if primary_vec is None and emb is not None:
            try:
                primary_vec = emb.embed(text)
            except Exception as e:
                logger.warning("primary embed failed: %s", e)
        if primary_vec is not None:
            try:
                xyz = _transform_new(primary_vec)
                if not _is_degenerate_projection(xyz):
                    return xyz
            except Exception as e:
                logger.warning("primary transform failed: %s", e)

        # Fallback: fresh engine on same connection
        try:
            from null_memory.embeddings import EmbeddingEngine
            fresh = EmbeddingEngine(self.db)
            fresh_vec = fresh.embed(text)
            xyz = _transform_new(fresh_vec)
            if _is_degenerate_projection(xyz):
                return None
            logger.warning(
                "viz-projection fallback engaged (primary was degenerate)"
            )
            return xyz
        except Exception as e:
            logger.error("fresh-engine fallback failed: %s", e)
            return None

    def _project_mistake_viz(self, mistake_id: int, embedding=None) -> None:
        """Project a mistake's embedding to 3D and persist on mistakes row.
        Silent on IO/DB failure by design (best-effort viz)."""
        if not getattr(self.db, "unified", False):
            return
        row = self.db.conn.execute(
            "SELECT mistake, why FROM mistakes WHERE id=?", (mistake_id,)
        ).fetchone()
        if row is None:
            return
        text = f"{row['mistake']} {row['why'] or ''}".strip()
        xyz = self._compute_viz_coords(text, vec=embedding)
        if xyz is None:
            return
        try:
            self.db.conn.execute(
                "UPDATE mistakes SET viz_x=?, viz_y=?, viz_z=? WHERE id=?",
                (xyz[0], xyz[1], xyz[2], mistake_id),
            )
            self.db.conn.commit()
        except Exception as e:
            logger.error("mistake viz persist failed: %s", e)

    def _ensure_mistake_viz_position(self, prefixed_id: str) -> None:
        """Live-placement for mistakes. No-op if already placed.
        prefixed_id is the m_<row-id> form used in nebula_events."""
        if not getattr(self.db, "unified", False):
            return
        try:
            mid = int(prefixed_id[2:])
        except (ValueError, IndexError):
            return
        row = self.db.conn.execute(
            "SELECT mistake, why, viz_x FROM mistakes WHERE id=?", (mid,)
        ).fetchone()
        if row is None or row["viz_x"] is not None:
            return
        # Delegate to _project_mistake_viz — shared projection + fallback
        # path. vec=None triggers primary-engine embed inside _compute_viz.
        self._project_mistake_viz(mid, embedding=None)

    def _ensure_viz_position(self, fact_id: str) -> None:
        """Live-placement for facts. Writes viz_x/y/z/cluster_id back to
        the facts row. No-op if already placed or fact missing.

        Delegates projection to `_compute_viz_coords` which handles the
        primary-then-fresh-engine retry (with a WARN log on fallback)."""
        if not getattr(self.db, "unified", False):
            return
        row = self.db.conn.execute(
            "SELECT fact, viz_x FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if row is None or row[1] is not None:
            return  # Already placed or fact not present
        fact_text = row[0]

        # Try to reuse a stored embedding first — avoid re-embedding on
        # every call for facts we've seen before.
        vec = None
        emb = self.embeddings
        if emb is not None:
            try:
                vec = emb.get_embedding(fact_id)
            except Exception as e:
                logger.debug("get_embedding(%s) failed: %s", fact_id, e)

        xyz = self._compute_viz_coords(fact_text, vec=vec)
        if xyz is None:
            return

        x, y, z = xyz
        try:
            cluster_id = self._nearest_cluster(x, y, z)
            self.db.conn.execute(
                "UPDATE facts SET viz_x=?, viz_y=?, viz_z=?, cluster_id=? WHERE id=?",
                (x, y, z, cluster_id, fact_id),
            )
            self.db.conn.commit()
        except Exception as e:
            logger.error("fact viz persist failed: %s", e)

    def _nearest_cluster(self, x: float, y: float, z: float) -> int | None:
        try:
            row = self.db.conn.execute(
                """SELECT cluster_id,
                          (AVG(viz_x)-?)*(AVG(viz_x)-?) +
                          (AVG(viz_y)-?)*(AVG(viz_y)-?) +
                          (AVG(viz_z)-?)*(AVG(viz_z)-?) AS d
                   FROM facts
                   WHERE cluster_id IS NOT NULL AND cluster_id >= 0
                         AND viz_x IS NOT NULL
                   GROUP BY cluster_id
                   ORDER BY d ASC LIMIT 1""",
                (x, x, y, y, z, z),
            ).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    def _emit_nebula_event(
        self,
        kind: str,
        fact_id: str | None = None,
        related_ids: list[str] | None = None,
        intensity: float = 1.0,
    ) -> None:
        """Insert a nebula_events row for live-firing animations.

        Best-effort: must not break the calling action if the unified DB
        is unavailable or the table is missing. Phase 3b Session 2.
        """
        if not getattr(self.db, "unified", False):
            return
        try:
            # If this event references a real fact that doesn't yet have
            # Nebula coordinates, project it now so the ring can land.
            # Cached UMAP transform is ~5ms; best-effort — non-blocking.
            if fact_id:
                if fact_id.startswith("m_"):
                    self._ensure_mistake_viz_position(fact_id)
                else:
                    self._ensure_viz_position(fact_id)

            now = datetime.now(timezone.utc).isoformat()
            self.db.conn.execute(
                """INSERT INTO nebula_events
                   (kind, fact_id, personality, related_ids, intensity, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    kind,
                    fact_id,
                    self.personality,
                    json.dumps(related_ids or []),
                    intensity,
                    now,
                ),
            )
            # Purge events older than 30s so the table stays tiny
            self.db.conn.execute(
                "DELETE FROM nebula_events WHERE created_at < datetime('now', '-30 seconds')"
            )
            # Commit is required — SQLite won't publish pending writes to
            # other readers (the Nebula websocket poller) without it.
            self.db.conn.commit()
        except Exception:
            pass  # Best-effort — never break the hot path
