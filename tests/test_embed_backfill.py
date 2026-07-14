"""Tests for the idle embed backfill (semantic recall starved of vectors).

A store can hold hundreds of facts with almost no embeddings (1/490 seen
live) — semantic recall silently degrades and coherence has no vectors.
run_embed_backfill closes the gap; start_background_backfill schedules it
off the request path.
"""

from __future__ import annotations

import os

import pytest

from null_memory.agent import AgentMemory
from null_memory.embeddings import (
    run_embed_backfill,
    start_background_backfill,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    mem = AgentMemory(agent_dir=str(tmp_path))
    for i in range(5):
        mem.learn(f"backfill test fact number {i}", confidence=0.9)
    return mem


def test_backfill_embeds_unembedded_facts(store):
    emb = store.embeddings
    if emb is None or not emb.available:
        pytest.skip("fastembed not installed")
    # Writes may have embedded some already — wipe to simulate a relocated
    # store with vectors missing.
    emb.conn.execute("DELETE FROM fact_embeddings")
    emb.conn.commit()
    assert emb.count() == 0

    count = run_embed_backfill(store, pause=0)
    assert count == len(store.db.get_active_facts())
    assert emb.count() == count


def test_backfill_skips_when_coverage_high(store):
    emb = store.embeddings
    if emb is None or not emb.available:
        pytest.skip("fastembed not installed")
    # Ensure full coverage first.
    run_embed_backfill(store, pause=0)
    assert run_embed_backfill(store, pause=0) == 0  # no-op second pass


def test_backfill_noop_without_engine(store, monkeypatch):
    monkeypatch.setattr(type(store), "embeddings", property(lambda self: None))
    assert run_embed_backfill(store, pause=0) == 0


def test_backfill_never_raises(store, monkeypatch):
    def boom(self):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(type(store), "embeddings", property(boom))
    assert run_embed_backfill(store, pause=0) == 0  # swallowed, logged


def test_env_kill_switch(store, monkeypatch):
    monkeypatch.setenv("NULL_EMBED_BACKFILL", "0")
    assert start_background_backfill(store) is None


def test_background_thread_runs(store):
    emb = store.embeddings
    if emb is None or not emb.available:
        pytest.skip("fastembed not installed")
    emb.conn.execute("DELETE FROM fact_embeddings")
    emb.conn.commit()

    t = start_background_backfill(store, delay=0.05)
    assert t is not None
    t.join(timeout=60)
    assert not t.is_alive()
    # Worker thread gets its own per-thread SQLite connection; verify from
    # this thread that the rows landed.
    assert emb.count() == len(store.db.get_active_facts())


def test_backfill_skips_when_not_leader(store):
    """N instances share one store — only the hypnos_live leader backfills."""
    emb = store.embeddings
    if emb is None or not emb.available:
        pytest.skip("fastembed not installed")
    emb.conn.execute("DELETE FROM fact_embeddings")
    emb.conn.commit()

    from null_memory.embeddings import BACKFILL_LEADER_KEY
    from null_memory.memory.leader import LeaderLock
    other = LeaderLock(store.db.db_path, BACKFILL_LEADER_KEY, "other-instance")
    try:
        assert other.claim_or_refresh(90.0)  # a fresh leader holds the key
        t = start_background_backfill(store, delay=0.05)
        assert t is not None
        t.join(timeout=30)
        assert not t.is_alive()
        assert emb.count() == 0  # hot standby skipped the backfill
    finally:
        other.close()


def test_backfill_runs_with_in_process_leader_id(store):
    """The MCP server passes its HypnosLiveWorker's instance_id — the claim
    is a heartbeat refresh, so the backfill proceeds on the leader."""
    emb = store.embeddings
    if emb is None or not emb.available:
        pytest.skip("fastembed not installed")
    emb.conn.execute("DELETE FROM fact_embeddings")
    emb.conn.commit()

    from null_memory.embeddings import BACKFILL_LEADER_KEY
    from null_memory.memory.leader import LeaderLock
    worker_lock = LeaderLock(store.db.db_path, BACKFILL_LEADER_KEY, "shared-id")
    try:
        assert worker_lock.claim_or_refresh(90.0)
        t = start_background_backfill(store, delay=0.05, leader_instance_id="shared-id")
        assert t is not None
        t.join(timeout=60)
        assert emb.count() == len(store.db.get_active_facts())
    finally:
        worker_lock.close()
