"""Tests for Phase 7.1 — null daemon (DaemonRunner)."""

from __future__ import annotations

from types import SimpleNamespace
import sqlite3

import pytest

from null_memory.agent import AgentMemory
from null_memory.daemon import (
    DaemonRunner,
    LEADER_KEY,
    PAUSE_KEY,
    LAST_TICK_KEY,
)
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


# ── Construction + cadence ──────────────────────────────────────────────


def test_default_cadence_is_15_min(unified_agent):
    runner = DaemonRunner(unified_agent)
    assert runner.cadence == 900.0


def test_explicit_cadence_override(unified_agent):
    runner = DaemonRunner(unified_agent, cadence_seconds=120)
    assert runner.cadence == 120


def test_cadence_clamped_at_minimum(unified_agent):
    runner = DaemonRunner(unified_agent, cadence_seconds=5)
    assert runner.cadence == 30.0  # MIN_CADENCE_SECONDS


def test_env_cadence_honored(unified_agent, monkeypatch):
    monkeypatch.setenv("NULL_DAEMON_CADENCE", "300")
    runner = DaemonRunner(unified_agent)
    assert runner.cadence == 300


# ── Tick — leader, pause, sub-component invocation ─────────────────────


def test_tick_claims_leader_and_writes_last_tick(unified_agent):
    runner = DaemonRunner(unified_agent)
    report = runner.tick_once()
    assert runner._is_leader is True
    assert not report.skipped_paused
    assert not report.skipped_not_leader
    # last_tick meta key was written
    row = unified_agent.db.conn.execute(
        f"SELECT value FROM meta WHERE key='{LAST_TICK_KEY}'"
    ).fetchone()
    assert row is not None and row[0]


def test_pause_flag_short_circuits_tick(unified_agent):
    unified_agent.db.conn.execute(
        f"INSERT OR REPLACE INTO meta(key,value) VALUES ('{PAUSE_KEY}','1')"
    )
    unified_agent.db.conn.commit()
    runner = DaemonRunner(unified_agent)
    report = runner.tick_once()
    assert report.skipped_paused is True
    # Outreach NOT invoked when paused
    assert report.outreach_fired == 0
    assert report.managers_ticked == 0


def test_second_runner_yields_to_first(unified_agent):
    """Two daemons against the same DB: first claims leader, second
    sees a fresh leader value and skips its tick."""
    r1 = DaemonRunner(unified_agent)
    assert r1._claim_or_refresh_leader() is True

    r2 = DaemonRunner(unified_agent)
    report2 = r2.tick_once()
    assert report2.skipped_not_leader is True


def test_tick_invokes_outreach_evaluator(unified_agent, monkeypatch):
    """The outreach evaluator's evaluate() should be called once per tick."""
    calls = {"n": 0}

    class _StubResult:
        fired = 0
        errors = 0

    class _StubEvaluator:
        def __init__(self, mem):
            calls["mem"] = mem
        def evaluate(self):
            calls["n"] += 1
            return _StubResult()

    monkeypatch.setattr(
        "null_memory.outreach.OutreachEvaluator", _StubEvaluator,
    )
    runner = DaemonRunner(unified_agent)
    runner.tick_once()
    assert calls["n"] == 1


def test_tick_iterates_personalities_and_calls_tick(unified_agent, monkeypatch, tmp_path):
    """For each discovered personality, load_manager() + manager.tick()."""
    ticks = []

    class _StubManager:
        def __init__(self, *_a, **_kw):
            pass
        def tick(self, items=None):
            ticks.append("called")
            from null_memory.managers.base import TickResult
            return TickResult(manager="stub")
        def digest(self, since=None):
            return ""

    # Stub list_personalities to return one fake entry
    from pathlib import Path
    fake_entry = SimpleNamespace(
        name="stub", dir=Path("/tmp"), manager_path=Path("/tmp/m.py"),
        identity={}, color=None,
    )
    monkeypatch.setattr(
        "null_memory.personality.list_personalities",
        lambda: [fake_entry],
    )
    monkeypatch.setattr(
        "null_memory.personality.load_manager",
        lambda name, mem, reasoner=None: _StubManager(),
    )

    runner = DaemonRunner(unified_agent)
    report = runner.tick_once()
    assert ticks == ["called"]
    assert report.managers_ticked == 1


def test_one_manager_failure_does_not_block_others(unified_agent, monkeypatch):
    """A throwing manager logs an error but the loop continues + survives."""
    from pathlib import Path

    class _OkManager:
        def tick(self, items=None):
            from null_memory.managers.base import TickResult
            return TickResult(manager="ok")
        def digest(self, since=None):
            return ""

    class _ExplodingManager:
        def tick(self, items=None):
            raise RuntimeError("boom")
        def digest(self, since=None):
            return ""

    entries = [
        SimpleNamespace(name="boom", dir=Path("/tmp"),
                        manager_path=Path("/tmp/m.py"), identity={}, color=None),
        SimpleNamespace(name="ok", dir=Path("/tmp"),
                        manager_path=Path("/tmp/m.py"), identity={}, color=None),
    ]
    monkeypatch.setattr(
        "null_memory.personality.list_personalities",
        lambda: entries,
    )
    def _loader(name, mem, reasoner=None):
        return _ExplodingManager() if name == "boom" else _OkManager()
    monkeypatch.setattr(
        "null_memory.personality.load_manager",
        _loader,
    )

    runner = DaemonRunner(unified_agent)
    report = runner.tick_once()
    # one succeeded, one logged error
    assert report.managers_ticked == 1
    assert any("boom" in e for e in report.manager_errors)


def test_status_shape(unified_agent):
    runner = DaemonRunner(unified_agent)
    s = runner.status()
    assert "instance_id" in s
    assert "is_leader" in s
    assert "cadence_seconds" in s
    assert s["cadence_seconds"] == 900.0
    assert "stats" in s


# ── Regression: `python -m null_memory.cli daemon ...` invocation ─────


def test_python_dash_m_daemon_status_works():
    """Regression for the launchd plist invocation path. The bug:
    a `_handle_daemon` def that lives BELOW the
    `if __name__ == "__main__": main()` block isn't in the namespace
    yet when main() is called via `python -m`. Smoke-test that the
    bottom def is reachable at __main__-launch time."""
    import subprocess
    import sys
    res = subprocess.run(
        [sys.executable, "-m", "null_memory.cli", "daemon", "status"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, (
        f"daemon status crashed: stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "[daemon] status" in res.stdout
