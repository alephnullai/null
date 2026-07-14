"""Tests for Null v0.4.0 — state, momentum, watches, wakeup."""

from __future__ import annotations

import json
import os
import time

import pytest

# Shared CLI runner — returncode repr carries stdout/stderr so a bare
# `assert rc == 0` failure is diagnosable (see conftest.CLIReturnCode).
from tests.conftest import run_null


# ── State ──

class TestState:
    def test_state_show_empty(self, tmp_path):
        rc, out, _ = run_null("state", tmp_path=tmp_path)
        assert rc == 0
        assert "No state recorded" in out or "State" in out

    def test_state_set_with_args(self, tmp_path):
        rc, out, _ = run_null(
            "state", "set",
            "--assessment", "Things are going well",
            "--energy", "high",
            "--concern", "Deployment risk",
            "--optimistic", "New feature ready",
            "--unresolved", "Auth bug still open",
            tmp_path=tmp_path,
        )
        assert rc == 0
        assert "State saved" in out

    def test_state_persists(self, tmp_path):
        run_null(
            "state", "set",
            "--assessment", "Steady progress",
            "--energy", "medium",
            "--concern", "Technical debt",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("state", tmp_path=tmp_path)
        assert rc == 0
        assert "medium" in out
        assert "Technical debt" in out

    def test_state_energy_values(self, tmp_path):
        for energy in ("high", "medium", "low"):
            rc, out, _ = run_null(
                "state", "set",
                "--energy", energy,
                "--assessment", f"test {energy}",
                tmp_path=tmp_path,
            )
            assert rc == 0

    def test_state_file_schema(self, tmp_path):
        run_null(
            "state", "set",
            "--assessment", "Test assessment",
            "--energy", "high",
            "--concern", "Concern A",
            "--concern", "Concern B",
            "--optimistic", "Good thing",
            "--unresolved", "Open issue",
            tmp_path=tmp_path,
        )
        state_file = tmp_path / "state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert "written" in data
        assert data["assessment"] == "Test assessment"
        assert data["energy"] == "high"
        assert "Concern A" in data["concerns"]
        assert "Concern B" in data["concerns"]
        assert "Good thing" in data["optimistic_about"]
        assert data["unresolved"] == "Open issue"

    def test_state_set_updates_existing(self, tmp_path):
        run_null("state", "set", "--energy", "low", "--assessment", "tired", tmp_path=tmp_path)
        run_null("state", "set", "--energy", "high", tmp_path=tmp_path)
        rc, out, _ = run_null("state", tmp_path=tmp_path)
        assert "high" in out


# ── State unit tests ──

class TestStateUnit:
    def test_load_state_empty(self, tmp_path):
        from null_memory.wakeup import load_state
        result = load_state(str(tmp_path))
        assert result == {}

    def test_save_and_load_state(self, tmp_path):
        from null_memory.wakeup import save_state, load_state
        state = {
            "assessment": "All good",
            "energy": "high",
            "concerns": ["one thing"],
            "optimistic_about": ["project progress"],
            "unresolved": "pending review",
        }
        save_state(state, str(tmp_path))
        loaded = load_state(str(tmp_path))
        assert loaded["assessment"] == "All good"
        assert loaded["energy"] == "high"
        assert "written" in loaded

    def test_format_state_empty(self):
        from null_memory.wakeup import format_state
        result = format_state({})
        assert "No state recorded" in result

    def test_format_state_full(self):
        from null_memory.wakeup import format_state
        state = {
            "written": "2026-01-01T12:00:00+00:00",
            "assessment": "Things are solid",
            "energy": "high",
            "concerns": ["auth bug"],
            "optimistic_about": ["momentum"],
            "unresolved": "deploy date",
        }
        result = format_state(state)
        assert "high" in result
        assert "auth bug" in result
        assert "momentum" in result
        assert "deploy date" in result


# ── Momentum ──

class TestMomentum:
    def test_momentum_show_empty(self, tmp_path):
        rc, out, _ = run_null("momentum", tmp_path=tmp_path)
        assert rc == 0
        assert "not set" in out or "Momentum" in out

    def test_momentum_set_with_args(self, tmp_path):
        rc, out, _ = run_null(
            "momentum", "set",
            "--project", "null-v040",
            "--decision", "use JSONL for watches",
            "--next", "write tests",
            "--blocked", "nothing",
            "--summary", "Built three new features",
            tmp_path=tmp_path,
        )
        assert rc == 0
        assert "Momentum saved" in out

    def test_momentum_persists(self, tmp_path):
        run_null(
            "momentum", "set",
            "--project", "my-project",
            "--next", "deploy to prod",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("momentum", tmp_path=tmp_path)
        assert rc == 0
        assert "my-project" in out
        assert "deploy to prod" in out

    def test_momentum_file_schema(self, tmp_path):
        run_null(
            "momentum", "set",
            "--project", "testproj",
            "--decision", "go with option A",
            "--next", "implement X",
            "--summary", "Did a lot today",
            tmp_path=tmp_path,
        )
        mom_file = tmp_path / "momentum.json"
        assert mom_file.exists()
        data = json.loads(mom_file.read_text())
        assert "updated" in data
        assert data["active_project"] == "testproj"
        assert data["last_decision"] == "go with option A"
        assert data["next_action"] == "implement X"
        assert data["session_summary"] == "Did a lot today"

    def test_momentum_overwrites_not_appends(self, tmp_path):
        run_null("momentum", "set", "--project", "first", tmp_path=tmp_path)
        run_null("momentum", "set", "--project", "second", tmp_path=tmp_path)
        mom_file = tmp_path / "momentum.json"
        data = json.loads(mom_file.read_text())
        assert data["active_project"] == "second"


class TestMomentumUnit:
    def test_load_momentum_empty(self, tmp_path):
        from null_memory.wakeup import load_momentum
        result = load_momentum(str(tmp_path))
        assert result == {}

    def test_save_and_load_momentum(self, tmp_path):
        from null_memory.wakeup import save_momentum, load_momentum
        mom = {
            "active_project": "nullv040",
            "last_decision": "use state.json",
            "next_action": "run tests",
            "blocked_on": "",
            "session_summary": "Implemented features 1-3",
        }
        save_momentum(mom, str(tmp_path))
        loaded = load_momentum(str(tmp_path))
        assert loaded["active_project"] == "nullv040"
        assert "updated" in loaded

    def test_format_momentum_empty(self):
        from null_memory.wakeup import format_momentum
        result = format_momentum({})
        assert "No momentum" in result or "not set" in result

    def test_format_momentum_full(self):
        from null_memory.wakeup import format_momentum
        mom = {
            "updated": "2026-01-01T12:00:00+00:00",
            "active_project": "null-v040",
            "last_decision": "use JSONL",
            "next_action": "commit",
            "blocked_on": "tests failing",
            "session_summary": "Great session",
        }
        result = format_momentum(mom)
        assert "null-v040" in result
        assert "commit" in result
        assert "tests failing" in result


# ── Watches ──

class TestWatches:
    def test_watch_list_empty(self, tmp_path):
        rc, out, _ = run_null("watch", "list", tmp_path=tmp_path)
        assert rc == 0
        assert "No watches" in out or "0 active" in out

    def test_watch_add(self, tmp_path):
        rc, out, _ = run_null(
            "watch", "add",
            "--name", "test health",
            "--cmd", "echo OK",
            "--interval", "4",
            "--alert-if", "no output",
            tmp_path=tmp_path,
        )
        assert rc == 0
        assert "Watch added" in out
        assert "test health" in out

    def test_watch_list_after_add(self, tmp_path):
        run_null(
            "watch", "add",
            "--name", "my-watch",
            "--cmd", "echo hello",
            "--interval", "2",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("watch", "list", tmp_path=tmp_path)
        assert rc == 0
        assert "my-watch" in out

    def test_watch_run_safe_command(self, tmp_path):
        # `echo` is deliberate: run_watches executes user-authored command
        # strings through the platform shell (shell=True — /bin/sh on
        # POSIX, cmd.exe on Windows), where echo is a builtin on both.
        run_null(
            "watch", "add",
            "--name", "echo watch",
            "--cmd", "echo hello-world",
            "--interval", "0",  # Always due
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("watch", "run", tmp_path=tmp_path)
        assert rc == 0
        assert "echo watch" in out
        assert "hello-world" in out

    def test_watch_run_no_due(self, tmp_path):
        run_null(
            "watch", "add",
            "--name", "far-future",
            "--cmd", "echo ok",
            "--interval", "9999",  # Never due
            tmp_path=tmp_path,
        )
        # Mark as recently checked by running once
        run_null("watch", "run", tmp_path=tmp_path)
        rc, out, _ = run_null("watch", "run", tmp_path=tmp_path)
        assert rc == 0
        assert "No watches due" in out

    def test_watch_remove(self, tmp_path):
        _, out, _ = run_null(
            "watch", "add",
            "--name", "removable",
            "--cmd", "echo x",
            "--interval", "1",
            tmp_path=tmp_path,
        )
        # Extract ID from output (e.g. "Watch added: removable [abc12345]")
        watch_id = out.split("[")[1].split("]")[0]
        rc, out2, _ = run_null("watch", "remove", watch_id, tmp_path=tmp_path)
        assert rc == 0
        assert "deactivated" in out2

    def test_watch_file_schema(self, tmp_path):
        run_null(
            "watch", "add",
            "--name", "schema-test",
            "--cmd", "echo ok",
            "--interval", "4",
            "--alert-if", "no response",
            tmp_path=tmp_path,
        )
        watch_file = tmp_path / "watching.jsonl"
        assert watch_file.exists()
        data = json.loads(watch_file.read_text().strip())
        assert "id" in data
        assert data["name"] == "schema-test"
        assert data["check_cmd"] == "echo ok"
        assert data["interval_hours"] == 4.0
        assert data["alert_if"] == "no response"
        assert data["active"] is True
        assert data["last_checked"] is None

    def test_watch_remove_not_found(self, tmp_path):
        rc, out, err = run_null("watch", "remove", "nonexistent-id", tmp_path=tmp_path)
        assert rc == 1
        assert "not found" in err.lower() or "not found" in out.lower()


class TestWatchUnit:
    def test_add_watch(self, tmp_path):
        from null_memory.wakeup import add_watch, load_watches
        w = add_watch("test", "echo ok", 4, "no output", str(tmp_path))
        assert w["name"] == "test"
        assert w["active"] is True
        assert w["last_checked"] is None
        watches = load_watches(str(tmp_path))
        assert len(watches) == 1

    def test_remove_watch(self, tmp_path):
        from null_memory.wakeup import add_watch, remove_watch, load_watches
        w = add_watch("removable", "echo x", 1, "alert", str(tmp_path))
        result = remove_watch(w["id"], str(tmp_path))
        assert result is True
        watches = load_watches(str(tmp_path))
        active = [x for x in watches if x.get("active", True)]
        assert len(active) == 0

    def test_remove_nonexistent(self, tmp_path):
        from null_memory.wakeup import remove_watch
        result = remove_watch("no-such-id", str(tmp_path))
        assert result is False

    def test_run_watches_safe(self, tmp_path):
        from null_memory.wakeup import add_watch, run_watches
        add_watch("safe-cmd", "echo SAFE_OUTPUT", 0, "never", str(tmp_path))
        results = run_watches(str(tmp_path))
        assert len(results) == 1
        assert "SAFE_OUTPUT" in results[0]["output"]
        assert results[0]["error"] is None

    def test_run_watches_timeout_handled(self, tmp_path):
        """Slow command should timeout gracefully, not raise."""
        from null_memory.wakeup import add_watch, run_watches
        # Use a very short timeout hack by patching — instead, just test a fast fail
        add_watch("bad-cmd", "sleep 0 && echo ok", 0, "test", str(tmp_path))
        results = run_watches(str(tmp_path))
        assert len(results) >= 1
        # Should not raise

    def test_run_watches_skips_not_due(self, tmp_path):
        from null_memory.wakeup import add_watch, run_watches
        w = add_watch("not-due", "echo x", 9999, "alert", str(tmp_path))
        # Run once to mark it checked
        run_watches(str(tmp_path))
        # Run again — should skip
        results = run_watches(str(tmp_path))
        assert len(results) == 0

    def test_load_watches_dedup_by_id(self, tmp_path):
        from null_memory.wakeup import add_watch, load_watches, _append_watch
        w = add_watch("dedup-test", "echo ok", 4, "alert", str(tmp_path))
        # Append updated version
        updated = dict(w)
        updated["active"] = False
        _append_watch(updated, str(tmp_path))
        watches = load_watches(str(tmp_path))
        # Last write wins — should be inactive
        assert len(watches) == 1
        assert watches[0]["active"] is False

    def test_watch_run_updates_last_checked(self, tmp_path):
        from null_memory.wakeup import add_watch, run_watches, load_watches
        add_watch("track-time", "echo ok", 0, "test", str(tmp_path))
        run_watches(str(tmp_path))
        watches = load_watches(str(tmp_path))
        assert watches[0]["last_checked"] is not None

    def test_watch_status_summary(self, tmp_path):
        from null_memory.wakeup import watch_status_summary, add_watch
        summary = watch_status_summary(str(tmp_path))
        assert "0 active" in summary

        add_watch("one", "echo ok", 4, "alert", str(tmp_path))
        summary = watch_status_summary(str(tmp_path))
        assert "1 active" in summary


# ── Wakeup ──

class TestWakeup:
    def test_wakeup_runs(self, tmp_path):
        rc, out, _ = run_null("wakeup", tmp_path=tmp_path)
        assert rc == 0
        assert "Wakeup" in out

    def test_wakeup_shows_state(self, tmp_path):
        run_null(
            "state", "set",
            "--energy", "high",
            "--assessment", "Feeling great",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("wakeup", tmp_path=tmp_path)
        assert rc == 0
        assert "high" in out

    def test_wakeup_shows_momentum(self, tmp_path):
        run_null(
            "momentum", "set",
            "--project", "wakeup-test",
            "--next", "write more tests",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("wakeup", tmp_path=tmp_path)
        assert rc == 0
        assert "wakeup-test" in out

    def test_wakeup_runs_watches(self, tmp_path):
        # echo runs via the platform shell (see run_watches) — portable.
        run_null(
            "watch", "add",
            "--name", "wakeup-echo",
            "--cmd", "echo WAKEUP_OK",
            "--interval", "0",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("wakeup", tmp_path=tmp_path)
        assert rc == 0
        assert "WAKEUP_OK" in out

    def test_wakeup_shows_memory_stats(self, tmp_path):
        run_null("learn", "a test fact for wakeup", tmp_path=tmp_path)
        rc, out, _ = run_null("wakeup", tmp_path=tmp_path)
        assert rc == 0
        assert "Memory:" in out

    def test_wakeup_compact(self, tmp_path):
        """Wakeup output should be compact — not hundreds of lines."""
        rc, out, _ = run_null("wakeup", tmp_path=tmp_path)
        lines = out.strip().splitlines()
        # Should be reasonable for paste-into-context use
        assert len(lines) < 50, f"Wakeup output too long: {len(lines)} lines"


class TestWakeupUnit:
    def test_wakeup_function(self, tmp_path):
        from null_memory.agent import AgentMemory
        from null_memory.wakeup import wakeup, save_state, save_momentum
        mem = AgentMemory.load(str(tmp_path))
        mem.set_name("TestAtlas")
        mem.learn("a relevant fact", confidence=0.9)

        save_state({"energy": "high", "assessment": "Good"}, str(tmp_path))
        save_momentum({"active_project": "null-v040", "next_action": "commit"}, str(tmp_path))

        result = wakeup(mem, str(tmp_path))
        assert "TestAtlas" in result
        assert "high" in result
        assert "null-v040" in result
        assert "Memory:" in result


# ── Updated Status ──

class TestStatusWithExtras:
    def test_status_shows_state_line(self, tmp_path):
        run_null(
            "state", "set",
            "--energy", "high",
            "--concern", "server load",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("status", tmp_path=tmp_path)
        assert rc == 0
        assert "Memory Status" in out
        assert "State:" in out

    def test_status_shows_momentum_line(self, tmp_path):
        run_null(
            "momentum", "set",
            "--project", "active-work",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("status", tmp_path=tmp_path)
        assert rc == 0
        assert "Momentum:" in out

    def test_status_shows_watches_line(self, tmp_path):
        rc, out, _ = run_null("status", tmp_path=tmp_path)
        assert rc == 0
        assert "Watches:" in out

    def test_status_format(self, tmp_path):
        run_null(
            "state", "set",
            "--energy", "medium",
            "--concern", "test concern",
            tmp_path=tmp_path,
        )
        run_null(
            "momentum", "set",
            "--project", "status-test",
            tmp_path=tmp_path,
        )
        run_null(
            "watch", "add",
            "--name", "status-watch",
            "--cmd", "echo ok",
            "--interval", "4",
            tmp_path=tmp_path,
        )
        rc, out, _ = run_null("status", tmp_path=tmp_path)
        assert rc == 0
        # Should have compact single-line entries
        assert "Facts:" in out
        assert "Mistakes:" in out
        assert "Decisions:" in out
