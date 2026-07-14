"""Tests for the poke loop — replay-on-pull (issue #20 Phase B).

No network: a local bare git repo stands in for the store remote. Two
"machines" (seat A and seat B) share the bare repo; A writes facts with
the event log on and pushes; B's poke cycle fetches, fast-forward pulls,
replays the new lines into its live db, and surfaces the one-line
briefing signal. Also covers: idempotent re-poke, the divergence warning
(non-event-log files are never merged), cursor handling, force debounce,
and interval config."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from null_memory.agent import AgentMemory
from null_memory.poke import (
    DEFAULT_POKE_INTERVAL_MINUTES,
    POKE_LAST_UPDATE_KEY,
    PokeWorker,
    poke_interval_seconds,
    poke_once,
    render_sync_lines,
    replay_new_log_lines,
)

from tests.conftest import quiesce_mem


def _git(cwd, *args) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", *args], cwd=str(cwd),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res


def _init_seat_repo(seat_dir) -> None:
    """Make a seat's store dir a git repo that only tracks event logs
    (db + machine-local config stay out — they're per-machine)."""
    _git(seat_dir, "init", "-b", "main")
    _git(seat_dir, "config", "user.email", "t@t")
    _git(seat_dir, "config", "user.name", "t")
    (seat_dir / ".gitignore").write_text(
        "*.db\n*.db-*\nconfig.json\nexchange/\nsessions/\n*.json\n")


@pytest.fixture
def two_seats(tmp_path, monkeypatch):
    """Seat A (writer) and seat B (replica) sharing a bare store remote.
    A has the event log enabled and one fact already pushed."""
    monkeypatch.setenv("NULL_EVENT_LOG", "1")
    bare = tmp_path / "store.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")

    seat_a = tmp_path / "seat_a"
    mem_a = AgentMemory.load(str(seat_a))
    mem_a.learn("Kafka provides distributed event streaming",
                confidence=0.9, project="demo")
    _init_seat_repo(seat_a)
    _git(seat_a, "add", "-A")
    _git(seat_a, "commit", "-m", "seat A: initial events")
    _git(seat_a, "remote", "add", "origin", str(bare))
    _git(seat_a, "push", "-u", "origin", "main")

    seat_b = tmp_path / "seat_b"
    _git(tmp_path, "clone", str(bare), str(seat_b))
    _git(seat_b, "config", "user.email", "t@t")
    _git(seat_b, "config", "user.name", "t")
    mem_b = AgentMemory.load(str(seat_b))  # fresh db, fresh machine_id

    yield mem_a, seat_a, mem_b, seat_b
    quiesce_mem(mem_a)
    quiesce_mem(mem_b)


def _push_more_facts(mem_a, seat_a, facts: list[str]) -> None:
    for f in facts:
        mem_a.learn(f, confidence=0.9, project="demo")
    _git(seat_a, "add", "-A")
    _git(seat_a, "commit", "-m", "seat A: more events")
    _git(seat_a, "push")


# ── the cycle ───────────────────────────────────────────────────────────


class TestPokeCycle:
    def test_replays_remote_events_into_live_db(self, two_seats):
        mem_a, seat_a, mem_b, _seat_b = two_seats
        report = poke_once(mem_b)
        assert report["fetched"]
        assert report["fast_forwarded"]
        assert report["warning"] is None
        assert report["replayed"] >= 1
        assert list(report["writers"]) == [mem_a.events.writer_id]
        facts = [f["fact"] for f in mem_b.db.get_active_facts()]
        assert any("Kafka" in f for f in facts)

    def test_repoke_is_idempotent_and_quiet(self, two_seats):
        _mem_a, _seat_a, mem_b, _seat_b = two_seats
        first = poke_once(mem_b)
        assert first["replayed"] >= 1
        n_facts = len(mem_b.db.get_active_facts())
        second = poke_once(mem_b)
        assert second["replayed"] == 0  # cursor advanced — nothing new
        assert len(mem_b.db.get_active_facts()) == n_facts

    def test_incremental_lines_replay_after_new_push(self, two_seats):
        mem_a, seat_a, mem_b, _seat_b = two_seats
        poke_once(mem_b)
        _push_more_facts(mem_a, seat_a, [
            "Redis provides in-memory key-value structures",
            "Nginx handles reverse proxy load balancing",
        ])
        report = poke_once(mem_b)
        assert report["replayed"] == 2
        facts = [f["fact"] for f in mem_b.db.get_active_facts()]
        assert any("Redis" in f for f in facts)
        assert any("Nginx" in f for f in facts)

    def test_forget_tombstone_replays(self, two_seats):
        mem_a, seat_a, mem_b, _seat_b = two_seats
        poke_once(mem_b)
        target = mem_a.db.get_active_facts()[0]
        mem_a.forget(fact_id=target["id"])
        _git(seat_a, "add", "-A")
        _git(seat_a, "commit", "-m", "seat A: forget")
        _git(seat_a, "push")
        poke_once(mem_b)
        row = mem_b.db.get_fact_by_id(target["id"])
        assert row is not None and row.get("forgotten")

    def test_divergence_on_non_log_files_warns_never_merges(self, two_seats):
        mem_a, seat_a, mem_b, seat_b = two_seats
        poke_once(mem_b)
        # B commits a stray non-log file locally; A pushes more upstream —
        # histories diverge on the replica.
        (seat_b / "stray.txt").write_text("local-only note\n")
        _git(seat_b, "add", "stray.txt")
        _git(seat_b, "commit", "-m", "seat B: stray local commit")
        _push_more_facts(mem_a, seat_a, ["Terraform provisions cloud infra"])
        head_before = _git(seat_b, "rev-parse", "HEAD").stdout.strip()
        report = poke_once(mem_b)
        assert report["warning"] is not None
        assert "not merging" in report["warning"]
        # No merge happened: HEAD unmoved, no merge commit.
        assert _git(seat_b, "rev-parse", "HEAD").stdout.strip() == head_before

    def test_no_repo_no_remote_is_graceful(self, tmp_path, monkeypatch):
        mem = AgentMemory.load(str(tmp_path / "lone"))
        try:
            report = poke_once(mem)
            assert report["fetched"] is False
            assert report["replayed"] == 0
        finally:
            quiesce_mem(mem)


# ── cursors ─────────────────────────────────────────────────────────────


class TestCursors:
    def test_own_log_is_skipped(self, two_seats):
        """B's own events never replay back into B (they're committed
        truth already); only foreign writers count."""
        mem_a, _seat_a, mem_b, _seat_b = two_seats
        mem_b.learn("local seat B fact", confidence=0.9, project="demo")
        count, writers = replay_new_log_lines(mem_b)
        assert mem_b.events.writer_id not in writers
        assert set(writers) <= {mem_a.events.writer_id}

    def test_shrunken_file_resets_cursor(self, two_seats):
        mem_a, _seat_a, mem_b, _seat_b = two_seats
        poke_once(mem_b)
        # Simulate a re-baselined (rewritten, shorter) foreign log.
        log = os.path.join(os.path.dirname(mem_b.db.db_path), "events",
                           f"{mem_a.events.writer_id}.jsonl")
        line = json.dumps({
            "seq": 999, "writer": mem_a.events.writer_id,
            "ts": "2099-01-01T00:00:00Z", "kind": "fact.add", "id": "feed01",
            "scope": "org",
            "data": {"fact": "rewritten log fact", "project": "demo",
                     "confidence": 0.9},
        })
        with open(log, "w", encoding="utf-8") as f:
            f.write(line + "\n")
        count, _writers = replay_new_log_lines(mem_b)
        assert count == 1
        assert mem_b.db.get_fact_by_id("feed01") is not None


# ── briefing line ───────────────────────────────────────────────────────


class TestBriefingLine:
    def test_one_line_when_fresh(self, two_seats):
        mem_a, _seat_a, mem_b, _seat_b = two_seats
        poke_once(mem_b)
        lines = render_sync_lines(mem_b.db)
        assert len(lines) == 1
        assert "↓ store updated from" in lines[0]
        assert mem_a.events.writer_id in lines[0]
        assert "m ago" in lines[0]
        assert "events" in lines[0]

    def test_appears_in_full_briefing(self, two_seats):
        _mem_a, _seat_a, mem_b, _seat_b = two_seats
        poke_once(mem_b)
        assert "↓ store updated from" in mem_b.briefing()

    def test_silent_with_no_update(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path / "quiet"))
        try:
            assert render_sync_lines(mem.db) == []
            assert "↓ store updated" not in mem.briefing()
        finally:
            quiesce_mem(mem)

    def test_silent_when_stale(self, two_seats):
        _mem_a, _seat_a, mem_b, _seat_b = two_seats
        poke_once(mem_b)
        # Age the update past the 24h freshness cap.
        mem_b.db.set_meta(POKE_LAST_UPDATE_KEY, json.dumps({
            "ts": "2020-01-01T00:00:00Z", "events": 3, "writers": ["x"]}))
        mem_b.db.conn.commit()
        assert render_sync_lines(mem_b.db) == []


# ── worker: interval config + force debounce ────────────────────────────


class TestPokeWorker:
    def test_default_interval_is_5_minutes(self, tmp_path):
        assert DEFAULT_POKE_INTERVAL_MINUTES == 5.0
        assert poke_interval_seconds(str(tmp_path)) == 300.0

    def test_interval_from_store_config(self, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"poke_interval_minutes": 2}))
        assert poke_interval_seconds(str(tmp_path)) == 120.0

    def test_force_debounced_to_one_per_window(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path / "seat"))
        try:
            worker = PokeWorker(mem, interval_seconds=3600,
                                force_debounce_seconds=10.0)
            assert worker.force() is True
            assert worker.force() is False
            assert worker.force() is False
            assert worker.stats["forced"] == 1
            assert worker.stats["force_debounced"] == 2
        finally:
            quiesce_mem(mem)

    def test_force_accepted_after_window(self, tmp_path):
        import time
        mem = AgentMemory.load(str(tmp_path / "seat"))
        try:
            worker = PokeWorker(mem, interval_seconds=3600,
                                force_debounce_seconds=0.05)
            assert worker.force() is True
            time.sleep(0.08)
            assert worker.force() is True
            assert worker.stats["forced"] == 2
        finally:
            quiesce_mem(mem)

    def test_cycle_once_is_leader_gated(self, two_seats):
        _mem_a, _seat_a, mem_b, _seat_b = two_seats
        w1 = PokeWorker(mem_b, interval_seconds=3600)
        w2 = PokeWorker(mem_b, interval_seconds=3600)
        assert w1.cycle_once() is not None  # claims leadership
        assert w2.cycle_once() is None      # standby yields
        assert w2.stats["skipped_not_leader"] == 1
