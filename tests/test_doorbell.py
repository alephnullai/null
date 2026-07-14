"""Tests for the UDP doorbell (issue #20 Phase B).

All sockets are localhost-only (bind 127.0.0.1, ephemeral ports via
port=0) — no network. Covers: datagram → ring, contentless-ping security
(garbage datagrams are harmless and content is ignored), debounce through
PokeWorker.force, silent sender failures, config-driven ring_from_store,
and the daemon wiring (PokeWorker + DoorbellListener spawned/stopped)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from null_memory.agent import AgentMemory
from null_memory.doorbell import (
    DEFAULT_DOORBELL_PORT,
    DoorbellListener,
    _parse_peer,
    ring_from_store,
    ring_peers,
)
from null_memory.poke import PokeWorker

from tests.conftest import quiesce_mem


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _send(port: int, payload: bytes = b"x") -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, ("127.0.0.1", port))
    finally:
        sock.close()


@pytest.fixture
def listener():
    """Localhost listener on an ephemeral port; rings counted thread-safely."""
    rings = []
    lock = threading.Lock()

    def on_ring():
        with lock:
            rings.append(time.monotonic())

    lst = DoorbellListener(on_ring=on_ring, bind="127.0.0.1", port=0)
    assert lst.start()
    yield lst, rings
    lst.stop()


# ── peer parsing ────────────────────────────────────────────────────────


def test_parse_peer_defaults_port():
    assert _parse_peer("10.0.0.5") == ("10.0.0.5", DEFAULT_DOORBELL_PORT)
    assert _parse_peer("10.0.0.5:1234") == ("10.0.0.5", 1234)
    assert _parse_peer("") is None
    assert _parse_peer("host:notaport") is None


# ── listener: any datagram rings ────────────────────────────────────────


def test_datagram_triggers_ring(listener):
    lst, rings = listener
    _send(lst.port)
    assert _wait_for(lambda: len(rings) == 1)


def test_contentless_ping_security_garbage_is_harmless(listener):
    """SECURITY: the ping carries and trusts NOTHING. Arbitrary garbage
    bytes from any source produce at most a ring (an early fetch) — the
    listener never parses content, never crashes, never acts on it."""
    lst, rings = listener
    _send(lst.port, os.urandom(300))
    _send(lst.port, b'{"evil": "payload", "cmd": "rm -rf"}')
    _send(lst.port, b"")  # zero-length datagram
    assert _wait_for(lambda: len(rings) >= 2)  # empty dgram may not deliver
    # Listener is still alive and ringing after garbage. Snapshot BEFORE
    # sending — on Windows, localhost UDP delivers fast enough that the
    # ring can land between the send and a post-send snapshot (flake
    # caught on the athena seat's first full Windows run).
    before = len(rings)
    _send(lst.port, b"ok")
    assert _wait_for(lambda: len(rings) > before)


def test_connection_reset_does_not_kill_listener(listener):
    """Windows (WSAECONNRESET): recvfrom on a UDP socket raises
    ConnectionResetError when a prior send from the socket drew an ICMP
    port-unreachable. ConnectionResetError IS an OSError, so the generic
    shutdown handler silently killed the listener on the first stray
    reset — the bell went permanently deaf while the daemon looked
    healthy. The listener must shrug and keep receiving."""
    lst, rings = listener

    class _ResetOnce:
        def __init__(self, real):
            self._real = real
            self._fired = False

        def recvfrom(self, bufsize):
            if not self._fired:
                self._fired = True
                raise ConnectionResetError(10054, "WSAECONNRESET")
            return self._real.recvfrom(bufsize)

        def __getattr__(self, name):
            return getattr(self._real, name)

    lst._sock = _ResetOnce(lst._sock)
    _send(lst.port)
    assert _wait_for(lambda: len(rings) >= 1), \
        "listener died on ConnectionResetError instead of continuing"


def test_start_is_idempotent(listener):
    """A second start() on a live listener must not double-bind or spawn
    a second thread — it reports success for the listener that exists
    (issue #35's operator confusion came from exactly this shape)."""
    lst, rings = listener
    first_thread = lst._thread
    assert lst.start() is True
    assert lst._thread is first_thread
    _send(lst.port)
    assert _wait_for(lambda: len(rings) >= 1)


def test_on_ring_exception_does_not_kill_listener():
    calls = []

    def exploding():
        calls.append(1)
        raise RuntimeError("boom")

    lst = DoorbellListener(on_ring=exploding, bind="127.0.0.1", port=0)
    assert lst.start()
    try:
        _send(lst.port)
        assert _wait_for(lambda: len(calls) == 1)
        _send(lst.port)
        assert _wait_for(lambda: len(calls) == 2)
    finally:
        lst.stop()


def test_bind_failure_returns_false():
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("127.0.0.1", 0))
    port = taken.getsockname()[1]
    try:
        # SO_REUSEADDR lets two UDP binds coexist on some platforms; use a
        # listener whose bind addr is invalid instead — deterministic.
        lst = DoorbellListener(on_ring=lambda: None,
                               bind="255.255.255.255", port=port)
        assert lst.start() is False
    finally:
        taken.close()


# ── doorbell → forced poke cycle (debounce honored) ─────────────────────


def test_datagram_forces_poke_cycle_with_debounce(tmp_path):
    """A datagram flood collapses into ONE forced cycle per debounce
    window: the doorbell rings every time, PokeWorker.force debounces."""
    mem = AgentMemory.load(str(tmp_path / "seat"))
    try:
        worker = PokeWorker(mem, interval_seconds=3600,
                            force_debounce_seconds=10.0)
        rings = []

        def on_ring():
            rings.append(1)
            worker.force()

        lst = DoorbellListener(on_ring=on_ring, bind="127.0.0.1", port=0)
        assert lst.start()
        try:
            for _ in range(3):
                _send(lst.port)
            assert _wait_for(lambda: len(rings) == 3)
            assert worker.stats["forced"] == 1
            assert worker.stats["force_debounced"] == 2
        finally:
            lst.stop()
    finally:
        quiesce_mem(mem)


def test_forced_cycle_actually_runs(tmp_path):
    """End-to-end: started worker + listener; a datagram wakes the worker
    out of its hour-long sleep and runs a cycle immediately."""
    mem = AgentMemory.load(str(tmp_path / "seat"))
    worker = PokeWorker(mem, interval_seconds=3600,
                        force_debounce_seconds=0.05)
    lst = DoorbellListener(on_ring=worker.force, bind="127.0.0.1", port=0)
    try:
        worker.start()
        assert _wait_for(lambda: worker.stats["cycles"] >= 1)  # boot cycle
        assert lst.start()
        time.sleep(0.1)  # clear the debounce window
        _send(lst.port)
        assert _wait_for(lambda: worker.stats["cycles"] >= 2)
    finally:
        lst.stop()
        worker.stop()
        quiesce_mem(mem)


# ── sender side ─────────────────────────────────────────────────────────


def test_ring_peers_delivers_datagram():
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5.0)
    port = receiver.getsockname()[1]
    try:
        assert ring_peers([f"127.0.0.1:{port}"]) == 1
        data, _addr = receiver.recvfrom(64)
        assert data == b"\x00"
    finally:
        receiver.close()


def test_ring_peers_failures_are_silent():
    # Unresolvable host + garbage entries: no exception, poll remains
    # the guarantee.
    sent = ring_peers(["definitely-not-a-host.invalid:47474",
                       "bad:port:garbage", "", None])
    assert sent == 0
    assert ring_peers([]) == 0


def test_ring_from_store_uses_config(tmp_path):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5.0)
    port = receiver.getsockname()[1]
    store = tmp_path / "store"
    store.mkdir()
    (store / "config.json").write_text(json.dumps(
        {"doorbell_peers": [f"127.0.0.1:{port}"]}))
    try:
        assert ring_from_store(str(store)) == 1
        receiver.recvfrom(64)  # delivered
    finally:
        receiver.close()


def test_ring_from_store_without_config_is_noop(tmp_path):
    assert ring_from_store(str(tmp_path / "nowhere")) == 0


# ── daemon wiring ───────────────────────────────────────────────────────


def test_daemon_spawns_poke_worker_and_doorbell(tmp_path, monkeypatch):
    from null_memory.daemon import DaemonRunner
    from null_memory.migrate_v3 import init_unified_db

    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    # Store config: localhost-only doorbell on an ephemeral port, slow poke.
    (tmp_path / "config.json").write_text(json.dumps({
        "doorbell_bind": "127.0.0.1",
        "doorbell_port": 0,
        "poke_interval_minutes": 60,
    }))

    # Keep the test focused: stub out the heavy HypnosLive sub-worker.
    class _StubHypnos:
        def __init__(self, _mem):
            pass

        def start(self):
            pass

        def stop(self, timeout=5.0):
            pass

    import null_memory.hypnos_live as hl
    monkeypatch.setattr(hl, "HypnosLiveWorker", _StubHypnos)

    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified
    runner = DaemonRunner(mem)
    try:
        runner.start()
        assert runner._poke_worker is not None
        assert runner._doorbell is not None
        port = runner._doorbell.port
        assert port > 0  # ephemeral port resolved
        status = runner.status()
        assert status["doorbell_port"] == port
        assert isinstance(status["poke"], dict)
        _send(port)
        assert _wait_for(lambda: runner._doorbell.rings >= 1)
    finally:
        runner.stop()
        quiesce_mem(mem)


def test_daemon_doorbell_disabled_by_config(tmp_path, monkeypatch):
    from null_memory.daemon import DaemonRunner
    from null_memory.migrate_v3 import init_unified_db

    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "doorbell_enabled": False,
        "poke_interval_minutes": 60,
    }))

    class _StubHypnos:
        def __init__(self, _mem):
            pass

        def start(self):
            pass

        def stop(self, timeout=5.0):
            pass

    import null_memory.hypnos_live as hl
    monkeypatch.setattr(hl, "HypnosLiveWorker", _StubHypnos)

    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    runner = DaemonRunner(mem)
    try:
        runner.start()
        assert runner._poke_worker is not None
        assert runner._doorbell is None
    finally:
        runner.stop()
        quiesce_mem(mem)


def test_daemon_runs_on_worker_seat_without_unified(tmp_path, monkeypatch):
    """A worker seat (per-personality store, NO unified.db) must still get
    the Phase B receive path: poke worker + doorbell run; only HypnosLive
    (which genuinely requires unified) is skipped. The previous whole-daemon
    unified gate silently disabled the doorbell on every worker seat —
    caught live on the athena seat (daemon.log: 'unified DB required')."""
    from null_memory.daemon import DaemonRunner

    seat = tmp_path / "personalities" / "steve"
    seat.mkdir(parents=True)
    monkeypatch.setenv("NULL_DIR", str(seat))
    (seat / "config.json").write_text(json.dumps({
        "doorbell_bind": "127.0.0.1",
        "doorbell_port": 0,
        "poke_interval_minutes": 60,
    }))

    mem = AgentMemory.load(agent_dir=str(seat), personality="steve")
    assert not mem.db.unified  # the worker-seat shape, by design
    runner = DaemonRunner(mem)
    try:
        runner.start()
        assert runner._poke_worker is not None, "poke loop must run on seats"
        assert runner._doorbell is not None, "doorbell must run on seats"
        assert runner._hypnos_worker is None, "HypnosLive needs unified"
        assert runner._thread is not None and runner._thread.is_alive()
        _send(runner._doorbell.port)
        assert _wait_for(lambda: runner._doorbell.rings >= 1)
    finally:
        runner.stop()
        quiesce_mem(mem)
