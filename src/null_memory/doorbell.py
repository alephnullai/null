"""UDP doorbell — the contentless dirty-ping (issue #20 Phase B).

After any store push or exchange post, the sender fires one tiny UDP
datagram at each configured peer meaning only "fetch now". The receiver's
daemon hears it and forces an immediate poke cycle instead of waiting for
the next poll. Lost pings cost nothing — the periodic poll remains the
delivery guarantee; the ping is pure acceleration.

SECURITY MODEL (deliberate, load-bearing):
    The ping carries NOTHING and the receiver trusts NOTHING about it.
    Datagram content is ignored entirely — any byte pattern from any
    source produces at most one debounced "fetch now". All real data
    still arrives over the authenticated git transports and is replayed
    idempotently. The worst an attacker on the LAN can do is make the
    daemon fetch a few seconds early; there is no content to spoof, no
    state to corrupt, no audit hole, and near-zero attack surface. A
    flood of garbage datagrams collapses into one forced cycle per
    debounce window (see poke.PokeWorker.force).

Config (per-store config.json — see events.load_store_config):
    "doorbell_enabled": true,            # listener on/off (default on)
    "doorbell_port": 47474,              # UDP listen port
    "doorbell_bind": "0.0.0.0",          # default: all LAN interfaces
    "doorbell_peers": ["host[:port]"]    # who to ring after a push/post
"""

from __future__ import annotations

import logging
import socket
import threading

from null_memory.events import load_store_config

logger = logging.getLogger("null.doorbell")

DEFAULT_DOORBELL_PORT = 47474
DEFAULT_DOORBELL_BIND = "0.0.0.0"
# One ping payload — content is ignored by receivers; a single byte keeps
# some stacks happier than a zero-length datagram.
PING_PAYLOAD = b"\x00"


class DoorbellListener:
    """Tiny UDP listener: any datagram → ``on_ring()``.

    Content is IGNORED by design (see module docstring); only the source
    address is logged. Debouncing lives in the callback (PokeWorker.force
    enforces max one forced cycle per window), so the listener itself
    stays a dumb bell."""

    def __init__(self, on_ring, bind: str = DEFAULT_DOORBELL_BIND,
                 port: int = DEFAULT_DOORBELL_PORT):
        self.on_ring = on_ring
        self.bind_addr = bind
        self._requested_port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.rings = 0  # datagrams heard (observability/tests)

    @property
    def port(self) -> int:
        """Actual bound port (meaningful when constructed with port=0)."""
        if self._sock is not None:
            try:
                return self._sock.getsockname()[1]
            except OSError:
                pass
        return self._requested_port

    def start(self) -> bool:
        """Bind + start the listener thread. Returns False (and logs) when
        the port can't be bound — the poll remains the guarantee.
        Idempotent: a second start() on a live listener is a no-op True."""
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_addr, self._requested_port))
            sock.settimeout(1.0)
        except OSError as exc:
            # Precise wording matters (issue #35): the port being taken
            # does NOT mean rings go unheard — if another null daemon on
            # this machine holds it, THAT instance receives them and its
            # poke cycle fetches the same remotes. Only this instance
            # lacks a listener; the poll remains the guarantee either way.
            logger.warning(
                "[doorbell] bind %s:%s failed: %s — no listener in THIS "
                "instance. If another null daemon on this machine holds "
                "the port, rings are still heard there; the periodic poll "
                "covers delivery regardless.",
                self.bind_addr, self._requested_port, exc)
            return False
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="null-doorbell", daemon=True)
        self._thread.start()
        logger.info("[doorbell] listening on %s:%s",
                    self.bind_addr, self.port)
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _data, addr = self._sock.recvfrom(512)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows quirk (WSAECONNRESET): a UDP socket's recvfrom
                # raises this when an earlier datagram SENT from the same
                # socket drew an ICMP port-unreachable. It says nothing
                # about our ability to keep receiving — treating it as
                # the generic OSError shutdown case killed the listener
                # permanently on the first stray reset.
                continue
            except OSError:
                break  # socket closed under us — shutdown path
            # Content deliberately ignored — contentless by design.
            self.rings += 1
            logger.info("[doorbell] ring from %s — forcing poke cycle",
                        addr[0])
            try:
                self.on_ring()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[doorbell] on_ring failed: %s", exc)


def _parse_peer(peer: str) -> tuple[str, int] | None:
    """'host' or 'host:port' → (host, port). None on garbage."""
    peer = (peer or "").strip()
    if not peer:
        return None
    host, sep, port_s = peer.rpartition(":")
    if not sep:
        return peer, DEFAULT_DOORBELL_PORT
    try:
        return host, int(port_s)
    except ValueError:
        return None


def ring_peers(peers: list[str]) -> int:
    """Fire one datagram at each peer address. Failures are SILENT — the
    periodic poll is the delivery guarantee, the ping is acceleration.
    Returns the number of datagrams attempted (observability/tests)."""
    sent = 0
    if not peers:
        return 0
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return 0
    try:
        for peer in peers:
            parsed = _parse_peer(str(peer))
            if parsed is None:
                continue
            try:
                sock.sendto(PING_PAYLOAD, parsed)
                sent += 1
            except OSError:
                pass  # silent by design
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return sent


def ring_from_store(store_dir: str) -> int:
    """Ring every peer in the store config's ``doorbell_peers`` list.
    Best-effort and silent — callers fire-and-forget after a push/post."""
    try:
        cfg = load_store_config(store_dir)
        peers = cfg.get("doorbell_peers") or []
        if not isinstance(peers, list):
            return 0
        return ring_peers(peers)
    except Exception:  # noqa: BLE001 — never break the push path
        return 0
