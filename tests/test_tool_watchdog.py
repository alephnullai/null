"""Tests for the per-tool-call watchdog (responsiveness contract, class A).

Every tool on the MCP surface runs as an async wrapper that offloads the
sync handler to a worker thread under two budgets:

  soft (NULL_TOOL_BUDGET, 15s default)  — call completes; breadcrumb
      recorded (meta `tool_budget_violations` + stderr line),
  hard (NULL_TOOL_HARD_BUDGET, 60s default) — an error string is RETURNED
      to the client and the runaway worker thread is abandoned (never
      killed), with a breadcrumb making the abandonment diagnosable.

`null doctor` surfaces the breadcrumbs.
"""

import asyncio
import json
import time

import pytest

from null_memory.mcp.server import (
    BUDGET_VIOLATIONS_META_KEY,
    DEFAULT_TOOL_BUDGET,
    DEFAULT_TOOL_HARD_BUDGET,
    TOOL_BUDGET_ENV,
    TOOL_HARD_BUDGET_ENV,
    _record_budget_violation,
    create_server,
    tool_budgets,
)


def _violations(handlers) -> list[dict]:
    raw = handlers.memory.db.get_meta(BUDGET_VIOLATIONS_META_KEY)
    return json.loads(raw) if raw else []


class TestBudgetEnv:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv(TOOL_BUDGET_ENV, raising=False)
        monkeypatch.delenv(TOOL_HARD_BUDGET_ENV, raising=False)
        assert tool_budgets() == (DEFAULT_TOOL_BUDGET,
                                  DEFAULT_TOOL_HARD_BUDGET)

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv(TOOL_BUDGET_ENV, "5")
        monkeypatch.setenv(TOOL_HARD_BUDGET_ENV, "30")
        assert tool_budgets() == (5.0, 30.0)

    def test_garbage_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv(TOOL_BUDGET_ENV, "banana")
        monkeypatch.setenv(TOOL_HARD_BUDGET_ENV, "-1")
        assert tool_budgets() == (DEFAULT_TOOL_BUDGET,
                                  DEFAULT_TOOL_HARD_BUDGET)

    def test_hard_clamped_to_at_least_soft(self, monkeypatch):
        # hard below soft would error calls never even flagged slow.
        monkeypatch.setenv(TOOL_BUDGET_ENV, "20")
        monkeypatch.setenv(TOOL_HARD_BUDGET_ENV, "5")
        soft, hard = tool_budgets()
        assert (soft, hard) == (20.0, 20.0)


class TestSoftBudget:
    def test_under_budget_leaves_no_breadcrumb(self, tmp_path, monkeypatch):
        monkeypatch.delenv(TOOL_BUDGET_ENV, raising=False)
        monkeypatch.delenv(TOOL_HARD_BUDGET_ENV, raising=False)
        server, handlers = create_server(str(tmp_path))
        out = asyncio.run(server._tool_manager._tools["null_status"].fn())
        assert isinstance(out, str) and out
        assert _violations(handlers) == []

    def test_soft_violation_records_breadcrumb_and_still_answers(
            self, tmp_path, monkeypatch, capsys):
        # A soft budget every real call exceeds: the call must still
        # complete normally, with a breadcrumb + stderr line behind it.
        monkeypatch.setenv(TOOL_BUDGET_ENV, "0.000001")
        monkeypatch.setenv(TOOL_HARD_BUDGET_ENV, "60")
        server, handlers = create_server(str(tmp_path))
        out = asyncio.run(server._tool_manager._tools["null_status"].fn())
        assert isinstance(out, str) and out
        assert "exceeded the hard budget" not in out

        entries = _violations(handlers)
        assert entries, "soft violation must leave a meta breadcrumb"
        last = entries[-1]
        assert last["kind"] == "soft"
        assert last["tool"] == "null_status"
        assert last["elapsed_s"] > 0
        assert "at" in last and "budget_s" in last

        err = capsys.readouterr().err
        assert "tool budget SOFT" in err
        assert "null_status" in err


class TestHardBudget:
    def test_hard_budget_returns_error_and_abandons(
            self, tmp_path, monkeypatch):
        """The artificially slow tool: the watchdog must answer the client
        at the hard budget instead of hanging it for the full sleep."""
        monkeypatch.setenv("NULL_DEBUG_TOOLS", "1")
        monkeypatch.setenv(TOOL_BUDGET_ENV, "0.05")
        monkeypatch.setenv(TOOL_HARD_BUDGET_ENV, "0.3")
        server, handlers = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        assert "null_debug_sleep" in tools

        t0 = time.monotonic()
        out = asyncio.run(tools["null_debug_sleep"].fn(seconds=3.0))
        elapsed = time.monotonic() - t0

        # Returned EARLY with an error string (not the sleep's result).
        assert elapsed < 2.0, (
            f"watchdog did not return early (took {elapsed:.2f}s)"
        )
        assert "exceeded the hard budget" in out
        assert "abandoned" in out
        assert "null doctor" in out  # points at the breadcrumb surface

        entries = _violations(handlers)
        assert any(e["kind"] == "hard" and e["tool"] == "null_debug_sleep"
                   for e in entries)

    def test_debug_sleep_not_registered_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NULL_DEBUG_TOOLS", raising=False)
        server, _ = create_server(str(tmp_path))
        assert "null_debug_sleep" not in server._tool_manager._tools


class TestBreadcrumbStore:
    def test_history_is_bounded(self, tmp_path):
        server, handlers = create_server(str(tmp_path))
        for i in range(25):
            _record_budget_violation(
                handlers, f"null_tool_{i}", elapsed=99.0,
                budget=15.0, kind="soft",
            )
        entries = _violations(handlers)
        assert len(entries) == 20  # bounded — never grows unbounded
        # Most recent kept (FIFO eviction).
        assert entries[-1]["tool"] == "null_tool_24"
        assert entries[0]["tool"] == "null_tool_5"

    def test_breadcrumb_never_raises_on_broken_db(self, tmp_path, capsys):
        server, handlers = create_server(str(tmp_path))
        handlers.memory.db.conn.close()  # sabotage the DB
        # Must not raise — the breadcrumb sits on the must-respond path.
        _record_budget_violation(handlers, "null_status", 99.0, 15.0, "soft")
        # The stderr fallback still fired.
        assert "tool budget SOFT" in capsys.readouterr().err


class TestDoctorSurfacing:
    def test_doctor_surfaces_recorded_violations(self, tmp_path):
        from null_memory.agent import AgentMemory
        from tests.conftest import quiesce_mem, run_null

        mem = AgentMemory.load(str(tmp_path))
        mem.db.set_meta(BUDGET_VIOLATIONS_META_KEY, json.dumps([
            {"tool": "null_briefing", "elapsed_s": 61.2, "budget_s": 60.0,
             "kind": "hard", "at": "2026-06-12T00:00:00+00:00"},
            {"tool": "null_recall", "elapsed_s": 16.0, "budget_s": 15.0,
             "kind": "soft", "at": "2026-06-12T00:01:00+00:00"},
        ]))
        mem.db.conn.commit()
        quiesce_mem(mem)

        rc, out, err = run_null("doctor", tmp_path=tmp_path)
        assert "tool budget violation" in out
        assert "1 hard/abandoned, 1 soft/slow" in out
        assert "null_briefing" in out
