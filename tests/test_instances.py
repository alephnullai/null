"""Instance presence registry — the multi-process honesty primitive.

Every live Null process (MCP server, CLI invocation) registers a row in
the shared `instances` table at AgentMemory.load(); the long-lived MCP
server refreshes its heartbeat via touch_instance() piggybacked on the
per-tool-call session touch. Covers:
  • registration on load (hostname/pid/transport/schema_version_seen)
  • transport resolution: explicit arg > NULL_TRANSPORT env > 'cli'
  • heartbeat advance + in-process throttle (no spurious writes)
  • liveness window (db.INSTANCE_LIVE_WINDOW_MINUTES)
  • GC of long-dead rows on registration (db.INSTANCE_GC_DAYS)
  • two instances in one process both visible to each other
  • re-registration when the row was GC'd out from under us
  • status line ("Instances: N live ...") and the briefing warning,
    which appears ONLY when >1 instance is live
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from null_memory.agent import AgentMemory
from null_memory.db import INSTANCE_GC_DAYS, INSTANCE_LIVE_WINDOW_MINUTES
from null_memory.migrate_v3 import UNIFIED_SCHEMA_VERSION


def _rows(mem):
    return mem.db.conn.execute(
        "SELECT * FROM instances ORDER BY started_at"
    ).fetchall()


def _backdate(mem, instance_id: str, *, minutes: float = 0, days: float = 0):
    """Rewind a row's last_heartbeat (test-only time travel)."""
    past = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes, days=days)
    ).isoformat()
    with mem.db.write_transaction() as conn:
        conn.execute(
            "UPDATE instances SET last_heartbeat = ? WHERE instance_id = ?",
            (past, instance_id),
        )


# ── Registration ──────────────────────────────────────────────────────────


class TestRegistration:
    def test_load_registers_instance(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        assert mem._instance_id
        rows = _rows(mem)
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["instance_id"] == mem._instance_id
        assert row["pid"] == os.getpid()
        assert row["hostname"]
        assert row["started_at"]
        assert row["last_heartbeat"]
        assert row["personality"] == "atlas"
        assert row["schema_version_seen"] == UNIFIED_SCHEMA_VERSION

    def test_transport_defaults_to_cli(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NULL_TRANSPORT", raising=False)
        mem = AgentMemory.load(str(tmp_path))
        assert dict(_rows(mem)[0])["transport"] == "cli"

    def test_transport_explicit_arg(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path), transport="mcp")
        assert dict(_rows(mem)[0])["transport"] == "mcp"

    def test_transport_env_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_TRANSPORT", "mcp")
        mem = AgentMemory.load(str(tmp_path))
        assert dict(_rows(mem)[0])["transport"] == "mcp"

    def test_two_instances_one_process_both_visible(self, tmp_path):
        m1 = AgentMemory.load(str(tmp_path))
        m2 = AgentMemory.load(str(tmp_path), transport="mcp")
        assert m1._instance_id != m2._instance_id
        live_ids = {r["instance_id"] for r in m1.db.get_live_instances()}
        assert {m1._instance_id, m2._instance_id} <= live_ids
        # ...and from the other connection too
        live_ids2 = {r["instance_id"] for r in m2.db.get_live_instances()}
        assert {m1._instance_id, m2._instance_id} <= live_ids2


# ── Heartbeat ─────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_touch_advances_heartbeat(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        _backdate(mem, mem._instance_id, minutes=3)
        before = dict(_rows(mem)[0])["last_heartbeat"]
        mem.touch_instance(force=True)
        after = dict(_rows(mem)[0])["last_heartbeat"]
        assert after > before

    def test_touch_is_throttled_in_process(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        # Registration just primed the monotonic throttle; a non-forced
        # touch inside the window must not write.
        _backdate(mem, mem._instance_id, minutes=3)
        before = dict(_rows(mem)[0])["last_heartbeat"]
        mem.touch_instance()
        assert dict(_rows(mem)[0])["last_heartbeat"] == before

    def test_touch_records_project(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.touch_instance(project="aleph", force=True)
        assert dict(_rows(mem)[0])["project"] == "aleph"
        # COALESCE keeps the last known project when none is passed
        mem.touch_instance(force=True)
        assert dict(_rows(mem)[0])["project"] == "aleph"

    def test_touch_reregisters_when_row_gone(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        with mem.db.write_transaction() as conn:
            conn.execute("DELETE FROM instances")
        mem.touch_instance(force=True)
        rows = _rows(mem)
        assert len(rows) == 1
        assert dict(rows[0])["instance_id"] == mem._instance_id


# ── Liveness window ───────────────────────────────────────────────────────


class TestLiveness:
    def test_stale_instance_not_live(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        _backdate(mem, mem._instance_id,
                  minutes=INSTANCE_LIVE_WINDOW_MINUTES + 1)
        assert mem.db.get_live_instances() == []

    def test_fresh_instance_is_live(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        _backdate(mem, mem._instance_id,
                  minutes=INSTANCE_LIVE_WINDOW_MINUTES - 1)
        live = mem.db.get_live_instances()
        assert [r["instance_id"] for r in live] == [mem._instance_id]


# ── GC ────────────────────────────────────────────────────────────────────


class TestGC:
    def test_registration_gcs_long_dead_rows(self, tmp_path):
        m1 = AgentMemory.load(str(tmp_path))
        dead_id, kept_id = m1._instance_id, None
        _backdate(m1, dead_id, days=INSTANCE_GC_DAYS + 1)
        m2 = AgentMemory.load(str(tmp_path))
        kept_id = m2._instance_id
        _backdate(m2, kept_id, days=INSTANCE_GC_DAYS - 1)
        # New registration GCs the >7d row, keeps the <7d one.
        m3 = AgentMemory.load(str(tmp_path))
        ids = {r["instance_id"] for r in _rows(m3)}
        assert dead_id not in ids
        assert kept_id in ids
        assert m3._instance_id in ids


# ── Surfaces ──────────────────────────────────────────────────────────────


class TestSurfaces:
    def test_status_includes_instances_line(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        status = mem.status()
        assert "Instances: 1 live" in status

    def test_instances_line_marks_self_and_counts(self, tmp_path):
        m1 = AgentMemory.load(str(tmp_path))
        AgentMemory.load(str(tmp_path), transport="mcp")
        line = m1.instances_line()
        assert line.startswith("Instances: 2 live")
        assert "(cli" in line and "(mcp" in line

    def test_briefing_silent_with_single_instance(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        assert mem.multi_instance_warning() is None
        assert "instances live on this store" not in mem.briefing()

    def test_briefing_warns_only_above_one_live(self, tmp_path):
        m1 = AgentMemory.load(str(tmp_path))
        m2 = AgentMemory.load(str(tmp_path), transport="mcp")
        warn = m1.multi_instance_warning()
        assert warn is not None
        assert "2 Atlas instances live on this store" in warn
        briefing = m1.briefing()
        assert sum(
            1 for ln in briefing.splitlines()
            if "instances live on this store" in ln
        ) == 1
        # Second instance goes stale → warning disappears again.
        _backdate(m1, m2._instance_id,
                  minutes=INSTANCE_LIVE_WINDOW_MINUTES + 1)
        assert m1.multi_instance_warning() is None
        assert "instances live on this store" not in m1.briefing()

    def test_warning_ignores_other_personalities(self, tmp_path):
        # On a unified store other live personalities share the instances
        # table — they are not fragments of THIS identity and must not
        # trigger the warning.
        mem = AgentMemory.load(str(tmp_path))
        mem.db.register_instance(
            "worker-row", hostname="h2", pid=999,
            personality="hermes", transport="mcp",
        )
        assert len(mem.db.get_live_instances()) == 2
        assert mem.multi_instance_warning() is None


# ── MCP piggyback ─────────────────────────────────────────────────────────


class TestMCPPiggyback:
    def test_handlers_register_as_mcp_and_touch(self, tmp_path):
        from null_memory.mcp.handlers import NullHandlers
        handlers = NullHandlers(agent_dir=str(tmp_path))
        try:
            handlers._ensure_session()  # the per-tool-call path
            mem = handlers.memory
            row = dict(_rows(mem)[0])
            assert row["transport"] == "mcp"
            assert row["instance_id"] == mem._instance_id
            # Heartbeat path is live: a forced touch after backdating
            # advances last_heartbeat through the same machinery
            # _ensure_session uses.
            _backdate(mem, mem._instance_id, minutes=3)
            before = dict(_rows(mem)[0])["last_heartbeat"]
            mem.touch_instance(force=True)
            assert dict(_rows(mem)[0])["last_heartbeat"] > before
        finally:
            if handlers._auto_close_timer is not None:
                handlers._auto_close_timer.cancel()
