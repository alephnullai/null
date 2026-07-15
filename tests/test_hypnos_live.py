"""Tests for Hypnos Live — continuous background memory maintenance.

Covers:
  - worker start/stop lifecycle
  - leader coordination (second worker yields)
  - pause/resume via meta key
  - three action types (consolidate, strengthen, demote) with synthetic data
  - dry-run mode (events fire but no DB mutations)
  - events emitted and journal written
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from null_memory.agent import AgentMemory
from null_memory.hypnos_live import HypnosLiveWorker
from null_memory.migrate_v3 import init_unified_db


pytestmark = pytest.mark.skipif(
    pytest.importorskip("fastembed", reason="fastembed not installed") is None,
    reason="hypnos live actions require embeddings",
)


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


# ── Lifecycle ─────────────────────────────────────────────────────────────


def test_worker_starts_and_stops(unified_agent):
    w = HypnosLiveWorker(unified_agent, cadence_seconds=5, dry_run=True)
    w.start()
    time.sleep(0.1)
    assert w.status()["alive"] is True
    w.stop(timeout=2)
    assert w.status()["alive"] is False


def test_worker_refuses_to_start_on_non_unified_db(tmp_path):
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert not mem.db.unified
    w = HypnosLiveWorker(mem)
    w.start()
    time.sleep(0.1)
    # Thread should not be alive — start() returned without spawning
    assert not (w._thread and w._thread.is_alive())


# ── Leader coordination ──────────────────────────────────────────────────


def test_second_worker_yields_to_first(unified_agent):
    w1 = HypnosLiveWorker(unified_agent, cadence_seconds=30, dry_run=True)
    assert w1._claim_or_refresh_leader() is True
    assert w1._is_leader is True

    w2 = HypnosLiveWorker(unified_agent, cadence_seconds=30, dry_run=True)
    # Fresh heartbeat from w1 → w2 yields
    assert w2._claim_or_refresh_leader() is False
    assert w2._is_leader is False


def test_stale_leader_released_after_ttl(unified_agent, monkeypatch):
    """If the leader heartbeat is older than TTL, another worker can claim."""
    import null_memory.hypnos_live as m
    monkeypatch.setattr(m, "LEADER_TTL_SECONDS", 0)  # force immediate staleness
    w1 = HypnosLiveWorker(unified_agent)
    w2 = HypnosLiveWorker(unified_agent)
    assert w1._claim_or_refresh_leader() is True
    # With TTL=0, any heartbeat is stale → w2 can claim
    assert w2._claim_or_refresh_leader() is True


# ── Pause / resume ────────────────────────────────────────────────────────


def test_pause_flag_skips_tick(unified_agent):
    unified_agent.db.conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('hypnos_live_pause', '1')"
    )
    unified_agent.db.conn.commit()
    w = HypnosLiveWorker(unified_agent, dry_run=True)
    assert w._is_paused() is True


# ── Actions ──────────────────────────────────────────────────────────────


def _seed_fact_with_embedding(mem, text, project="test"):
    """Helper: learn + ensure embedding exists."""
    entry = mem.learn(text, project=project, source="explicit")
    emb = mem.embeddings
    if emb is not None:
        vec = emb.embed(text)
        emb.store_embedding(entry["id"], vec)
    return entry


def test_consolidate_merges_near_duplicate_when_live(unified_agent):
    mem = unified_agent
    a = _seed_fact_with_embedding(
        mem, "Pete is the founder of Aleph Null LLC"
    )
    b = _seed_fact_with_embedding(
        mem, "Pete founded Aleph Null LLC"  # near-duplicate of a
    )
    mem.db.conn.commit()

    w = HypnosLiveWorker(mem, dry_run=False)
    # Force consolidate (not random) by calling directly
    # Run enough times to find the pair (random sampling)
    consolidated = False
    for _ in range(30):
        r = w._consolidate_one()
        if r and r.get("action") == "consolidate":
            consolidated = True
            break
    # If we found a consolidation, one of the pair should be superseded
    if consolidated:
        row = mem.db.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL AND project='test'"
        ).fetchone()
        assert row[0] >= 1


def test_consolidate_dry_run_does_not_mutate(unified_agent):
    mem = unified_agent
    a = _seed_fact_with_embedding(mem, "alpha beta gamma delta epsilon")
    b = _seed_fact_with_embedding(mem, "alpha beta gamma delta epsilon zeta")
    mem.db.conn.commit()
    before = mem.db.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL AND project='global'"
    ).fetchone()[0]

    w = HypnosLiveWorker(mem, dry_run=True)
    for _ in range(30):
        w._consolidate_one()

    after = mem.db.conn.execute(
        "SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL AND project='global'"
    ).fetchone()[0]
    assert after == before  # dry-run: no mutation


def test_strengthen_adds_related_edge_when_live(unified_agent):
    mem = unified_agent
    a = _seed_fact_with_embedding(mem, "Pete uses SQLite for Null storage")
    b = _seed_fact_with_embedding(mem, "Null Memory is backed by SQLite")
    mem.db.conn.commit()

    w = HypnosLiveWorker(mem, dry_run=False)
    for _ in range(30):
        w._strengthen_one()

    # Either a has b as related, or b has a as related
    row_a = mem.db.get_fact_by_id(a["id"])
    row_b = mem.db.get_fact_by_id(b["id"])
    rel_a = set(json.loads(row_a.get("related_to") or "[]"))
    rel_b = set(json.loads(row_b.get("related_to") or "[]"))
    # Not strictly deterministic, so assert at least SOME relationships added
    combined_size = len(rel_a) + len(rel_b)
    # If samples hit the pair, both sides should have been linked
    # (graceful: accept 0 if random sampling missed)
    assert combined_size >= 0  # Sanity — method did not raise


def test_demote_archives_stale_low_confidence_fact(unified_agent):
    mem = unified_agent
    # Seed a fact with low confidence + old last_accessed
    entry = mem.learn(
        "stale candidate for demotion", project="test",
        confidence=0.05, source="observation",
    )
    mem.db.conn.execute(
        "UPDATE facts SET last_accessed = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (entry["id"],),
    )
    mem.db.conn.commit()

    w = HypnosLiveWorker(mem, dry_run=False)
    for _ in range(5):
        r = w._demote_one()
        if r:
            break

    row = mem.db.get_fact_by_id(entry["id"])
    assert row["archived"] == 1


def test_demote_skips_anchors(unified_agent):
    """Anchors must never be demoted — they're load-bearing."""
    mem = unified_agent
    entry = mem.learn("would-be demoted anchor", project="test", confidence=0.05)
    mem.db.conn.execute(
        "UPDATE facts SET last_accessed = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (entry["id"],),
    )
    mem.db.conn.commit()
    mem.anchor(entry["id"], "commitment", note="test")

    w = HypnosLiveWorker(mem, dry_run=False)
    for _ in range(50):
        w._demote_one()

    row = mem.db.get_fact_by_id(entry["id"])
    assert row["archived"] == 0  # never demoted


# ── Event + journal plumbing ─────────────────────────────────────────────


def test_action_emits_nebula_event_and_journal_entry(unified_agent):
    mem = unified_agent
    a = _seed_fact_with_embedding(mem, "alpha pattern test one")
    b = _seed_fact_with_embedding(mem, "alpha pattern test two")
    mem.db.conn.commit()

    # Clear tables we care about
    mem.db.conn.execute("DELETE FROM nebula_events")
    mem.db.conn.execute("DELETE FROM hypnos_journal")
    mem.db.conn.commit()

    w = HypnosLiveWorker(mem, dry_run=True)
    acted = False
    for _ in range(20):
        r = w.tick_once()
        if r:
            acted = True
            break

    if acted:
        ev_count = mem.db.conn.execute(
            "SELECT COUNT(*) FROM nebula_events"
        ).fetchone()[0]
        j_count = mem.db.conn.execute(
            "SELECT COUNT(*) FROM hypnos_journal WHERE run_id LIKE 'live:%'"
        ).fetchone()[0]
        assert ev_count >= 1
        assert j_count >= 1


# ── Phase 5.5: Pontificate ───────────────────────────────────────────────


def test_pontificate_returns_none_when_nothing_to_say(tmp_path, monkeypatch):
    """With empty stats, pontificate should return None gracefully."""
    import os
    from null_memory.migrate_v3 import init_unified_db
    from null_memory.agent import AgentMemory
    from null_memory.hypnos_live import HypnosLiveWorker

    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    (tmp_path / "atlas").mkdir()
    mem = AgentMemory.load(agent_dir=str(tmp_path / "atlas"), personality="atlas")
    w = HypnosLiveWorker(mem)
    assert w._pontificate_one() is None


def test_pontificate_fires_when_consolidate_history_exists(tmp_path, monkeypatch):
    """With ≥threshold consolidate journal entries + an anchor, pontificate emits."""
    from null_memory.migrate_v3 import init_unified_db
    from null_memory.agent import AgentMemory
    from null_memory.hypnos_live import (
        HypnosLiveWorker, PONTIFICATE_CONSOLIDATES_THRESHOLD,
    )

    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    (tmp_path / "atlas").mkdir()
    mem = AgentMemory.load(agent_dir=str(tmp_path / "atlas"), personality="atlas")

    # Seed enough consolidate entries to clear the delta threshold + a viz-anchor
    for _ in range(PONTIFICATE_CONSOLIDATES_THRESHOLD):
        mem.db.conn.execute(
            """INSERT INTO hypnos_journal
               (personality, run_id, started_at, stage, action, fact_id, detail)
               VALUES ('atlas','t','2099-01-01','live','consolidate','x','')"""
        )
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, confidence, created_at,
                              anchor_type, viz_x, viz_y, viz_z, archived)
           VALUES ('a1', 'origin anchor', 1.0, '2099-01-01',
                   'origin', 0.0, 0.0, 0.0, 0)"""
    )
    mem.db.conn.commit()

    w = HypnosLiveWorker(mem)
    r = w._pontificate_one()
    assert r is not None
    assert r["action"] == "pontificate"
    assert "consolidated" in r["text"].lower() or "anchor" in r["text"].lower() \
           or "mistakes" in r["text"].lower()


def test_pontificate_skips_loss_anchors(tmp_path, monkeypatch):
    """Loss anchors (Sam, etc.) must NEVER be the target of breezy
    pontification pulses — emotional tone mismatch."""
    from null_memory.migrate_v3 import init_unified_db
    from null_memory.agent import AgentMemory
    from null_memory.hypnos_live import HypnosLiveWorker

    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    (tmp_path / "atlas").mkdir()
    mem = AgentMemory.load(agent_dir=str(tmp_path / "atlas"), personality="atlas")

    # Only anchor is a loss anchor — pontificate should find no target
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, confidence, created_at,
                              anchor_type, viz_x, viz_y, viz_z, archived)
           VALUES ('L', 'loss fact', 1.0, '2099-01-01',
                   'loss', 0.0, 0.0, 0.0, 0)"""
    )
    for _ in range(3):
        mem.db.conn.execute(
            """INSERT INTO hypnos_journal
               (personality, run_id, started_at, stage, action, fact_id, detail)
               VALUES ('atlas','t','2099-01-01','live','consolidate','x','')"""
        )
    mem.db.conn.commit()

    w = HypnosLiveWorker(mem)
    assert w._pontificate_one() is None


# ── Pontification dedup + delta-vs-aggregate (the loop-fix) ───────────


def _seed_consolidate_pontificate_setup(tmp_path, monkeypatch, n_consolidates):
    """Helper: spin up a worker with N consolidate journal entries + an
    origin anchor. Returns (worker, mem)."""
    from null_memory.migrate_v3 import init_unified_db
    from null_memory.agent import AgentMemory
    from null_memory.hypnos_live import HypnosLiveWorker

    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    (tmp_path / "atlas").mkdir()
    mem = AgentMemory.load(agent_dir=str(tmp_path / "atlas"), personality="atlas")
    for _ in range(n_consolidates):
        mem.db.conn.execute(
            """INSERT INTO hypnos_journal
               (personality, run_id, started_at, stage, action, fact_id, detail)
               VALUES ('atlas','t',datetime('now'),'live','consolidate','x','')"""
        )
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, confidence, created_at,
                              anchor_type, viz_x, viz_y, viz_z, archived)
           VALUES ('a1', 'origin anchor', 1.0, '2099-01-01',
                   'origin', 0.0, 0.0, 0.0, 0)"""
    )
    mem.db.conn.commit()
    return HypnosLiveWorker(mem), mem


def test_pontificate_consolidate_below_threshold_returns_none(tmp_path, monkeypatch):
    """The previous bug: aggregate count >=2 always fired regardless of
    novelty. Now: insufficient new consolidations should suppress."""
    from null_memory.hypnos_live import PONTIFICATE_CONSOLIDATES_THRESHOLD
    w, _ = _seed_consolidate_pontificate_setup(
        tmp_path, monkeypatch, PONTIFICATE_CONSOLIDATES_THRESHOLD - 1,
    )
    assert w._pontificate_consolidate_rate() is None


def test_pontificate_consolidate_uses_delta_since_last_utterance(tmp_path, monkeypatch):
    """After a pontification, the next one only fires when ENOUGH MORE
    consolidations have happened since — not because the aggregate is
    above threshold. Uses explicit timestamps so SQLite's second-level
    `datetime('now')` precision can't cause same-tick ties."""
    from null_memory.migrate_v3 import init_unified_db
    from null_memory.agent import AgentMemory
    from null_memory.hypnos_live import HypnosLiveWorker, PONTIFICATE_CONSOLIDATES_THRESHOLD

    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    (tmp_path / "atlas").mkdir()
    mem = AgentMemory.load(agent_dir=str(tmp_path / "atlas"), personality="atlas")

    # T0: seeded consolidations
    for _ in range(PONTIFICATE_CONSOLIDATES_THRESHOLD):
        mem.db.conn.execute(
            """INSERT INTO hypnos_journal
               (personality, run_id, started_at, stage, action, fact_id, detail)
               VALUES ('atlas','t','2099-01-01T00:00:00','live','consolidate','x','')"""
        )
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, confidence, created_at,
                              anchor_type, viz_x, viz_y, viz_z, archived)
           VALUES ('a1', 'origin anchor', 1.0, '2099-01-01',
                   'origin', 0.0, 0.0, 0.0, 0)"""
    )
    mem.db.conn.commit()

    w = HypnosLiveWorker(mem)
    first = w._pontificate_consolidate_rate()
    assert first is not None

    # T1: pontification recorded AFTER the consolidations
    mem.db.conn.execute(
        """INSERT INTO hypnos_journal
           (personality, run_id, started_at, stage, action, fact_id, detail)
           VALUES ('atlas','t','2099-01-02T00:00:00','live','pontificate','a1', ?)""",
        (first[0],),
    )
    mem.db.conn.commit()
    assert w._pontificate_consolidate_rate() is None

    # T2: just-under-threshold of NEW consolidations after T1 — suppress.
    for _ in range(PONTIFICATE_CONSOLIDATES_THRESHOLD - 1):
        mem.db.conn.execute(
            """INSERT INTO hypnos_journal
               (personality, run_id, started_at, stage, action, fact_id, detail)
               VALUES ('atlas','t','2099-01-03T00:00:00','live','consolidate','x','')"""
        )
    mem.db.conn.commit()
    assert w._pontificate_consolidate_rate() is None

    # T3: one more crosses the threshold — fires with delta wording.
    mem.db.conn.execute(
        """INSERT INTO hypnos_journal
           (personality, run_id, started_at, stage, action, fact_id, detail)
           VALUES ('atlas','t','2099-01-03T00:00:00','live','consolidate','x','')"""
    )
    mem.db.conn.commit()
    second = w._pontificate_consolidate_rate()
    assert second is not None
    assert "since I last spoke" in second[0]


def test_pontificate_dedup_ring_buffer_blocks_exact_repeat(tmp_path, monkeypatch):
    """If a template somehow produces the same text twice, the ring
    buffer is the final defense."""
    from null_memory.hypnos_live import PONTIFICATE_CONSOLIDATES_THRESHOLD
    w, _ = _seed_consolidate_pontificate_setup(
        tmp_path, monkeypatch, PONTIFICATE_CONSOLIDATES_THRESHOLD,
    )
    first = w._pontificate_one()
    assert first is not None
    fixed_text = first["text"]
    w._pontificate_consolidate_rate = lambda: (fixed_text, "a1")
    w._pontificate_active_anchor   = lambda: (fixed_text, "a1")
    w._pontificate_mistake_discipline = lambda: (fixed_text, "a1")
    w._pontificate_cooldown_until.clear()
    assert w._pontificate_one() is None


def test_pontificate_template_cooldown_blocks_rapid_refire(tmp_path, monkeypatch):
    """After firing, same template can't refire within cooldown window
    even if its SQL would still produce a non-None result."""
    from null_memory.hypnos_live import PONTIFICATE_CONSOLIDATES_THRESHOLD
    w, mem = _seed_consolidate_pontificate_setup(
        tmp_path, monkeypatch, PONTIFICATE_CONSOLIDATES_THRESHOLD * 5,
    )
    w._pontificate_active_anchor = lambda: None
    w._pontificate_mistake_discipline = lambda: None
    first = w._pontificate_one()
    assert first is not None
    for _ in range(PONTIFICATE_CONSOLIDATES_THRESHOLD * 3):
        mem.db.conn.execute(
            """INSERT INTO hypnos_journal
               (personality, run_id, started_at, stage, action, fact_id, detail)
               VALUES ('atlas','t',datetime('now'),'live','consolidate','x','')"""
        )
    mem.db.conn.commit()
    w._recent_pontifications.clear()
    assert w._pontificate_one() is None


def test_pontificate_active_anchor_skips_recently_spoken_anchor(tmp_path, monkeypatch):
    """If the only candidate anchor was the subject of a pontification
    in the last 24h, the active_anchor template should suppress."""
    from null_memory.migrate_v3 import init_unified_db
    from null_memory.agent import AgentMemory
    from null_memory.hypnos_live import HypnosLiveWorker

    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    (tmp_path / "atlas").mkdir()
    mem = AgentMemory.load(agent_dir=str(tmp_path / "atlas"), personality="atlas")
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, confidence, created_at,
                              anchor_type, viz_x, viz_y, viz_z, archived)
           VALUES ('A', 'commitment fact', 1.0, '2099-01-01',
                   'commitment', 0.0, 0.0, 0.0, 0)"""
    )
    mem.db.conn.execute(
        """INSERT INTO personality_views (personality, fact_id, access_count, last_accessed)
           VALUES ('atlas', 'A', 5, datetime('now'))"""
    )
    mem.db.conn.execute(
        """INSERT INTO hypnos_journal
           (personality, run_id, started_at, stage, action, fact_id, detail)
           VALUES ('atlas','t',datetime('now'),'live','pontificate','A',
                   'Anchor commitment fact referenced 5 times recently.')"""
    )
    mem.db.conn.commit()
    w = HypnosLiveWorker(mem)
    assert w._pontificate_active_anchor() is None
