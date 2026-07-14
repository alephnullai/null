"""Tests for `null exchange post --data-file` (Windows-shell-safe JSON input).

Inline --data JSON gets mangled by quoting rules on some shells (Windows
PowerShell 5.1 most notably), which forced seats to generate temp Python
scripts just to post. --data-file reads the payload from a UTF-8 file
('-' = stdin) so a seat can Write a file and post it with plain argv.

No network: a local bare git repo is the exchange remote (mirrors
tests/test_exchange.py / test_attend.py)."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from null_memory.agent import AgentMemory
from null_memory.exchange import ExchangeClient

from tests.conftest import quiesce_mem


def _git(cwd, *args) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", *args], cwd=str(cwd),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


@pytest.fixture
def bare(tmp_path):
    bare = tmp_path / "org-exchange.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")
    return bare


@pytest.fixture
def seat(tmp_path, bare):
    d = tmp_path / "seata"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({
        "machine_id": "seata-mach",
        "exchange": {"url": str(bare), "subscribe": []},
    }))
    mem = AgentMemory.load(str(d))
    yield mem
    quiesce_mem(mem)


def _post_args(**kw):
    class A:
        exchange_cmd = "post"
        kind = kw.get("kind", "broadcast")
        data = kw.get("data")
        data_file = kw.get("data_file")
        scope = kw.get("scope", "org")
    return A()


def _stream_events(mem):
    client = ExchangeClient(mem)
    path = client.clone_dir + "/streams/" + client.stream + ".jsonl"
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


class TestDataFile:
    def test_posts_payload_from_file(self, seat, tmp_path, monkeypatch, capsys):
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: seat)
        payload = tmp_path / "payload.json"
        payload.write_text(
            json.dumps({"text": "posted from a file — quotes \" and $ intact"}),
            encoding="utf-8")
        cli._handle_exchange(_post_args(data_file=str(payload)), None)
        out = capsys.readouterr().out
        assert "Posted broadcast" in out
        events = _stream_events(seat)
        assert events[-1]["data"]["text"].startswith("posted from a file")

    def test_reads_stdin_with_dash(self, seat, monkeypatch, capsys):
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: seat)
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"text": "via stdin"})))
        cli._handle_exchange(_post_args(data_file="-"), None)
        assert "Posted broadcast" in capsys.readouterr().out
        assert _stream_events(seat)[-1]["data"]["text"] == "via stdin"

    def test_missing_file_exits_1_with_flag_name(self, seat, monkeypatch, capsys):
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: seat)
        with pytest.raises(SystemExit) as exc:
            cli._handle_exchange(
                _post_args(data_file=str("no/such/file.json")), None)
        assert exc.value.code == 1
        assert "--data-file" in capsys.readouterr().err

    def test_invalid_json_in_file_names_data_file(self, seat, tmp_path,
                                                  monkeypatch, capsys):
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: seat)
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            cli._handle_exchange(_post_args(data_file=str(bad)), None)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--data-file" in err and "not valid JSON" in err

    def test_inline_data_still_works(self, seat, monkeypatch, capsys):
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: seat)
        cli._handle_exchange(
            _post_args(data=json.dumps({"text": "inline unchanged"})), None)
        assert "Posted broadcast" in capsys.readouterr().out
        assert _stream_events(seat)[-1]["data"]["text"] == "inline unchanged"
