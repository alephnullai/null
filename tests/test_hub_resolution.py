"""Hub resolution for `null persona create` (issue #22).

The first cross-machine worker creation registered a seat into the wrong
hub silently: NULL_DIR lived only inside the MCP server's env in
~/.claude.json, not in the user's shell, so the CLI fell back to ~/.null
while the serving Atlas read a different registry. The fixes under test:

  1. The resolved hub is always printed: ``Hub: <dir> (from ...)``.
  2. ``--hub`` (alias ``--null-dir``) targets a hub explicitly.
  3. Falling back to the default ~/.null while a DIFFERENT NULL_DIR is
     discoverable in ~/.claude.json mcpServers entries prints a
     prominent warning naming both paths.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys

import pytest

from null_memory.persona_wizard import (
    create_worker,
    discover_configured_hubs,
    hub_resolution_report,
    resolve_hub,
)


# ── resolve_hub ────────────────────────────────────────────────────────────


class TestResolveHub:
    def test_explicit_hub_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_DIR", str(tmp_path / "env-hub"))
        hub, source = resolve_hub(str(tmp_path / "flag-hub"))
        assert hub == str(tmp_path / "flag-hub")
        assert source == "--hub"

    def test_env_null_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_DIR", str(tmp_path / "env-hub"))
        hub, source = resolve_hub(None)
        assert hub == str(tmp_path / "env-hub")
        assert source == "NULL_DIR"

    def test_default_fallback(self, monkeypatch):
        monkeypatch.delenv("NULL_DIR", raising=False)
        hub, source = resolve_hub(None)
        assert hub == os.path.join(os.path.expanduser("~"), ".null")
        assert source == "default"

    def test_empty_env_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("NULL_DIR", "")
        _, source = resolve_hub(None)
        assert source == "default"


# ── discover_configured_hubs (read-only ~/.claude.json scan) ──────────────


class TestDiscoverConfiguredHubs:
    def _write(self, tmp_path, data) -> str:
        path = tmp_path / ".claude.json"
        path.write_text(json.dumps(data))
        return str(path)

    def test_finds_top_level_and_project_entries(self, tmp_path):
        path = self._write(tmp_path, {
            "mcpServers": {
                "null": {"command": "py", "env": {"NULL_DIR": "P:/null-atlas"}},
                "other": {"command": "x"},
            },
            "projects": {
                "/some/proj": {
                    "mcpServers": {
                        "athena": {"env": {"NULL_DIR": "/mnt/athena-hub"}},
                    },
                },
            },
        })
        hubs = discover_configured_hubs(path)
        assert hubs == ["P:/null-atlas", "/mnt/athena-hub"]

    def test_missing_file_is_fail_soft(self, tmp_path):
        assert discover_configured_hubs(str(tmp_path / "nope.json")) == []

    def test_corrupt_file_is_fail_soft(self, tmp_path):
        path = tmp_path / ".claude.json"
        path.write_text("{ not json")
        assert discover_configured_hubs(str(path)) == []

    def test_duplicates_collapsed(self, tmp_path):
        path = self._write(tmp_path, {
            "mcpServers": {
                "a": {"env": {"NULL_DIR": "/hub"}},
                "b": {"env": {"NULL_DIR": "/hub"}},
            },
        })
        assert discover_configured_hubs(path) == ["/hub"]


# ── hub_resolution_report ──────────────────────────────────────────────────


class TestHubResolutionReport:
    def test_prints_resolved_hub_and_source(self, tmp_path):
        lines = hub_resolution_report(str(tmp_path), "NULL_DIR")
        assert lines == [f"Hub: {tmp_path} (from NULL_DIR)"]

    def test_warns_on_default_fallback_with_configured_hub(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "null": {"env": {"NULL_DIR": "P:/petes_code/null-atlas"}},
            },
        }))
        default_hub = str(tmp_path / ".null")
        lines = hub_resolution_report(
            default_hub, "default", claude_json_path=str(claude_json))
        text = "\n".join(lines)
        assert f"Hub: {default_hub} (from default)" in text
        assert "WARNING" in text
        # names both paths and suggests --hub
        assert default_hub in text
        assert "P:/petes_code/null-atlas" in text
        assert "--hub" in text

    def test_no_warning_when_source_is_not_default(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {"null": {"env": {"NULL_DIR": "/other/hub"}}},
        }))
        lines = hub_resolution_report(
            str(tmp_path / "hub"), "NULL_DIR",
            claude_json_path=str(claude_json))
        assert not any("WARNING" in line for line in lines)

    def test_no_warning_when_configured_hub_is_the_default(self, tmp_path):
        default_hub = str(tmp_path / ".null")
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {"null": {"env": {"NULL_DIR": default_hub}}},
        }))
        lines = hub_resolution_report(
            default_hub, "default", claude_json_path=str(claude_json))
        assert not any("WARNING" in line for line in lines)


# ── create_worker --hub plumbing ───────────────────────────────────────────


class TestCreateWorkerHub:
    def test_explicit_hub_overrides_env(self, tmp_path, monkeypatch):
        env_hub = tmp_path / "env-hub"
        flag_hub = tmp_path / "flag-hub"
        monkeypatch.setenv("NULL_DIR", str(env_hub))

        result = create_worker("athena", focus="x", hub=str(flag_hub))

        assert result["hub"] == str(flag_hub)
        assert result["hub_source"] == "--hub"
        assert result["dir"] == str(flag_hub / "personalities" / "athena")
        # registered in the FLAG hub's registry, not the env hub's
        assert (flag_hub / "multiverse.db").is_file()
        assert not (env_hub / "multiverse.db").exists()

    def test_default_resolution_reports_source(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_DIR", str(tmp_path / "hub"))
        result = create_worker("athena", focus="x")
        assert result["hub"] == str(tmp_path / "hub")
        assert result["hub_source"] == "NULL_DIR"


# ── CLI surface ────────────────────────────────────────────────────────────


def _run_cli(args, env):
    return subprocess.run(
        [sys.executable, "-m", "null_memory.cli", *args],
        capture_output=True, encoding="utf-8", errors="replace", env=env,
    )


def test_cli_prints_hub_resolution(tmp_path):
    from tests.conftest import run_null

    rc, out, _ = run_null(
        "persona", "create", "steve", "--focus", "hiwave-linux",
        tmp_path=tmp_path,
    )
    assert rc == 0
    assert f"Hub: {tmp_path} (from NULL_DIR)" in out


def test_cli_hub_flag_targets_explicit_hub(tmp_path):
    from tests.conftest import run_null

    hub = tmp_path / "explicit-hub"
    rc, out, _ = run_null(
        "persona", "create", "steve", "--hub", str(hub),
        tmp_path=tmp_path,  # NULL_DIR points elsewhere — flag must win
    )
    assert rc == 0, out
    assert f"Hub: {hub} (from --hub)" in out
    conn = sqlite3.connect(hub / "multiverse.db")
    try:
        row = conn.execute(
            "SELECT dir FROM personalities WHERE name='steve'"
        ).fetchone()
        assert row is not None
        assert row[0] == "personalities/steve"  # relative (issue #23)
    finally:
        conn.close()
    assert not (tmp_path / "multiverse.db").exists()


def test_cli_warns_when_default_hub_disagrees_with_claude_json(tmp_path):
    """The exact incident shape: plain shell (no NULL_DIR), but
    ~/.claude.json configures a different hub for the MCP server."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    live_hub = tmp_path / "live-hub"
    (fake_home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "null": {
                "type": "stdio", "command": "py",
                "args": ["-m", "null_memory.cli", "serve"],
                "env": {"NULL_DIR": str(live_hub)},
            },
        },
    }))

    env = os.environ.copy()
    env.pop("NULL_DIR", None)  # plain shell — no NULL_DIR exported
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)  # Windows expanduser

    result = _run_cli(
        ["persona", "create", "athena", "--focus", "hiwave-windows"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    default_hub = os.path.join(str(fake_home), ".null")
    assert f"Hub: {default_hub} (from default)" in out
    assert "WARNING" in out
    assert str(live_hub) in out
    assert "--hub" in out
