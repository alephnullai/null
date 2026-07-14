from __future__ import annotations

import sqlite3

from null_memory.agent import AgentMemory
from null_memory.migrate_v3 import init_unified_db


def test_ensure_viz_position_commits_for_other_connections(tmp_path, monkeypatch):
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")

    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, confidence, created_at, archived)
           VALUES ('viz_fact', 'fact needing placement', 1.0,
                   '2026-01-01T00:00:00+00:00', 0)"""
    )
    mem.db.conn.commit()
    mem._embeddings = False
    monkeypatch.setattr(mem, "_compute_viz_coords", lambda _text, vec=None: (1.0, 2.0, 3.0))

    mem._ensure_viz_position("viz_fact")

    other = sqlite3.connect(unified)
    try:
        row = other.execute(
            "SELECT viz_x, viz_y, viz_z FROM facts WHERE id='viz_fact'"
        ).fetchone()
    finally:
        other.close()
    assert row == (1.0, 2.0, 3.0)
