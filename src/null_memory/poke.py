"""The poke loop — replay-on-pull for same-store replicas
(issue #20 Phase B; design: docs/design/EVENT_SOURCED_SYNC.md).

Every N minutes (store config ``poke_interval_minutes``, default 5) the
daemon's PokeWorker runs one poke cycle:

  1. ``git fetch`` the store repo via the hardened ``_run_git``
     (non-interactive, tree-kill timeout — issue #4 machinery).
  2. Fast-forward pull when clean. Divergence on event-log files is
     structurally impossible (append-only, one writer per file); on
     anything else the cycle surfaces a warning and never merges.
  3. Replay new event-log lines into the live db through replay.py's
     appliers — idempotent by Phase A construction (INSERT OR IGNORE
     adds, field-level LWW updates), tracked by per-log byte cursors in
     the meta table (``replay_cursor.<file>``).
  4. Ingest the org exchange when configured (see exchange.py).
  5. Fire the wakeup path (due watches run) and record freshness so the
     briefing can show ONE line:
     ``↓ store updated from <writer> Nm ago — X events``.

The UDP doorbell (doorbell.py) forces an immediate cycle, debounced to at
most one forced cycle per 10 seconds — the poll remains the guarantee,
the ping is acceleration.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from null_memory.events import (
    EVENTS_DIRNAME,
    GENESIS_KIND,
    _utc_now_iso,
    get_machine_id,
    load_store_config,
)

logger = logging.getLogger("null.poke")

DEFAULT_POKE_INTERVAL_MINUTES = 5.0
MIN_POKE_INTERVAL_SECONDS = 5.0          # runaway guard
FORCE_DEBOUNCE_SECONDS = 10.0            # max one forced cycle per 10s
POKE_LEADER_KEY = "null_poke_leader"
POKE_LAST_UPDATE_KEY = "poke_last_update"


def poke_interval_seconds(store_dir: str) -> float:
    cfg = load_store_config(store_dir)
    try:
        minutes = float(cfg.get("poke_interval_minutes",
                                DEFAULT_POKE_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_POKE_INTERVAL_MINUTES
    return max(MIN_POKE_INTERVAL_SECONDS, minutes * 60.0)


# ── one cycle ───────────────────────────────────────────────────────────────


def poke_once(mem: Any) -> dict:
    """Run one poke cycle against an AgentMemory. Returns a report dict:
    {fetched, fast_forwarded, warning, replayed, writers, exchange}."""
    report: dict[str, Any] = {
        "fetched": False, "fast_forwarded": False, "warning": None,
        "replayed": 0, "writers": {}, "exchange": None,
    }
    store_dir = os.path.dirname(mem.db.db_path)

    # 1+2 — fetch + ff-only pull of the store repo.
    from null_memory.session import MemoryRepo, _run_git
    repo = MemoryRepo(store_dir)
    if repo.is_repo():
        remote = _run_git(["remote"], cwd=repo.repo_dir)
        if remote.stdout.strip():
            fetch = _run_git(["fetch", "--quiet"], cwd=repo.repo_dir,
                             timeout=30)
            report["fetched"] = fetch.returncode == 0
            pull = _run_git(["pull", "--ff-only", "--quiet"],
                            cwd=repo.repo_dir, timeout=30)
            if pull.returncode == 0:
                report["fast_forwarded"] = True
            else:
                # Event logs cannot diverge (append-only, disjoint files);
                # this is a stray non-log file. Warn — never merge.
                report["warning"] = (
                    "store pull was not a fast-forward — not merging "
                    "(divergence on non-event-log files needs a human): "
                    + (pull.stderr or "").strip()[:200])
                logger.warning("[poke] %s", report["warning"])

    # 3 — replay new event-log lines (idempotent, cursor-tracked).
    replayed, writers = replay_new_log_lines(mem)
    report["replayed"] = replayed
    report["writers"] = writers

    # 4 — exchange ingestion (different-identity edges).
    try:
        from null_memory.exchange import ExchangeClient
        client = ExchangeClient(mem)
        if client.available:
            report["exchange"] = client.ingest()
    except Exception as exc:  # noqa: BLE001 — exchange must not kill the cycle
        logger.warning("[poke] exchange ingestion failed: %s", exc)

    # 5 — freshness record + wakeup path.
    ex = report["exchange"] or {}
    fresh = replayed > 0 or any(
        ex.get(k) for k in ("facts", "claims", "repo_pushes", "queries"))
    if fresh:
        mem.db.set_meta(POKE_LAST_UPDATE_KEY, json.dumps({
            "ts": _utc_now_iso(),
            "events": replayed,
            "writers": sorted(writers, key=writers.get, reverse=True),
        }))
        mem.db.conn.commit()
        try:
            # The wakeup path: run due watches so triggers fire promptly
            # on freshly-arrived state instead of next session start.
            from null_memory.wakeup import run_watches
            run_watches(getattr(mem, "agent_dir", None))
        except Exception:  # noqa: BLE001
            pass
    return report


def replay_new_log_lines(mem: Any) -> tuple[int, dict[str, int]]:
    """Replay new lines from every FOREIGN event log (and genesis file)
    under <store>/events/ into the live db. Returns (count, {writer: n}).

    Own files are skipped — this writer's events are already committed
    truth here (and replay is idempotent anyway, so a misnamed file can't
    corrupt anything). Per-file byte cursors live in meta
    (``replay_cursor.<filename>``); a shrunken file (re-baselined genesis)
    resets its cursor and re-replays idempotently."""
    from null_memory.exchange import _read_new_lines
    from null_memory.replay import apply_events, ensure_fact_columns

    store_dir = os.path.dirname(mem.db.db_path)
    events_dir = os.path.join(store_dir, EVENTS_DIRNAME)
    if not os.path.isdir(events_dir):
        return 0, {}

    writer_id = f"{get_machine_id(store_dir)}.{getattr(mem, 'personality', 'atlas')}"
    own_files = {f"{writer_id}.jsonl", f"{GENESIS_KIND}.{writer_id}.jsonl"}

    new_events: list[dict] = []
    cursor_updates: list[tuple[str, int]] = []
    for name in sorted(os.listdir(events_dir)):
        if not name.endswith(".jsonl") or name in own_files:
            continue
        path = os.path.join(events_dir, name)
        cursor_key = f"replay_cursor.{name}"
        offset = int(mem.db.get_meta(cursor_key) or 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < offset:
            offset = 0  # file rewritten (genesis --force) — replay from top
        if size == offset:
            continue
        events, new_offset = _read_new_lines(path, offset)
        new_events.extend(events)
        cursor_updates.append((cursor_key, new_offset))

    if not new_events:
        return 0, {}

    # Deterministic replay order; genesis headers carry no row state.
    applicable = [e for e in new_events if e.get("kind") != GENESIS_KIND]
    applicable.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""),
                                   e.get("seq", 0)))
    ensure_fact_columns(mem.db.conn)
    stats = apply_events(mem.db, applicable)

    # Cursors advance only after a successful apply — a crash mid-cycle
    # re-replays the same lines next time (idempotent by construction).
    for key, offset in cursor_updates:
        mem.db.set_meta(key, str(offset))
    mem.db.conn.commit()

    writers: dict[str, int] = {}
    for e in applicable:
        w = e.get("writer", "?")
        writers[w] = writers.get(w, 0) + 1
    logger.info("[poke] replayed %s events from %s",
                stats.get("applied", 0), ", ".join(sorted(writers)) or "—")
    return len(applicable), writers


# ── briefing line ───────────────────────────────────────────────────────────


def render_sync_lines(db: Any) -> list[str]:
    """ONE briefing line when fresh:
    ``↓ store updated from <writer> Nm ago — X events``.

    Fresh = the last poke update is newer than the last clean session
    close, capped at 24h (the same boundary the Hypnos section uses), so
    the line shows once and doesn't nag forever."""
    raw = db.get_meta(POKE_LAST_UPDATE_KEY)
    if not raw:
        return []
    try:
        update = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    ts = update.get("ts", "")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return []
    now = datetime.now(timezone.utc)
    boundary = now - timedelta(hours=24)
    try:
        pred, params = db._personality_predicate()
        where = f" WHERE {pred}" if pred else ""
        row = db.conn.execute(
            f"""SELECT MAX(created_at) FROM session_fingerprints
               {where + ' AND' if where else 'WHERE'}
               outcome IN ('clean', 'neutral')""", params).fetchone()
        if row and row[0]:
            close_dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            boundary = max(boundary, close_dt)
    except Exception:  # noqa: BLE001 — no fingerprints table: 24h cap alone
        pass
    if dt <= boundary:
        return []
    mins = max(0, int((now - dt).total_seconds() // 60))
    writers = update.get("writers") or ["?"]
    n = update.get("events", 0)
    return [f"  ↓ store updated from {', '.join(writers)} {mins}m ago "
            f"— {n} events"]


# ── the worker (daemon-embedded) ────────────────────────────────────────────


class PokeWorker:
    """Periodic poke cycle on its own thread; the daemon embeds one
    (like HypnosLiveWorker). ``force()`` — wired to the UDP doorbell —
    triggers an immediate cycle, debounced to at most one forced cycle
    per FORCE_DEBOUNCE_SECONDS.

    Single-poker-per-store via the shared LeaderLock (key
    ``null_poke_leader``): N daemons/servers may embed workers; exactly
    one replays per store at a time."""

    def __init__(self, memory: Any, interval_seconds: float | None = None,
                 force_debounce_seconds: float = FORCE_DEBOUNCE_SECONDS):
        self.memory = memory
        store_dir = os.path.dirname(memory.db.db_path)
        self.interval = (max(MIN_POKE_INTERVAL_SECONDS, interval_seconds)
                         if interval_seconds is not None
                         else poke_interval_seconds(store_dir))
        self.force_debounce = force_debounce_seconds
        self.instance_id = f"{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._leader = None
        self._last_force_monotonic: float | None = None
        self._force_lock = threading.Lock()
        self.stats = {"cycles": 0, "forced": 0, "force_debounced": 0,
                      "errors": 0, "skipped_not_leader": 0}
        self.last_report: dict | None = None

    # ── lifecycle ──

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"null-poke-{self.instance_id}",
            daemon=True)
        self._thread.start()
        logger.info("[poke] worker started interval=%.0fs instance=%s",
                    self.interval, self.instance_id)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def force(self) -> bool:
        """Request an immediate cycle (doorbell path). Debounced: at most
        one forced cycle per ``force_debounce`` seconds — a datagram flood
        collapses into one early fetch. Returns True when accepted."""
        with self._force_lock:
            now = time.monotonic()
            if (self._last_force_monotonic is not None
                    and now - self._last_force_monotonic
                    < self.force_debounce):
                self.stats["force_debounced"] += 1
                return False
            self._last_force_monotonic = now
        self.stats["forced"] += 1
        self._wake.set()
        return True

    # ── leader election (shared LeaderLock) ──

    def _claim_leader(self) -> bool:
        try:
            if self._leader is None:
                from null_memory.memory.leader import LeaderLock
                self._leader = LeaderLock(
                    self.memory.db.db_path, POKE_LEADER_KEY,
                    self.instance_id)
            ttl = max(60, int(self.interval * 3))
            return self._leader.claim_or_refresh(ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[poke] leader check failed: %s", exc)
            return False

    # ── loop ──

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle_once()
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                logger.exception("[poke] cycle crashed: %s", exc)
            self._wake.wait(self.interval)
            self._wake.clear()
        logger.info("[poke] worker stopping instance=%s", self.instance_id)

    def cycle_once(self) -> dict | None:
        """One leader-gated cycle. Public so tests/CLI can drive it."""
        if not self._claim_leader():
            self.stats["skipped_not_leader"] += 1
            return None
        self.stats["cycles"] += 1
        report = poke_once(self.memory)
        self.last_report = report
        return report
