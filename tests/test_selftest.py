"""Tests for `null selftest` — the RESPONSIVENESS CONTRACT release gate.

The selftest drives a fresh MCP server over stdio and exercises EVERY tool
on the 15-tool surface against a per-tool time budget. It is the
regression guard for the class of bug unit tests miss: a real 9-minute
null_identity hang once shipped while 1172 unit tests passed.

Covered here:
  * budget math (base, heavy factor, NULL_SELFTEST_BUDGET_MULT),
  * probe surface kept in sync with the server's registered tools,
  * budget table shape (tool / elapsed / budget / status) + exit codes,
  * the TIMEOUT path: a hung tool is killed and the run CONTINUES.
"""

import inspect

import pytest

from null_memory.selftest import (
    SELFTEST_BASE_BUDGET,
    SELFTEST_BUDGET_MULT_ENV,
    SELFTEST_PROBES,
    SELFTEST_TOOL_SURFACE,
    budget_for,
    budget_multiplier,
    format_report,
    run_selftest,
)
from tests.conftest import run_null


class TestBudgetMath:
    def test_default_base_budget(self):
        assert budget_for("null_recall", mult=1.0) == SELFTEST_BASE_BUDGET

    def test_heavy_tools_get_double_budget(self):
        # The historical 9-minute hang lived in null_identity; briefing
        # builds the largest payload. Both get 2x — but ONLY those two.
        for tool in ("null_identity", "null_briefing"):
            assert budget_for(tool, mult=1.0) == 2 * SELFTEST_BASE_BUDGET
        assert budget_for("null_status", mult=1.0) == SELFTEST_BASE_BUDGET

    def test_env_multiplier_scales_everything(self, monkeypatch):
        monkeypatch.setenv(SELFTEST_BUDGET_MULT_ENV, "3")
        assert budget_multiplier() == 3.0
        assert budget_for("null_status") == 3 * SELFTEST_BASE_BUDGET
        assert budget_for("null_identity") == 6 * SELFTEST_BASE_BUDGET

    def test_garbage_or_nonpositive_multiplier_is_one(self, monkeypatch):
        for raw in ("banana", "", "0", "-2"):
            monkeypatch.setenv(SELFTEST_BUDGET_MULT_ENV, raw)
            assert budget_multiplier() == 1.0
        monkeypatch.delenv(SELFTEST_BUDGET_MULT_ENV)
        assert budget_multiplier() == 1.0


class TestProbeSurfaceSync:
    def test_every_registered_tool_has_a_probe(self, tmp_path, monkeypatch):
        """A new MCP tool can't ship without a selftest probe (and a
        removed tool can't leave a ghost probe behind)."""
        monkeypatch.delenv("NULL_LEGACY_TOOLS", raising=False)
        monkeypatch.delenv("NULL_DEBUG_TOOLS", raising=False)
        from null_memory.mcp.server import create_server

        server, _ = create_server(str(tmp_path))
        registered = set(server._tool_manager._tools.keys())
        assert SELFTEST_TOOL_SURFACE == registered, (
            f"unprobed tools: {registered - SELFTEST_TOOL_SURFACE}; "
            f"ghost probes: {SELFTEST_TOOL_SURFACE - registered}"
        )

    def test_close_is_the_last_probe(self):
        # null_close ends the session — anything after it would probe a
        # closed-session edge case instead of the normal path.
        assert SELFTEST_PROBES[-1][0] == "null_close"


class TestSelftestCLI:
    def test_selftest_exits_zero_and_prints_budget_table(self, tmp_path):
        # Fresh throwaway store via --store so we never touch real memory.
        rc, out, err = run_null("selftest", "--store", str(tmp_path))
        assert rc == 0, err
        # Budget table header: tool / elapsed / budget / status.
        assert "responsiveness contract" in out
        for col in ("tool", "elapsed", "budget", "status"):
            assert col in out
        # Every tool on the surface appears as a row.
        for tool in SELFTEST_TOOL_SURFACE:
            assert tool in out
        # No regressions: every probe row ends in OK. (The summary line
        # contains FAIL/SLOW/TIMEOUT as labels, so assert on tool rows.)
        tool_rows = [
            ln for ln in out.splitlines()
            if ln.strip().startswith("null_")
        ]
        assert len(tool_rows) == len(SELFTEST_PROBES)
        for ln in tool_rows:
            assert ln.rstrip().endswith("OK"), ln


class TestSelftestStructured:
    """Drive run_selftest() directly so we can assert on the report."""

    def test_full_surface_all_ok_on_throwaway_store(self):
        # No store arg: must mkdtemp its own throwaway store and clean up.
        report = run_selftest(budget=5.0)
        assert report["ok"] is True
        assert report["base_budget"] == 5.0

        results = report["results"]
        assert len(results) == len(SELFTEST_PROBES)
        # Budget table shape: every row carries the full contract tuple.
        for r in results:
            assert set(r) >= {"tool", "seconds", "budget", "status"}, r
            assert r["status"] == "OK", f"{r['tool']} was {r['status']}"
            assert r["budget"] == budget_for(r["tool"], base=5.0)

        by_tool = {r["tool"] for r in results}
        assert by_tool == SELFTEST_TOOL_SURFACE

        # The historically-hanging tool hung for ~9 minutes; assert a
        # generous wall-clock ceiling so loaded CI machines don't flake
        # while a true hang regression still fails fast.
        identity = next(r for r in results if r["tool"] == "null_identity")
        assert identity["seconds"] < 30.0, (
            f"null_identity took {identity['seconds']:.3f}s — "
            "possible hang regression"
        )

        # Throwaway store cleaned up.
        import os
        assert not os.path.exists(report["store"])

    def test_format_report_renders_table_and_summary(self):
        report = {
            "results": [
                {"tool": "null_status", "seconds": 0.1, "budget": 10.0,
                 "status": "OK"},
                {"tool": "null_identity", "seconds": 31.2, "budget": 20.0,
                 "status": "TIMEOUT", "detail": "no response — server killed"},
            ],
            "ok": False, "base_budget": 10.0, "multiplier": 1.0,
            "store": "/tmp/x",
        }
        lines = format_report(report)
        text = "\n".join(lines)
        for col in ("tool", "elapsed", "budget", "status"):
            assert col in text
        assert "null_status" in text and "null_identity" in text
        assert "1 OK, 0 SLOW, 0 FAIL, 1 TIMEOUT of 2 probes" in text
        assert "RELEASE GATE: RED" in text
        assert "server killed" in text  # detail lines surface in the table

    def test_spawns_with_sys_executable_not_bare_python(self):
        # Belt-and-suspenders: the helper must use sys.executable so it
        # works under the project venv (never bare "python").
        source = inspect.getsource(run_selftest)
        assert "sys.executable" in source
        assert '"python"' not in source


class TestSelftestTimeoutPath:
    """THE contract: a hung tool gets TIMEOUT, is killed, and the run
    continues — it can never hang the release gate itself."""

    def test_hung_tool_times_out_and_run_continues(self):
        # null_debug_sleep (NULL_DEBUG_TOOLS=1, test-only registration)
        # sleeps far past the kill deadline (budget 1s x kill factor 3).
        # NULL_TOOL_HARD_BUDGET is raised so the server-side watchdog
        # doesn't answer first — this test exercises the CLIENT-side kill.
        report = run_selftest(
            budget=1.0,
            probes=[
                ("null_debug_sleep", {"seconds": 120}),
                ("null_status", {}),
            ],
            extra_env={
                "NULL_DEBUG_TOOLS": "1",
                "NULL_TOOL_HARD_BUDGET": "600",
            },
        )
        assert report["ok"] is False  # → nonzero exit via handle_selftest

        sleep_row, status_row = report["results"]
        assert sleep_row["tool"] == "null_debug_sleep"
        assert sleep_row["status"] == "TIMEOUT"
        assert "server killed" in sleep_row["detail"]

        # The run continued on a FRESH server: the next probe answered.
        assert status_row["tool"] == "null_status"
        assert status_row["status"] in ("OK", "SLOW"), status_row

    def test_timeout_report_exits_nonzero_via_handler(self, tmp_path, monkeypatch):
        import null_memory.selftest as selftest_mod

        fake_report = {
            "results": [{"tool": "null_status", "seconds": 99.0,
                         "budget": 10.0, "status": "TIMEOUT",
                         "detail": "x"}],
            "ok": False, "base_budget": 10.0, "multiplier": 1.0,
            "store": str(tmp_path),
        }
        monkeypatch.setattr(selftest_mod, "run_selftest",
                            lambda **kw: fake_report)

        class Args:
            store = None
            budget = None

        assert selftest_mod.handle_selftest(Args()) == 1
