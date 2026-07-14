"""Tests for `null setup --global` writing the location Claude Code reads.

Claude Code loads global MCP servers from the TOP-LEVEL mcpServers key of
~/.claude.json. The command previously wrote ~/.claude/.mcp.json — a file
Claude Code never reads — so "global" setup silently did nothing.
"""

from __future__ import annotations

import json
import os

import pytest

from null_memory.cli import _handle_setup_global


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser
    return tmp_path


def _claude_json(home):
    return os.path.join(str(home), ".claude.json")


def test_creates_claude_json_when_missing(fake_home):
    _handle_setup_global()
    with open(_claude_json(fake_home), encoding="utf-8") as f:
        cfg = json.load(f)
    entry = cfg["mcpServers"]["null"]
    assert entry["type"] == "stdio"
    assert entry["args"] == ["-m", "null_memory.cli", "serve"]
    assert os.path.basename(entry["command"]).startswith("python")
    # git env hardening always emitted (issue #24)
    assert entry["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert entry["env"]["GCM_INTERACTIVE"] == "never"


def test_merge_preserves_other_servers_and_state(fake_home):
    existing = {
        "numStartups": 42,
        "mcpServers": {
            "rube": {"type": "http", "url": "https://rube.app/mcp"},
        },
        "projects": {"P:/somewhere": {"history": ["x"]}},
    }
    with open(_claude_json(fake_home), "w", encoding="utf-8") as f:
        json.dump(existing, f)

    _handle_setup_global()

    with open(_claude_json(fake_home), encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg["numStartups"] == 42                       # state preserved
    assert cfg["mcpServers"]["rube"]["type"] == "http"    # other server kept
    assert cfg["projects"]["P:/somewhere"]["history"] == ["x"]
    assert "null" in cfg["mcpServers"]


def test_rerun_is_idempotent(fake_home):
    _handle_setup_global()
    first = open(_claude_json(fake_home), encoding="utf-8").read()
    _handle_setup_global()
    assert open(_claude_json(fake_home), encoding="utf-8").read() == first


def test_refuses_to_clobber_corrupt_file(fake_home):
    with open(_claude_json(fake_home), "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    with pytest.raises(SystemExit):
        _handle_setup_global()
    # untouched
    assert open(_claude_json(fake_home), encoding="utf-8").read() == "{ this is not json"


def test_does_not_write_dead_locations(fake_home):
    _handle_setup_global()
    assert not os.path.exists(os.path.join(str(fake_home), ".claude", ".mcp.json"))


_OTHER_INTERPRETER_CFG = {
    "mcpServers": {
        "null": {
            "type": "stdio",
            "command": "/opt/anaconda3/bin/python",  # pinned interpreter, != sys.executable
            "args": ["-m", "null_memory.cli", "serve"],
            "env": {"NULL_HOME": "/custom/null-home"},
        },
    },
}


def test_refuses_interpreter_swap_without_force(fake_home, capsys):
    with open(_claude_json(fake_home), "w", encoding="utf-8") as f:
        json.dump(_OTHER_INTERPRETER_CFG, f)
    before = open(_claude_json(fake_home), encoding="utf-8").read()

    with pytest.raises(SystemExit):
        _handle_setup_global()

    # untouched, and the old→new diff was shown
    assert open(_claude_json(fake_home), encoding="utf-8").read() == before
    err = capsys.readouterr().err
    assert "/opt/anaconda3/bin/python" in err
    assert "--force" in err


def test_force_overwrites_and_preserves_custom_env(fake_home):
    import sys as _sys

    with open(_claude_json(fake_home), "w", encoding="utf-8") as f:
        json.dump(_OTHER_INTERPRETER_CFG, f)

    _handle_setup_global(force=True)

    with open(_claude_json(fake_home), encoding="utf-8") as f:
        cfg = json.load(f)
    entry = cfg["mcpServers"]["null"]
    assert entry["command"] == _sys.executable            # interpreter swapped
    # custom env kept, AND the git hardening is always present (issue #24)
    assert entry["env"]["NULL_HOME"] == "/custom/null-home"
    assert entry["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert entry["env"]["GCM_INTERACTIVE"] == "never"

    # a one-time backup of the pre-rewrite file exists
    bak = _claude_json(fake_home) + ".bak"
    assert os.path.isfile(bak)
    with open(bak, encoding="utf-8") as f:
        assert json.load(f)["mcpServers"]["null"]["command"] == "/opt/anaconda3/bin/python"
