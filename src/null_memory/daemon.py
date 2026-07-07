"""Null daemon — Phase 7.1.

A long-running subprocess that runs Hypnos Live + Outreach evaluation
+ every personality manager's tick on a cadence, completely
independently of any Claude Code MCP session.

Architecture:
    DaemonRunner
        ├── HypnosLiveWorker (own thread, own 60s cadence — self-managing)
        ├── PokeWorker (own thread, N-minute poke cycle — fetch/ff-pull/
        │       replay/exchange-ingest; issue #20 Phase B)
        ├── DoorbellListener (UDP, contentless ping → PokeWorker.force())
        └── tick loop (cadence = NULL_DAEMON_CADENCE seconds, default 900)
                ├── OutreachEvaluator(memory).evaluate()
                ├── for entry in list_personalities():
                │       manager = load_manager(entry.name, mem)
                │       _resolve(manager.tick())
                └── sleep(cadence)

Reuses every existing safety primitive — leader election, cooldowns,
budgets, per-kind caps, pause flags. Doesn't reinvent any of them.

Scheduling: this is the OUTER-LOOP engine (15-minute ticks: outreach +
personality managers). Continuous 60s maintenance is the embedded
HypnosLiveWorker (which claims the shared ``hypnos_live_leader`` key, so
it coexists safely with MCP-embedded live workers); batch maintenance is
hypnos.Hypnos. Single-leader via meta key ``null_daemon_leader`` through
the shared null_memory.memory.leader.LeaderLock implementation.

Pause via meta key ``null_daemon_pause`` (separate from Hypnos's so
the user can independently pause maintenance vs the outer loop).

CLI surface lives in cli.py under ``null daemon ...``.
"""

from __future__ import annotations

import inspect
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from null_memory.memory.leader import LeaderLock

logger = logging.getLogger(__name__)


DEFAULT_CADENCE_SECONDS = 900.0   # 15 minutes — conservative for API limits
MIN_CADENCE_SECONDS = 30.0        # runaway guard
LEADER_TTL_SECONDS = 1800         # 30 min — daemon leadership is sticky
PAUSE_KEY = "null_daemon_pause"
LEADER_KEY = "null_daemon_leader"
LAST_TICK_KEY = "null_daemon_last_tick"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(value):
    """Run an awaitable to completion, or return the value as-is.
    Mirrors cli._run_maybe_async — duplicated here so the daemon module
    has zero CLI dependency and can be imported by tests cleanly."""
    if inspect.isawaitable(value):
        import asyncio
        return asyncio.run(value)
    return value


@dataclass
class TickReport:
    """One daemon tick's summary."""
    started_at: str = ""
    finished_at: str = ""
    skipped_paused: bool = False
    skipped_not_leader: bool = False
    outreach_fired: int = 0
    outreach_errors: int = 0
    managers_ticked: int = 0
    manager_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class DaemonRunner:
    """Outer loop coordinator. Composition of existing primitives —
    nothing here re-implements work that Hypnos / Outreach / personality
    loader already do."""

    def __init__(self, memory: Any, cadence_seconds: float | None = None):
        self.memory = memory
        env_cadence = os.environ.get("NULL_DAEMON_CADENCE", "").strip()
        if cadence_seconds is not None:
            self.cadence = max(MIN_CADENCE_SECONDS, float(cadence_seconds))
        elif env_cadence:
            try:
                self.cadence = max(MIN_CADENCE_SECONDS, float(env_cadence))
            except ValueError:
                self.cadence = DEFAULT_CADENCE_SECONDS
        else:
            self.cadence = DEFAULT_CADENCE_SECONDS

        self.instance_id = f"{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_leader = False
        self._hypnos_worker = None
        self._poke_worker = None
        self._doorbell = None
        self._leader: LeaderLock | None = None
        self._stats = {
            "ticks": 0,
            "outreach_fired_total": 0,
            "manager_ticks_total": 0,
            "errors": 0,
            "skipped_paused": 0,
            "skipped_not_leader": 0,
        }
        self._last_report: TickReport | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        # HypnosLive genuinely requires the unified store — but the poke
        # loop, doorbell, and tick loop do not. A worker seat runs a
        # per-personality store by design (ORG_TOPOLOGY), and gating the
        # whole daemon on `unified` silently disabled the entire Phase B
        # receive path (no poke, no doorbell) on every worker seat.
        # Degrade per-component instead of refusing to start.
        if getattr(self.memory.db, "unified", False):
            # Spin up Hypnos Live as a sub-component so its 60s cadence
            # runs in parallel with the daemon's outer loop.
            try:
                from null_memory.hypnos_live import HypnosLiveWorker
                self._hypnos_worker = HypnosLiveWorker(self.memory)
                self._hypnos_worker.start()
            except Exception as e:
                logger.warning("[daemon] HypnosLive failed to start: %s", e)
        else:
            logger.info("[daemon] per-seat store: HypnosLive skipped "
                        "(requires unified db); poke/doorbell/ticks run")

        # Poke loop (issue #20 Phase B): periodic fetch/ff-pull/replay +
        # exchange ingestion. Its own thread so the N-minute poke cadence
        # is independent of the 15-minute outer loop.
        try:
            from null_memory.poke import PokeWorker
            self._poke_worker = PokeWorker(self.memory)
            self._poke_worker.start()
        except Exception as e:
            logger.warning("[daemon] PokeWorker failed to start: %s", e)

        # UDP doorbell: any datagram (content ignored — contentless by
        # design) forces an immediate poke cycle, debounced inside
        # PokeWorker.force(). Bind failure is non-fatal — the poll above
        # remains the delivery guarantee.
        if self._poke_worker is not None:
            try:
                from null_memory.doorbell import (
                    DEFAULT_DOORBELL_BIND,
                    DEFAULT_DOORBELL_PORT,
                    DoorbellListener,
                )
                from null_memory.events import load_store_config
                store_dir = os.path.dirname(self.memory.db.db_path)
                cfg = load_store_config(store_dir)
                if cfg.get("doorbell_enabled", True):
                    self._doorbell = DoorbellListener(
                        on_ring=self._poke_worker.force,
                        bind=str(cfg.get("doorbell_bind",
                                         DEFAULT_DOORBELL_BIND)),
                        port=int(cfg.get("doorbell_port",
                                         DEFAULT_DOORBELL_PORT)),
                    )
                    if not self._doorbell.start():
                        self._doorbell = None
            except Exception as e:
                logger.warning("[daemon] doorbell failed to start: %s", e)
                self._doorbell = None

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"null-daemon-{self.instance_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[daemon] started instance=%s cadence=%.0fs",
            self.instance_id, self.cadence,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._hypnos_worker is not None:
            try:
                self._hypnos_worker.stop(timeout=timeout)
            except Exception:
                pass
        if self._doorbell is not None:
            try:
                self._doorbell.stop(timeout=timeout)
            except Exception:
                pass
        if self._poke_worker is not None:
            try:
                self._poke_worker.stop(timeout=timeout)
            except Exception:
                pass

    def status(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "is_leader": self._is_leader,
            "alive": bool(self._thread and self._thread.is_alive()),
            "cadence_seconds": self.cadence,
            "stats": dict(self._stats),
            "last_report": self._last_report.__dict__ if self._last_report else None,
            "poke": (dict(self._poke_worker.stats)
                     if self._poke_worker is not None else None),
            "doorbell_port": (self._doorbell.port
                              if self._doorbell is not None else None),
        }

    # ── Leader election + pause (shared LeaderLock) ───────────────────

    def _leader_lock(self) -> LeaderLock:
        if self._leader is None:
            self._leader = LeaderLock(
                self.memory.db.db_path, LEADER_KEY, self.instance_id,
            )
        return self._leader

    def _claim_conn(self) -> sqlite3.Connection:
        """Dedicated connection so leader claims (and meta reads/writes)
        don't race the main thread's in-flight writes on the shared
        memory.db.conn. Owned by the shared LeaderLock."""
        return self._leader_lock().conn

    def _claim_or_refresh_leader(self) -> bool:
        try:
            claimed = self._leader_lock().claim_or_refresh(LEADER_TTL_SECONDS)
            self._is_leader = claimed
            return claimed
        except Exception as e:
            logger.warning("[daemon] leader check failed: %s", e)
            self._is_leader = False
            return False

    def _is_paused(self) -> bool:
        try:
            row = self._claim_conn().execute(
                f"SELECT value FROM meta WHERE key='{PAUSE_KEY}'"
            ).fetchone()
        except Exception:
            return False
        return bool(row) and (row[0] or "0") == "1"

    # ── Run loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as e:  # noqa: BLE001
                self._stats["errors"] += 1
                logger.exception("[daemon] tick crashed: %s", e)
            if self._stop.wait(self.cadence):
                break
        logger.info("[daemon] stopping instance=%s", self.instance_id)

    def tick_once(self) -> TickReport:
        """Single outer-loop iteration. Public so the CLI can run it
        manually for testing without spinning up the background thread."""
        report = TickReport(started_at=_now_iso())
        self._stats["ticks"] += 1

        if self._is_paused():
            self._stats["skipped_paused"] += 1
            report.skipped_paused = True
            report.finished_at = _now_iso()
            self._last_report = report
            return report

        if not self._claim_or_refresh_leader():
            self._stats["skipped_not_leader"] += 1
            report.skipped_not_leader = True
            report.finished_at = _now_iso()
            self._last_report = report
            return report

        # Outreach evaluator — fires through Phase 4 channels with all
        # the existing safety nets (cooldown, budget, per-kind caps).
        try:
            from null_memory.outreach import OutreachEvaluator
            ev = OutreachEvaluator(self.memory)
            res = ev.evaluate()
            report.outreach_fired = res.fired
            report.outreach_errors = res.errors
            self._stats["outreach_fired_total"] += res.fired
        except Exception as e:
            report.notes.append(f"outreach: {e}")
            self._stats["errors"] += 1

        # Per-personality tick — load each, call .tick(), tolerate
        # individual failures (one bad manager shouldn't kill the loop).
        try:
            from null_memory.personality import (
                list_personalities, load_manager,
            )
            entries = list_personalities()
            for entry in entries:
                try:
                    mgr = load_manager(entry.name, self.memory)
                    _resolve(mgr.tick())
                    report.managers_ticked += 1
                    self._stats["manager_ticks_total"] += 1
                except Exception as e:
                    msg = f"{entry.name}: {type(e).__name__}: {e}"
                    report.manager_errors.append(msg)
                    logger.warning("[daemon] manager tick failed — %s", msg)
        except Exception as e:
            report.notes.append(f"personality discovery: {e}")
            self._stats["errors"] += 1

        # Persist last-tick timestamp for `null daemon status` consumers
        try:
            self._claim_conn().execute(
                f"INSERT OR REPLACE INTO meta(key,value) VALUES ('{LAST_TICK_KEY}', ?)",
                (_now_iso(),),
            )
        except Exception:
            pass

        report.finished_at = _now_iso()
        self._last_report = report
        return report


def daemon_log_path() -> Path:
    base = os.environ.get("NULL_DIR") or os.path.expanduser("~/.null")
    return Path(base) / "daemon.log"


def configure_logging() -> None:
    """Attach a rotating-friendly file handler so `null daemon logs` has
    something to tail. Idempotent — safe to call repeatedly (e.g., from
    both `daemon run` and `daemon tick` paths)."""
    log_path = daemon_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(h, "_null_daemon", False) for h in root.handlers):
        return
    handler = logging.FileHandler(str(log_path), encoding="utf-8")
    handler._null_daemon = True  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s"
    ))
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
