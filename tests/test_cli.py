"""Tests for Null CLI commands."""

import json
import os

import pytest

# Shared CLI runner — returncode repr carries stdout/stderr so a bare
# `assert rc == 0` failure is diagnosable (see conftest.CLIReturnCode).
from tests.conftest import run_null


class TestCLIStatus:
    def test_status_runs(self):
        rc, out, _ = run_null("status")
        assert rc == 0
        assert "Memory Status" in out

    def test_version(self):
        rc, out, _ = run_null("--version")
        assert rc == 0
        assert "null" in out


class TestCLILearn:
    def test_learn_and_recall(self, tmp_path):
        env = {"NULL_DIR": str(tmp_path)}
        # Learn doesn't use NULL_DIR (uses ~/.null), so we test via Python
        # Just verify the command parses correctly
        rc, out, _ = run_null("learn", "test fact for CLI")
        assert rc == 0
        assert "Learned" in out

    def test_learn_with_confidence(self):
        rc, out, _ = run_null("learn", "confident fact", "--confidence", "0.95")
        assert rc == 0
        assert "95%" in out

    def test_learn_with_project(self):
        rc, out, _ = run_null("learn", "project fact", "--project", "myproject")
        assert rc == 0


class TestCLIRecall:
    def test_recall_runs(self):
        rc, out, _ = run_null("recall", "test")
        assert rc == 0
        # Either finds results or says no match — both are valid


class TestCLIMistake:
    def test_mistake_records(self):
        rc, out, _ = run_null("mistake", "test mistake", "test reason")
        assert rc == 0
        assert "Mistake recorded" in out


class TestCLIReflect:
    def test_reflect_records(self):
        rc, out, _ = run_null("reflect", "went well", "was missed", "do different")
        assert rc == 0
        assert "Reflection saved" in out


class TestCLIGC:
    def test_gc_runs(self):
        rc, out, _ = run_null("gc")
        assert rc == 0
        assert "GC" in out


class TestCLIName:
    def test_name_set(self):
        # Save original name, set test name, restore
        rc, out, _ = run_null("status")
        rc, out, _ = run_null("name", "CLITestBot")
        assert rc == 0
        assert "CLITestBot" in out
        # Restore
        run_null("name", "Atlas")


class TestCLIExportImport:
    def test_export_to_stdout(self):
        rc, out, _ = run_null("export")
        assert rc == 0
        data = json.loads(out)
        assert "version" in data

    def test_export_to_file(self, tmp_path):
        outfile = str(tmp_path / "export.json")
        rc, out, _ = run_null("export", "-o", outfile)
        assert rc == 0
        assert os.path.isfile(outfile)


class TestCLINoArgs:
    def test_no_command_shows_help(self):
        rc, out, err = run_null()
        assert rc == 1  # Should exit with error


class TestSetupHooks:
    """P1-2: `null setup --hooks` registers deterministic capture hooks
    into the target project's .claude/settings.json. NEVER touches the
    user's real ~/.claude — every test operates on a tmp project dir."""

    def _settings(self, root):
        path = os.path.join(str(root), ".claude", "settings.json")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_creates_valid_settings(self, tmp_path):
        from null_memory.cli import _register_claude_hooks, NULL_HOOK_SPECS
        root = tmp_path / "proj"
        root.mkdir()
        summary = _register_claude_hooks(str(root))
        assert summary["settings_path"] == str(root / ".claude" / "settings.json")
        settings = self._settings(root)
        assert "hooks" in settings
        # Every shipped hook script registered under its event
        blob = json.dumps(settings["hooks"])
        for _event, _matcher, basename in NULL_HOOK_SPECS:
            assert basename in blob
        # Claude Code hook JSON shape
        for event, groups in settings["hooks"].items():
            assert isinstance(groups, list)
            for group in groups:
                assert "hooks" in group
                for hook in group["hooks"]:
                    assert hook["type"] == "command"
                    assert hook["command"]
        # file-change hook carries the tool matcher
        post = settings["hooks"]["PostToolUse"]
        assert any(g.get("matcher") == "Write|Edit|MultiEdit" for g in post)

    def test_idempotent_rerun_does_not_duplicate(self, tmp_path):
        from null_memory.cli import _register_claude_hooks
        root = tmp_path / "proj"
        root.mkdir()
        _register_claude_hooks(str(root))
        first = self._settings(root)
        summary = _register_claude_hooks(str(root))
        second = self._settings(root)
        assert first == second
        assert summary["added"] == []

    def test_preserves_unrelated_keys_and_hooks(self, tmp_path):
        from null_memory.cli import _register_claude_hooks
        root = tmp_path / "proj"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "env": {"FOO": "bar"},
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Bash",
                     "hooks": [{"type": "command", "command": "echo unrelated"}]}
                ]
            },
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing))

        _register_claude_hooks(str(root))
        settings = self._settings(root)
        assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
        assert settings["env"] == {"FOO": "bar"}
        bash_groups = [g for g in settings["hooks"]["PostToolUse"]
                       if g.get("matcher") == "Bash"]
        assert bash_groups and bash_groups[0]["hooks"][0]["command"] == "echo unrelated"

    def test_updates_stale_command_in_place(self, tmp_path):
        from null_memory.cli import _register_claude_hooks
        root = tmp_path / "proj"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        stale = {
            "hooks": {
                "SessionStart": [
                    {"matcher": "",
                     "hooks": [{"type": "command",
                                "command": "/old/python /old/null-session-hook.py"}]}
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(stale))

        summary = _register_claude_hooks(str(root))
        assert "null-session-hook.py" in summary["updated"]
        settings = self._settings(root)
        commands = [h["command"]
                    for g in settings["hooks"]["SessionStart"]
                    for h in g["hooks"]
                    if "null-session-hook.py" in h["command"]]
        assert len(commands) == 1  # updated, not duplicated
        assert commands[0] != "/old/python /old/null-session-hook.py"

    def test_refuses_to_clobber_invalid_json(self, tmp_path):
        from null_memory.cli import _register_claude_hooks
        root = tmp_path / "proj"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("{not valid json")
        summary = _register_claude_hooks(str(root))
        assert summary.get("error")
        assert (claude_dir / "settings.json").read_text() == "{not valid json"

    def test_cli_setup_hooks_end_to_end(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        rc, out, err = run_null("setup", str(root), "--hooks")
        assert rc == 0, err
        settings_path = root / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings

    def test_hook_install_status(self, tmp_path):
        from null_memory.cli import (
            _hook_install_status, _register_claude_hooks, NULL_HOOK_SPECS,
        )
        root = tmp_path / "proj"
        root.mkdir()
        status = _hook_install_status(str(root))
        assert status and not any(status.values())  # nothing registered yet
        _register_claude_hooks(str(root))
        status = _hook_install_status(str(root))
        assert all(status.values())
        assert set(status) == {b for _e, _m, b in NULL_HOOK_SPECS}


class TestDoctorInstallHealth:
    """P1-5: doctor detects editable/git-checkout installs and reports
    hook status; everything is fail-soft."""

    def test_detect_dev_install_shape(self):
        from null_memory.cli import _detect_dev_install
        info = _detect_dev_install()
        assert set(info) == {"editable", "repo_root", "dirty"}
        # This test suite runs from the git checkout (editable install).
        # .git is a dir in a normal clone but a file in a git worktree —
        # both count (mirrors _detect_dev_install's exists() check).
        assert info["editable"] is True
        assert info["repo_root"] and os.path.exists(
            os.path.join(info["repo_root"], ".git"))
        assert info["dirty"] in (True, False, None)

    def test_detect_dev_install_fail_soft_without_git(self, monkeypatch):
        from null_memory.cli import _detect_dev_install
        import subprocess as sp

        def boom(*args, **kwargs):
            raise FileNotFoundError("git not installed")

        monkeypatch.setattr(sp, "run", boom)
        info = _detect_dev_install()  # must not raise
        assert info["editable"] is True
        assert info["dirty"] is None

    def test_doctor_reports_install_and_hooks(self, tmp_path):
        rc, out, err = run_null("doctor")
        assert rc == 0, err
        assert "Install:" in out
        assert "Hooks:" in out


class TestCLIProbeAdd:
    """`null probe add` — CLI home for the probe surface cut from MCP."""

    def test_probe_add_records(self, tmp_path):
        env = {"NULL_DIR": str(tmp_path)}
        rc, out, err = run_null(
            "probe", "add", "What region is staging in?", "us-east-2",
            env_override=env,
        )
        assert rc == 0, err
        assert "Probe added" in out
        assert "us-east-2" in out
        # Response must point at the CLI, not the removed MCP tools.
        assert "null_doctor" not in out and "null_calibrate" not in out
        assert "null doctor" in out and "null calibrate" in out

    def test_probe_add_persists_with_category(self, tmp_path):
        import sqlite3
        env = {"NULL_DIR": str(tmp_path)}
        rc, out, err = run_null(
            "probe", "add", "Who owns the deploy pipeline?", "the infra team",
            "--category", "user",
            env_override=env,
        )
        assert rc == 0, err
        db_files = list(tmp_path.rglob("*.db"))
        assert db_files, "no database created under NULL_DIR"
        rows = []
        for db in db_files:
            conn = sqlite3.connect(str(db))
            try:
                rows += conn.execute(
                    "SELECT question, expected, probe_type FROM probes"
                ).fetchall()
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        assert ("Who owns the deploy pipeline?", "the infra team", "user") in rows

    def test_probe_without_subcommand_errors(self, tmp_path):
        env = {"NULL_DIR": str(tmp_path)}
        rc, out, err = run_null("probe", env_override=env)
        assert rc == 1


class TestCLIOutreachSend:
    """`null outreach send` — manual emission, CLI home for null_outreach."""

    @staticmethod
    def _init_unified(tmp_path):
        from null_memory.migrate_v3 import init_unified_db
        init_unified_db(str(tmp_path / "unified.db")).close()

    def test_send_writes_log_and_db(self, tmp_path):
        import sqlite3
        self._init_unified(tmp_path)
        env = {"NULL_DIR": str(tmp_path)}
        rc, out, err = run_null(
            "outreach", "send", "Heads up", "The nightly build broke twice.",
            "--urgency", "0.7", "--channel", "log",
            env_override=env,
        )
        assert rc == 0, err
        assert "[outreach] sent id=" in out
        assert "Heads up" in out
        # Row landed in the outreaches table
        conn = sqlite3.connect(str(tmp_path / "unified.db"))
        rows = conn.execute(
            "SELECT subject, body, urgency, delivered, trigger_id FROM outreaches"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        subject, body, urgency, delivered, trigger_id = rows[0]
        assert subject == "Heads up"
        assert "nightly build" in body
        assert abs(urgency - 0.7) < 1e-9
        assert delivered == 1
        assert trigger_id is None  # manual sends have no trigger
        # Log channel wrote under NULL_DIR
        log = tmp_path / "outreaches.log"
        assert log.exists()
        assert "Heads up" in log.read_text()

    def test_send_default_urgency_and_channel(self, tmp_path):
        self._init_unified(tmp_path)
        env = {"NULL_DIR": str(tmp_path)}
        rc, out, err = run_null(
            "outreach", "send", "Subject only", "Body only",
            env_override=env,
        )
        assert rc == 0, err
        assert "via log" in out

    def test_send_fails_cleanly_without_outreach_tables(self, tmp_path):
        # Fresh legacy (non-unified) DB has no outreaches table —
        # the command must fail with a message, not a traceback.
        env = {"NULL_DIR": str(tmp_path)}
        rc, out, err = run_null(
            "outreach", "send", "x", "y", env_override=env,
        )
        assert rc == 1
        assert "send failed" in err
        assert "Traceback" not in err
