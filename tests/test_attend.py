"""Tests for the NULL attention loop (`null attend`).

The attention layer is distinct from store freshness: the daemon poke
loop keeps the STORE fresh (ingest cursor), while `null attend` wakes the
CONVERSATIONAL layer to SURFACE what arrived, tracked by a SEPARATE
``exchange_attended.<stream>`` cursor.

The load-bearing regression here is the DUAL CURSOR: simulate the daemon
ingesting (which advances ``exchange_cursor.<stream>``), then assert
attend STILL surfaces the message — because attention reads its own
``exchange_attended.<stream>`` offset off the stream files directly.

No network: a local bare git repo is the exchange remote (mirrors
tests/test_exchange.py)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from null_memory.agent import AgentMemory
from null_memory.exchange import ExchangeClient, attend_render_lines

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


def _make_seat(tmp_path, name: str, url: str, subscribe: list[str]):
    seat = tmp_path / name
    seat.mkdir()
    (seat / "config.json").write_text(json.dumps({
        "machine_id": f"{name}-mach",
        "exchange": {"url": url, "subscribe": subscribe},
    }))
    return AgentMemory.load(str(seat))


@pytest.fixture
def seats(tmp_path, bare):
    """Seat A (worker) and seat B (hub, subscribed to A's stream)."""
    mem_a = _make_seat(tmp_path, "seata", str(bare), [])
    mem_b = _make_seat(tmp_path, "seatb", str(bare), ["seata-mach.atlas"])
    yield mem_a, mem_b
    quiesce_mem(mem_a)
    quiesce_mem(mem_b)


ATTENDED_KEY = "exchange_attended.seata-mach.atlas"
INGEST_KEY = "exchange_cursor.seata-mach.atlas"


# ── surfacing + cursor advance ──────────────────────────────────────────


class TestAttendBasics:
    def test_surfaces_new_item_and_advances_attended_cursor(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("broadcast", {"text": "CI is green again"})

        client_b = ExchangeClient(mem_b)
        assert mem_b.db.get_meta(ATTENDED_KEY) is None  # no cursor yet
        result = client_b.attend()
        items = result["items"]
        assert len(items) == 1
        assert items[0]["writer"] == "seata-mach.atlas"
        assert items[0]["kind"] == "broadcast"
        assert items[0]["text"] == "CI is green again"
        # Attended cursor now set (and non-zero — we consumed bytes).
        cursor = mem_b.db.get_meta(ATTENDED_KEY)
        assert cursor is not None and int(cursor) > 0

    def test_attended_cursor_advances_only_on_surface(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("broadcast", {"text": "one"})
        client_b = ExchangeClient(mem_b)
        first = client_b.attend()
        assert len(first["items"]) == 1
        moved = mem_b.db.get_meta(ATTENDED_KEY)
        # Second tick with nothing new: cursor unchanged, nothing surfaced.
        second = client_b.attend()
        assert second["items"] == []
        assert mem_b.db.get_meta(ATTENDED_KEY) == moved


# ── THE dual-cursor regression ──────────────────────────────────────────


class TestDualCursorRegression:
    def test_daemon_ingested_then_attend_still_surfaces(self, seats):
        """The crux: the daemon ingests first (advancing the INGEST
        cursor), and attend MUST still surface the message — because
        attention reads its own ATTENDED cursor, not the ingest delta."""
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("report.session", {
            "summary": "ALSA backend now passes loopback", "project": "hi"})

        # Simulate the daemon poke: ingest consumes into the store and
        # advances the INGEST cursor.
        client_b = ExchangeClient(mem_b)
        ingest_report = client_b.ingest()
        assert ingest_report["facts"] == 1
        ingest_cursor = mem_b.db.get_meta(INGEST_KEY)
        assert ingest_cursor is not None and int(ingest_cursor) > 0
        # A naive re-ingest now finds nothing — proving the trap is real.
        assert client_b.ingest()["streams"] == {}

        # ...yet attend STILL surfaces it: separate cursor, read off files.
        assert mem_b.db.get_meta(ATTENDED_KEY) is None
        result = client_b.attend()
        assert len(result["items"]) == 1
        assert "ALSA backend" in result["items"][0]["text"]
        # The two cursors are independent (and end up tracking the same
        # bytes here, but via separate keys).
        assert mem_b.db.get_meta(ATTENDED_KEY) is not None
        assert INGEST_KEY != ATTENDED_KEY

    def test_attend_does_not_disturb_ingest_cursor(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("broadcast", {"text": "x"})
        client_b = ExchangeClient(mem_b)
        client_b.attend()
        # Attention surfacing must not advance the daemon's ingest cursor —
        # the daemon still needs to ingest this into the store.
        assert mem_b.db.get_meta(INGEST_KEY) is None


# ── scoping ─────────────────────────────────────────────────────────────


class TestScoping:
    def test_own_stream_excluded(self, tmp_path, bare):
        mem = _make_seat(tmp_path, "seatself", str(bare),
                         ["seatself-mach.atlas"])  # misconfigured self-sub
        try:
            client = ExchangeClient(mem)
            assert client.subscribed == []  # own stream filtered
            client.post("broadcast", {"text": "talking to myself"})
            result = client.attend()
            assert result["items"] == []
        finally:
            quiesce_mem(mem)

    def test_unsubscribed_stream_not_surfaced(self, seats, tmp_path, bare):
        mem_a, mem_b = seats
        mem_c = _make_seat(tmp_path, "seatc", str(bare), [])
        try:
            ExchangeClient(mem_c).post("broadcast", {"text": "C's note"})
            ExchangeClient(mem_a).post("broadcast", {"text": "A's note"})
            result = ExchangeClient(mem_b).attend()
            texts = [it["text"] for it in result["items"]]
            assert "A's note" in texts
            assert "C's note" not in texts
        finally:
            quiesce_mem(mem_c)


# ── multiple items grouped across streams ───────────────────────────────


class TestGrouping:
    def test_multiple_items_across_streams_grouped(self, tmp_path, bare):
        mem_a = _make_seat(tmp_path, "seata", str(bare), [])
        mem_c = _make_seat(tmp_path, "seatc", str(bare), [])
        mem_b = _make_seat(tmp_path, "seatb", str(bare),
                           ["seata-mach.atlas", "seatc-mach.atlas"])
        try:
            ExchangeClient(mem_a).post("broadcast", {"text": "from A one"})
            ExchangeClient(mem_a).post("directive", {"text": "from A two"})
            ExchangeClient(mem_c).post("broadcast", {"text": "from C"})
            result = ExchangeClient(mem_b).attend()
            assert len(result["items"]) == 3
            writers = {it["writer"] for it in result["items"]}
            assert writers == {"seata-mach.atlas", "seatc-mach.atlas"}
            lines = attend_render_lines(result["items"])
            blob = "\n".join(lines)
            # Grouped by sender, both senders present.
            assert "── from seata-mach.atlas ──" in blob
            assert "── from seatc-mach.atlas ──" in blob
            assert "from A one" in blob and "from A two" in blob
            assert "from C" in blob
        finally:
            quiesce_mem(mem_a)
            quiesce_mem(mem_c)
            quiesce_mem(mem_b)


# ── --dry-run ───────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_shows_without_advancing(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("broadcast", {"text": "peek"})
        client_b = ExchangeClient(mem_b)
        result = client_b.attend(dry_run=True)
        assert len(result["items"]) == 1
        # Cursor NOT advanced — a real tick later still surfaces it.
        assert mem_b.db.get_meta(ATTENDED_KEY) is None
        again = client_b.attend()
        assert len(again["items"]) == 1


# ── limit ───────────────────────────────────────────────────────────────


class TestLimit:
    def test_limit_caps_but_cursor_covers_all(self, seats):
        mem_a, mem_b = seats
        ca = ExchangeClient(mem_a)
        for i in range(4):
            ca.post("broadcast", {"text": f"msg {i}"})
        client_b = ExchangeClient(mem_b)
        result = client_b.attend(limit=2)
        assert len(result["items"]) == 2
        # The cursor advanced over EVERYTHING scanned: the remainder does
        # NOT re-surface next tick (a cap is "show me a few", not a queue).
        assert client_b.attend()["items"] == []


# ── instrumentation: experimental-feature tick telemetry ────────────────


class TestTickCounters:
    def test_counts_news_and_quiet_ticks(self, seats):
        from null_memory.exchange import attend_status_lines
        mem_a, mem_b = seats
        client_b = ExchangeClient(mem_b)

        # No ticks yet → status line absent (experimental, opt-in: silent).
        assert attend_status_lines(mem_b.db) == []

        # First tick is quiet (nothing posted).
        client_b.attend()
        assert int(mem_b.db.get_meta("attend.ticks_total")) == 1
        assert int(mem_b.db.get_meta("attend.ticks_quiet")) == 1
        assert mem_b.db.get_meta("attend.ticks_news") in (None, "0")

        # Post then tick → a news tick.
        ExchangeClient(mem_a).post("broadcast", {"text": "ship it"})
        client_b.attend()
        assert int(mem_b.db.get_meta("attend.ticks_total")) == 2
        assert int(mem_b.db.get_meta("attend.ticks_news")) == 1
        assert int(mem_b.db.get_meta("attend.ticks_quiet")) == 1

        # Status now surfaces the experimental telemetry with idle fraction.
        lines = attend_status_lines(mem_b.db)
        assert len(lines) == 1
        assert "experimental" in lines[0]
        assert "2 ticks" in lines[0]
        assert "50% idle" in lines[0]

    def test_dry_run_does_not_count(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("broadcast", {"text": "x"})
        client_b = ExchangeClient(mem_b)
        client_b.attend(dry_run=True)
        # A dry-run is manual inspection, not a loop tick — never counted.
        assert mem_b.db.get_meta("attend.ticks_total") in (None, "0")


# ── CLI: quiet-when-nothing vs verbose, fail-soft ───────────────────────


class TestCLI:
    def _args(self, **kw):
        class A:
            verbose = kw.get("verbose", False)
            dry_run = kw.get("dry_run", False)
            limit = kw.get("limit", 0)
        return A()

    def test_quiet_when_nothing(self, seats, monkeypatch, capsys):
        mem_a, mem_b = seats
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: mem_b)
        rc = cli._handle_attend(self._args())
        out = capsys.readouterr()
        assert rc == 0
        assert out.out == ""  # silent by default when idle

    def test_verbose_announces_nothing_new(self, seats, monkeypatch, capsys):
        mem_a, mem_b = seats
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: mem_b)
        rc = cli._handle_attend(self._args(verbose=True))
        out = capsys.readouterr()
        assert rc == 0
        assert "nothing new" in out.out.lower()

    def test_surfaces_loud_when_present(self, seats, monkeypatch, capsys):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("broadcast", {"text": "LOUD message"})
        from null_memory import cli
        monkeypatch.setattr(cli, "_load_seat_memory", lambda: mem_b)
        rc = cli._handle_attend(self._args())
        out = capsys.readouterr()
        assert rc == 0
        assert "LOUD message" in out.out
        assert "seata-mach.atlas" in out.out

    def test_fail_soft_when_unconfigured(self, tmp_path, monkeypatch, capsys):
        mem = AgentMemory.load(str(tmp_path / "lone"))
        try:
            from null_memory import cli
            monkeypatch.setattr(cli, "_load_seat_memory", lambda: mem)
            # Quiet by default even when unconfigured.
            rc = cli._handle_attend(self._args())
            assert rc == 0
            assert capsys.readouterr().out == ""
            # Verbose gives a hint, still exit 0.
            rc = cli._handle_attend(self._args(verbose=True))
            assert rc == 0
            assert "not configured" in capsys.readouterr().out.lower()
        finally:
            quiesce_mem(mem)


# ── unconfigured client.attend() report ─────────────────────────────────


def test_attend_unconfigured_reports_warning(tmp_path):
    mem = AgentMemory.load(str(tmp_path / "lone"))
    try:
        result = ExchangeClient(mem).attend()
        assert result["warning"] == "exchange not configured"
        assert result["items"] == []
    finally:
        quiesce_mem(mem)
