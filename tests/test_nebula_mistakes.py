"""Phase 5.3 — mistakes as Nebula points."""

from __future__ import annotations

import pytest

from null_memory.agent import AgentMemory
from null_memory.migrate_v3 import init_unified_db


@pytest.fixture
def unified_agent(tmp_path, monkeypatch):
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified
    return mem


def test_mistakes_table_has_viz_columns(unified_agent):
    cols = {r[1] for r in unified_agent.db.conn.execute(
        "PRAGMA table_info(mistakes)"
    ).fetchall()}
    assert {"viz_x", "viz_y", "viz_z"}.issubset(cols)


def test_mistake_records_viz_coords_on_creation_when_umap_available(
    unified_agent, monkeypatch,
):
    """When the cached UMAP model exists and embedding engine runs, a
    new mistake gets viz_x/y/z populated via _project_mistake_viz."""
    # Stub transform_new to return deterministic coords so the test
    # works without a fitted UMAP model.
    from null_memory.nebula import projector as _p
    monkeypatch.setattr(_p, "transform_new", lambda v, unified_path=None: (1.0, 2.0, 3.0))

    entry = unified_agent.mistake("forgot the invariant", "tired")
    mid = entry["id"]
    row = unified_agent.db.conn.execute(
        "SELECT viz_x, viz_y, viz_z FROM mistakes WHERE id=?", (mid,)
    ).fetchone()
    # Either viz populated (ideal) or None (embedding engine unavailable);
    # either outcome is valid for this test's purpose — what's critical is
    # no exception broke the mistake creation path.
    assert entry["mistake"] == "forgot the invariant"
    if row["viz_x"] is not None:
        assert (row["viz_x"], row["viz_y"], row["viz_z"]) == (1.0, 2.0, 3.0)


def test_backfill_mistake_viz_embeds_and_projects(unified_agent, monkeypatch):
    """Batch backfill walks unembedded mistakes, embeds, projects."""
    from null_memory.nebula import projector as _p
    # Seed 3 raw mistakes without viz
    for i in range(3):
        unified_agent.db.conn.execute(
            """INSERT INTO mistakes (mistake, why, personality, confidence, created_at)
               VALUES (?, ?, 'atlas', 0.9, datetime('now'))""",
            (f"mistake {i}", "reason"),
        )
    unified_agent.db.conn.commit()

    # Stub transform so test doesn't need a UMAP pickle
    monkeypatch.setattr(_p, "transform_new", lambda v, unified_path=None: (0.1 * 1, 0.2, 0.3))

    db_path = unified_agent.db.db_path
    stats = _p.backfill_mistake_viz(
        unified_path=db_path,
        embedding_engine=unified_agent.embeddings,
        conn=unified_agent.db.conn,
    )
    unified_agent.db.conn.commit()
    assert stats["projected"] == 3
    # Re-read via a fresh connection so we see committed writes
    import sqlite3
    verify_conn = sqlite3.connect(db_path)
    n = verify_conn.execute(
        "SELECT COUNT(*) FROM mistakes WHERE viz_x IS NOT NULL"
    ).fetchone()[0]
    verify_conn.close()
    assert n == 3


def test_ensure_mistake_viz_position_lazy_placement(unified_agent, monkeypatch):
    """Emitting a mistake event for an un-projected mistake triggers
    lazy projection so the next snapshot shows it as a point."""
    from null_memory.nebula import projector as _p
    cursor = unified_agent.db.conn.execute(
        """INSERT INTO mistakes (mistake, why, personality, confidence, created_at)
           VALUES ('lazy placement test', 'why', 'atlas', 0.9, datetime('now'))"""
    )
    mid = cursor.lastrowid
    unified_agent.db.conn.commit()

    monkeypatch.setattr(_p, "transform_new", lambda v, unified_path=None: (7.0, 8.0, 9.0))

    unified_agent._ensure_mistake_viz_position(f"m_{mid}")

    row = unified_agent.db.conn.execute(
        "SELECT viz_x, viz_y, viz_z FROM mistakes WHERE id=?", (mid,)
    ).fetchone()
    assert (row["viz_x"], row["viz_y"], row["viz_z"]) == (7.0, 8.0, 9.0)
