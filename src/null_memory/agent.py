"""Core agent memory manager — the brain of Null.

v0.5.0: SQLite-backed storage with FTS5 search and trigram fuzzy matching.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import logging

from null_memory.db import NullDB, migrate_jsonl_to_sqlite
from null_memory.session import Session, SessionManager

logger = logging.getLogger(__name__)

# Process-wide sync coordination (P0-5/N8): one lock guards the dirty-flag
# state machine, the other serializes the actual git commit+push so two
# flushes (timer + lifecycle, or two AgentMemory instances in one process)
# can never run git concurrently.
_SYNC_STATE_LOCK = threading.Lock()
_SYNC_GIT_LOCK = threading.Lock()


_AGENT_NAMES = [
    "Sage", "Nova", "Forge", "Prism",
    "Echo", "Rune", "Drift", "Ember", "Arc",
    "Onyx", "Zenith", "Flux", "Cipher", "Helix",
]  # Note: "Atlas" is reserved — set via `null name` (CLI) if desired

# ── Source Authority Tiers ──
# Higher number = more authoritative. Used to resolve conflicts on dedup.
# - witnessed (4): Atlas directly observed this happen
# - explicit  (3): Pete explicitly stated this as fact
# - observation (2): Atlas inferred from context (default for observe())
# - told      (1): mentioned in passing, uncertain
SOURCE_TIERS: dict[str, int] = {
    "witnessed": 4,
    "explicit": 3,
    "observation": 2,
    "told": 1,
}

# Tokenization / thesaurus helpers live with the recall pipeline now.
# Re-exported here because tests and historical callers import them from
# null_memory.agent.
from null_memory.memory.recall import (  # noqa: E402
    FORGET_NEAR_TIE_RATIO,
    RecallMixin,
    WORD_EXPANSION,
    _REVERSE_EXPANSION,
    _STOP_WORDS,
    _expand_tokens,
    _strip_punctuation,
)


class ForgetAmbiguousError(ValueError):
    """Fuzzy forget refused: the top two matches are a near-tie.

    Raised instead of guessing when the runner-up's fused recall score is
    within FORGET_NEAR_TIE_RATIO of the top match — the signature of
    near-duplicate facts (the incident class: 90% text overlap, wrong
    fact soft-deleted). ``candidates`` holds the tied facts (dicts with
    at least ``id`` and ``fact``) so callers can retry with an exact id.
    """

    def __init__(self, candidates: list[dict]):
        self.candidates = candidates
        ids = ", ".join(c.get("id", "?")[:12] for c in candidates)
        super().__init__(
            f"forget refused: near-tie between candidates ({ids}); "
            "retry with an exact fact id"
        )
from null_memory.memory.briefing_render import BriefingRenderMixin  # noqa: E402
from null_memory.memory.evaluation import EvaluationMixin  # noqa: E402
from null_memory.memory.maintenance import MaintenanceMixin  # noqa: E402
from null_memory.memory.probes import ProbesMixin  # noqa: E402
from null_memory.memory.viz import VizMixin  # noqa: E402


@dataclass
class AgentMemory(RecallMixin, VizMixin, ProbesMixin, EvaluationMixin,
                  BriefingRenderMixin, MaintenanceMixin):
    """Central memory manager for a Null agent instance.

    Uses SQLite with FTS5 for storage, search, and fuzzy matching.
    Identity and session records remain as JSON files for human editability.
    """

    agent_dir: str
    personality: str = "atlas"
    identity: dict = field(default_factory=dict)
    projects: dict[str, dict] = field(default_factory=dict)
    _db: Any = field(default=None, repr=False)
    _config: dict = field(default_factory=dict, repr=False)
    _turn_count: int = field(default=0, repr=False)
    _token_budget_used: int = field(default=0, repr=False)
    _session_manager: Any = field(default=None, repr=False)
    _current_session: Any = field(default=None, repr=False)
    _prior_crash: Any = field(default=None, repr=False)  # Session | None
    _session_git_thread: Any = field(default=None, repr=False)  # daemon thread running deferred session-start git
    _session_git_atexit_registered: bool = field(default=False, repr=False)
    _session_recalled_ids: list = field(default_factory=list, repr=False)  # Track recalled fact IDs for relationship linking
    _insight_topics_surfaced: list = field(default_factory=list, repr=False)  # Dedup insight topics per session
    _embeddings: Any = field(default=None, repr=False)  # EmbeddingEngine | None
    # Event-sourced sync Phase A (issue #20): lazily-built EventEmitter.
    # Only constructed when NULL_EVENT_LOG=1 — zero behavior change unset.
    _events: Any = field(default=None, repr=False)
    # Phase 3b — Continuous identity: rolling buffer of per-turn signatures
    # (numpy float32 arrays) + a one-shot warning flag consumed by handlers.
    _turn_signatures: list = field(default_factory=list, repr=False)
    _mid_session_drift_warning: Any = field(default=None, repr=False)
    _mid_session_drift_surfaced: bool = field(default=False, repr=False)
    # P0-5 (N8) — debounced remote sync: per-write callers set a dirty flag;
    # an actual commit+push happens on the debounce timer or on lifecycle
    # boundaries (checkpoint / debrief / close), never per fact write.
    _sync_dirty: bool = field(default=False, repr=False)
    _sync_timer: Any = field(default=None, repr=False)  # threading.Timer | None
    _sync_triggers: list = field(default_factory=list, repr=False)
    _sync_atexit_registered: bool = field(default=False, repr=False)
    # Instance presence registry: this process's row in the shared
    # `instances` table. Registered by load(); heartbeat refreshed (with an
    # in-process throttle) by touch_instance() on existing periodic paths.
    _instance_id: str = field(default="", repr=False)
    _instance_heartbeat_monotonic: Any = field(default=None, repr=False)
    # Live immediate-flush threads (named "null-sync-flush"), tracked so
    # callers (and test teardown) can deterministically join them instead
    # of letting a late flush bleed into unrelated work (issue #5).
    _sync_flush_threads: list = field(default_factory=list, repr=False)

    # Default configuration values
    _DEFAULT_CONFIG = {
        "age_decay_rate": 0.003,
        "gc_archive_threshold": 0.1,
        "max_facts": 5000,
        "consolidation_jaccard_low": 0.40,
        "consolidation_jaccard_high": 0.65,
        "dedup_jaccard_threshold": 0.65,
        "consolidation_min_words": 5,
        "consolidation_strengthen_threshold": 5,
        "consolidation_fade_days": 30,
        # Hypnos sleep cycle thresholds
        "hypnos_decay_archive_threshold": 0.05,
        "hypnos_decay_min_age_days": 60,
        "hypnos_promote_access_threshold": 10,
        "hypnos_promote_verify_threshold": 2,
        "hypnos_promote_decision_ref_threshold": 2,
        "hypnos_demote_idle_days": 60,
        "hypnos_decision_impact_boost": 0.2,
        "hypnos_mistake_impact_boost": 0.3,
        "hypnos_cold_storage_age_days": 90,
        "hypnos_cold_storage_confidence_threshold": 0.3,
        # Hypnos Stage 5: Knowledge synthesis (LLM-powered)
        "hypnos_synthesis_enabled": False,
        "hypnos_synthesis_max_clusters": 5,
        "hypnos_synthesis_min_cluster_size": 3,
    }

    @property
    def db(self) -> NullDB:
        if self._db is None:
            # Only use unified.db when this AgentMemory lives inside the
            # configured NULL_DIR. Test fixtures using tmp_path must fall back
            # to per-agent memory.db to preserve isolation.
            null_home = os.path.realpath(
                os.environ.get("NULL_DIR", os.path.expanduser("~/.null"))
            )
            try:
                agent_real = os.path.realpath(self.agent_dir)
            except OSError:
                agent_real = self.agent_dir
            inside_null_home = (
                agent_real == null_home
                or agent_real.startswith(null_home + os.sep)
            )
            unified_path: str | None = None
            if inside_null_home:
                unified_path = os.environ.get(
                    "NULL_UNIFIED_DB", os.path.join(null_home, "unified.db")
                )
            self._db = NullDB(
                self.agent_dir,
                unified_path=unified_path,
                personality=self.personality,
            )
            self._db.initialize()
        return self._db

    def _note_embed_failure(self, context: str, exc: Exception) -> None:
        """Count a swallowed embedding failure (P0-6).

        Embedding errors are deliberately non-fatal, but silently eating
        them meant semantic recall could degrade with no signal anywhere.
        Every swallow site calls this; status() and doctor surface the
        counter."""
        try:
            self.db.bump_meta_counter("embed_failures")
            self.db.set_meta(
                "embed_failures_last",
                f"{datetime.now(timezone.utc).isoformat()} | {context} | {exc}",
            )
            self.db.conn.commit()
        except Exception:
            pass  # The health counter must never break the primary action
        logger.debug("embedding failure in %s: %s", context, exc)

    @property
    def embeddings(self):
        """Lazy-load embedding engine. Returns None if fastembed not installed."""
        if self._embeddings is None:
            try:
                from null_memory.embeddings import EmbeddingEngine
                # Pass the NullDB itself (not a snapshot of .conn): NullDB
                # connections are thread-local, and the engine must resolve
                # the current thread's connection on every access.
                engine = EmbeddingEngine(self.db)
                if engine.available:
                    self._embeddings = engine
                else:
                    self._embeddings = False  # Sentinel: checked but unavailable
            except Exception:
                self._embeddings = False
        return self._embeddings if self._embeddings is not False else None

    @property
    def events(self):
        """Event-log emitter (event-sourced sync Phase A, issue #20).

        Returns None unless NULL_EVENT_LOG=1 — checked per access so the
        gate works regardless of construction order. The emitter is rooted
        at the store directory (where the live db file lives), so unified
        stores log to ``~/.null/events/`` and per-agent test stores log to
        ``<agent_dir>/events/``."""
        from null_memory.events import event_log_enabled, EventEmitter
        if not event_log_enabled():
            return None
        if self._events is None:
            store_dir = os.path.dirname(self.db.db_path)
            self._events = EventEmitter(
                store_dir, self.db, personality=self.personality)
        return self._events

    def _emit_store_event(self, kind: str, entity_id: Any, data: dict,
                          scope: str = "org") -> None:
        """Dual-write half of Phase A: append one event for a knowledge
        mutation that just committed to the db. Best-effort — the db is
        authoritative this phase, so an emit failure warns and counts
        (meta key ``event_log_failures``) but never breaks the write."""
        if getattr(self, "_events_suppressed", False):
            # Set by self-test paths (calibration system probes) whose
            # temp rows are hard-deleted afterwards — ephemeral scaffolding
            # must not enter the permanent log.
            return
        emitter = self.events
        if emitter is None:
            return
        try:
            emitter.emit(kind, entity_id, data, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("event log emit failed (%s %s): %s",
                           kind, entity_id, exc)
            try:
                self.db.bump_meta_counter("event_log_failures")
                self.db.conn.commit()
            except Exception:
                pass  # The health counter must never break the primary action

    @property
    def config(self) -> dict:
        if not self._config:
            self._config = dict(self._DEFAULT_CONFIG)
            config_path = os.path.join(self.agent_dir, "config.json")
            if os.path.isfile(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        user_config = json.load(f)
                    self._config.update(user_config)
                except (json.JSONDecodeError, OSError):
                    pass
        return self._config

    # ── Compatibility properties ──
    # These expose data from SQLite as lists for backward compatibility
    # with code that iterates over self.knowledge, self.decisions, etc.

    @property
    def knowledge(self) -> list[dict]:
        return self.db.get_active_facts()

    @knowledge.setter
    def knowledge(self, value: list[dict]) -> None:
        # Used by import_from — no-op since we write directly to SQLite
        pass

    @property
    def decisions(self) -> list[dict]:
        return self.db.get_decisions()

    @decisions.setter
    def decisions(self, value: list[dict]) -> None:
        pass

    @property
    def mistakes(self) -> list[dict]:
        return self.db.get_mistakes()

    @mistakes.setter
    def mistakes(self, value: list[dict]) -> None:
        pass

    @property
    def reflections(self) -> list[dict]:
        return self.db.get_reflections()

    @reflections.setter
    def reflections(self, value: list[dict]) -> None:
        pass

    @property
    def name(self) -> str:
        return self.identity.get("name", "Agent")

    @property
    def token_budget(self) -> int:
        budget_str = os.environ.get("NULL_TOKEN_BUDGET", "1000")
        try:
            return int(budget_str)
        except ValueError:
            return 1000

    @property
    def token_budget_remaining(self) -> int:
        return max(0, self.token_budget - self._token_budget_used)

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _use_budget(self, text: str) -> None:
        self._token_budget_used += self._estimate_tokens(text)

    # ── Loading ──

    @classmethod
    def load(cls, agent_dir: str | None = None, personality: str = "atlas",
             transport: str | None = None) -> AgentMemory:
        """Load agent memory from disk, or create empty if none exists.

        Args:
            agent_dir: Explicit directory path. If None, resolves from personality name.
            personality: Personality name. "atlas" uses ~/.null/atlas/,
                        others use ~/.null/personalities/{name}/.
                        Falls back to ~/.null/ if atlas/ subdir doesn't exist (pre-migration).
            transport: Presence-registry transport tag ('mcp' | 'cli').
                        Defaults to NULL_TRANSPORT env, then 'cli'.
        """
        if agent_dir is None:
            base = os.environ.get("NULL_DIR", os.path.join(os.path.expanduser("~"), ".null"))
            if personality == "atlas":
                atlas_dir = os.path.join(base, "atlas")
                # Fall back to flat layout if not yet migrated
                if os.path.isdir(atlas_dir):
                    agent_dir = atlas_dir
                else:
                    agent_dir = base
            else:
                agent_dir = os.path.join(base, "personalities", personality)

        os.makedirs(agent_dir, exist_ok=True)

        mem = cls(agent_dir=agent_dir, personality=personality)

        # Auto-migrate JSONL to SQLite if needed (legacy per-personality DBs only;
        # unified mode owns its own schema).
        if not mem.db.unified and mem.db.needs_migration:
            migrate_jsonl_to_sqlite(agent_dir, personality=personality)
            # Reinitialize DB connection after migration — as THIS
            # personality, not the 'atlas' default (init-path bleed audit).
            mem._db = NullDB(agent_dir, personality=personality)
            mem._db.initialize()

        # Load identity (stays as JSON file — human-editable)
        identity_path = os.path.join(agent_dir, "identity.json")
        if os.path.isfile(identity_path):
            with open(identity_path, "r", encoding="utf-8") as f:
                mem.identity = json.load(f)
            # Classifier identity entities live in identity.json under
            # "identity_terms" (deployment-specific, never shipped in the
            # package). Absent terms mean generic classification only.
        else:
            # First run — generate identity
            mem.identity = {
                "version": "1.0",
                "name": random.choice(_AGENT_NAMES),
                "working_style": {},
                "user_preferences": {},
                "capabilities": [],
                "anti_patterns": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        # Load project contexts (stay as JSON files)
        projects_dir = os.path.join(agent_dir, "projects")
        if os.path.isdir(projects_dir):
            for fname in os.listdir(projects_dir):
                if fname.endswith(".json"):
                    project_name = fname[:-5]
                    with open(os.path.join(projects_dir, fname), "r", encoding="utf-8") as f:
                        try:
                            mem.projects[project_name] = json.load(f)
                        except json.JSONDecodeError:
                            pass

        # Initialize session manager and check for crashes. Carries the
        # store's personality so session records are attributed to it,
        # never to the 'atlas' dataclass default (init-path bleed audit).
        mem._session_manager = SessionManager(
            agent_dir, personality=personality)
        crashed = mem._session_manager.detect_crash()
        if crashed is not None:
            mem._prior_crash = crashed

        # Instance presence: register this process on the shared store.
        # load() is the layer both MCP (handlers.memory) and CLI traverse
        # exactly once per process — best-effort, never breaks loading.
        mem._register_instance(transport)

        return mem

    # ── Saving ──

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize a name for safe use as a filename. Prevents path traversal."""
        import re
        safe = re.sub(r'[/\\\.]+', '_', name)
        safe = re.sub(r'[^a-zA-Z0-9_\-]', '', safe)
        return safe or "unnamed"

    def _ensure_dir(self) -> None:
        os.makedirs(self.agent_dir, exist_ok=True)
        os.makedirs(os.path.join(self.agent_dir, "projects"), exist_ok=True)
        os.makedirs(os.path.join(self.agent_dir, "sessions"), exist_ok=True)

    # Debounce window for remote sync. Per-write callers only mark dirty;
    # one commit+push covers everything written inside the window.
    SYNC_DEBOUNCE_SECONDS = 120.0

    def _sync_to_remote(self, trigger: str = "write",
                        immediate: bool = False) -> None:
        """Mark memory dirty for remote sync (debounced).

        Instead of one git commit+push thread per fact write (the old
        behavior — dozens of concurrent pushes per chatty session), this
        sets a dirty flag and arms a debounce timer. Lifecycle boundaries
        (checkpoint / debrief / close / hypnos) pass ``immediate=True`` to
        flush right away. A process-wide lock serializes the actual git
        work so two flushes can never race each other.
        """
        import threading

        with _SYNC_STATE_LOCK:
            self._sync_dirty = True
            if trigger not in self._sync_triggers:
                self._sync_triggers.append(trigger)

            # The old fire-and-forget threads lost commits on interpreter
            # exit; flush any still-pending debounce window at shutdown.
            if not self._sync_atexit_registered:
                import atexit
                atexit.register(self._flush_sync_now)
                self._sync_atexit_registered = True

            if immediate:
                if self._sync_timer is not None:
                    self._sync_timer.cancel()
                    self._sync_timer = None
                t = threading.Thread(target=self._flush_sync_now, daemon=True,
                                     name="null-sync-flush")
                # Track (and prune) live flush threads so _join_sync_threads
                # can quiesce sync deterministically.
                self._sync_flush_threads = [
                    th for th in self._sync_flush_threads if th.is_alive()]
                self._sync_flush_threads.append(t)
                t.start()
                return

            if self._sync_timer is None:
                debounce = float(os.environ.get(
                    "NULL_SYNC_DEBOUNCE", self.SYNC_DEBOUNCE_SECONDS))
                timer = threading.Timer(debounce, self._flush_sync_now)
                timer.daemon = True
                timer.start()
                self._sync_timer = timer

    def _flush_sync_now(self) -> None:
        """Flush a pending remote sync: one commit+push for all dirty writes.

        Serialized by the process-wide _SYNC_GIT_LOCK; clears the dirty
        flag and timer first so writes landing during the push arm a new
        debounce cycle instead of being silently absorbed."""
        with _SYNC_STATE_LOCK:
            if not self._sync_dirty:
                return
            self._sync_dirty = False
            triggers = self._sync_triggers
            self._sync_triggers = []
            if self._sync_timer is not None:
                self._sync_timer.cancel()
                self._sync_timer = None

        trigger = "+".join(triggers) if triggers else "write"
        with _SYNC_GIT_LOCK:
            try:
                from null_memory.session import MemoryRepo
                store = MemoryRepo(self.agent_dir)
                msg = f"null: {trigger} [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"
                # Synchronous commit+push (not commit_and_push, which spawns
                # its own fire-and-forget thread and would escape the lock).
                store.commit(msg)
                store.push()
            except Exception as e:
                # Log sync failures to file instead of silently dropping them
                try:
                    log_path = os.path.join(self.agent_dir, "sync_errors.log")
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"{datetime.now(timezone.utc).isoformat()} | {trigger} | {e}\n")
                except OSError:
                    pass

    # ── Instance presence ──
    # The unifying primitive for Atlas fragmentation: multiple live
    # processes (MCP servers, CLI invocations, the daemon) share one store
    # with no cross-awareness. Each registers a row in `instances` at
    # load(); the long-lived MCP server refreshes last_heartbeat by
    # piggybacking touch_instance() on the per-tool-call session touch
    # (handlers._ensure_session) — the one path every MCP tool call
    # traverses regardless of HypnosLive being enabled. CLI processes are
    # seconds-long, so registration alone covers them. No new timer
    # threads; liveness honestly tracks actual activity.

    # Minimum seconds between heartbeat writes from this process. Keeps
    # the per-tool-call piggyback at ≤1 write/min.
    INSTANCE_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0

    def _register_instance(self, transport: str | None = None) -> None:
        """Register this process in the shared instances table.

        Best-effort: presence is advisory and must never break load().
        Transport resolution: explicit arg > NULL_TRANSPORT env > 'cli'.
        """
        try:
            from null_memory.migrate_v3 import UNIFIED_SCHEMA_VERSION
            resolved = (
                transport
                or os.environ.get("NULL_TRANSPORT", "").strip().lower()
                or "cli"
            )
            self._instance_id = str(uuid.uuid4())
            self.db.register_instance(
                self._instance_id,
                hostname=socket.gethostname(),
                pid=os.getpid(),
                personality=self.personality,
                transport=resolved,
                project=None,
                schema_version_seen=UNIFIED_SCHEMA_VERSION,
            )
            self._instance_heartbeat_monotonic = time.monotonic()
        except Exception:
            logger.debug("instance registration failed", exc_info=True)

    def touch_instance(self, project: str | None = None,
                       force: bool = False) -> None:
        """Refresh this instance's presence heartbeat (throttled).

        Called from existing periodic paths — never from its own timer.
        Throttle is in-process (monotonic clock) so the piggyback hosts
        can call this on every tool call at negligible cost. Re-registers
        if the row vanished (e.g. GC'd by another instance after a long
        sleep)."""
        if not self._instance_id:
            return
        now = time.monotonic()
        last = self._instance_heartbeat_monotonic
        if (not force and last is not None
                and now - last < self.INSTANCE_HEARTBEAT_MIN_INTERVAL_SECONDS):
            return
        self._instance_heartbeat_monotonic = now
        try:
            if not self.db.heartbeat_instance(self._instance_id,
                                              project=project):
                self._register_instance()
        except Exception:
            logger.debug("instance heartbeat failed", exc_info=True)

    def instances_line(self) -> str | None:
        """One status line describing live instances on this store, or
        None when presence data is unavailable. Shared by status() and the
        CLI status renderer."""
        try:
            live = self.db.get_live_instances()
        except Exception:
            return None
        if not live:
            return None
        now = datetime.now(timezone.utc)
        parts = []
        for inst in live:
            try:
                hb = datetime.fromisoformat(inst.get("last_heartbeat"))
                age_m = max(0, int((now - hb).total_seconds() // 60))
                age = f"{age_m}m"
            except (TypeError, ValueError):
                age = "?"
            marker = "*" if inst.get("instance_id") == self._instance_id else ""
            parts.append(
                f"{inst.get('hostname') or '?'}/{inst.get('pid')}{marker} "
                f"{inst.get('personality') or '?'} "
                f"({inst.get('transport') or '?'}, {age})"
            )
        return f"Instances: {len(live)} live — " + "; ".join(parts)

    def multi_instance_warning(self) -> str | None:
        """One-line fragmentation warning, or None unless >1 instance of
        THIS personality is live on this store. The briefing's only
        presence surface — a single live instance (the normal case) stays
        silent, and on a unified store other live personalities don't
        count as fragments of this one."""
        try:
            live = [
                inst for inst in self.db.get_live_instances()
                if (inst.get("personality") or "atlas") == self.personality
            ]
        except Exception:
            return None
        if len(live) < 2:
            return None
        who = ", ".join(
            f"{inst.get('hostname') or '?'}({inst.get('transport') or '?'})"
            for inst in live
        )
        name = (self.personality or "atlas").capitalize()
        return f"⚠ {len(live)} {name} instances live on this store — {who}"

    def _join_sync_threads(self, timeout: float = 5.0) -> None:
        """Deterministically quiesce background sync for this instance.

        Cancels (and joins) the pending debounce timer and joins every
        immediate-flush thread plus the deferred session-start git thread.
        After it returns no background sync work spawned by this instance
        is still running. Used by test teardown (issue #5: a late flush
        thread from one test landed commits inside a later test's
        assertions) and safe for any caller that needs sync settled.
        """
        with _SYNC_STATE_LOCK:
            timer = self._sync_timer
            self._sync_timer = None
            threads = [t for t in self._sync_flush_threads if t.is_alive()]
            self._sync_flush_threads = []
        if timer is not None:
            timer.cancel()
            timer.join(timeout)
        for t in threads:
            t.join(timeout)
        self._join_session_git_thread(timeout)

    # ── Identity ──

    def save_identity(self) -> None:
        self._ensure_dir()
        self.identity["updated_at"] = datetime.now(timezone.utc).isoformat()
        path = os.path.join(self.agent_dir, "identity.json")
        self._atomic_write_json(path, self.identity)

    def _atomic_write_json(self, path: str, data: dict) -> None:
        """Atomically rewrite a JSON file via unique tmp + os.replace."""
        self._ensure_dir()
        fd, tmp_path = tempfile.mkstemp(dir=self.agent_dir, suffix=".tmp",
                                        prefix=".null_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── Knowledge ──

    @staticmethod
    def _get_ts(entry: dict) -> str:
        """Get the creation timestamp from an entry, handling both old and new schema."""
        return entry.get("created_at", entry.get("ts", ""))

    def _parse_since(self, since: str) -> datetime | None:
        """Parse a 'since' parameter into a datetime.

        Supports:
        - ISO8601 timestamps
        - "last_session" — start of last completed session
        - "yesterday" — yesterday 00:00 UTC
        - "today" — today 00:00 UTC
        - "this_week" — Monday 00:00 of current week
        - "this_month" — 1st of current month 00:00 UTC
        - "Nd" — N days ago (e.g. "7d", "30d")
        - "Nw" — N weeks ago (e.g. "2w")
        - "Nh" — N hours ago (e.g. "6h", "24h")
        """
        if not since:
            return None

        now = datetime.now(timezone.utc)

        # Relative: "Nd" (days ago)
        if since.endswith("d") and since[:-1].isdigit():
            days = int(since[:-1])
            return now - timedelta(days=days)

        # Relative: "Nw" (weeks ago)
        if since.endswith("w") and since[:-1].isdigit():
            weeks = int(since[:-1])
            return now - timedelta(weeks=weeks)

        # Relative: "Nh" (hours ago)
        if since.endswith("h") and since[:-1].isdigit():
            hours = int(since[:-1])
            return now - timedelta(hours=hours)

        # "yesterday"
        if since == "yesterday":
            yesterday = now - timedelta(days=1)
            return yesterday.replace(hour=0, minute=0, second=0, microsecond=0)

        # "today"
        if since == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)

        # "last_session"
        if since == "last_session":
            if self._session_manager:
                last = self._session_manager.last_completed_session()
                if last:
                    try:
                        return datetime.fromisoformat(last.started_at)
                    except ValueError:
                        pass
            return None

        # "this_week"
        if since == "this_week":
            days_since_monday = now.weekday()
            monday = now - timedelta(days=days_since_monday)
            return monday.replace(hour=0, minute=0, second=0, microsecond=0)

        # "this_month"
        if since == "this_month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # ISO8601
        try:
            dt = datetime.fromisoformat(since)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None

    @staticmethod
    def _content_hash(fact: str, project: str = "global") -> str:
        """Compute a 12-char hex content hash for dedup.

        Includes project in the hash so the same fact text in different
        projects gets different IDs.
        """
        normalized = f"{project.strip().lower()}:{fact.strip().lower()}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]

    def effective_confidence(self, entry: dict) -> float:
        """Compute dynamic confidence from base confidence + temporal signals.

        Emotional anchors (schema v13) bypass decay entirely — they represent
        load-bearing memories for Atlas's identity and should never fade.

        Factors (non-anchored facts):
        - base_confidence (stored value, or legacy "confidence" field)
        - age_decay: exp(-0.003 * age_days) — slower than old 0.005
        - access_boost: min(1.5, 1.0 + 0.05 * access_count)
        - verification_boost: 1.3 if verified recently, else 1.0
        - provenance_weight: lessons/debriefs > observed > reconstructed
        """
        # Anchored facts never decay.
        if entry.get("anchor_type"):
            return 1.0

        base = entry.get("base_confidence", entry.get("confidence", 0.5))

        now = datetime.now(timezone.utc)
        ts_str = self._get_ts(entry)

        # Age decay
        try:
            entry_time = datetime.fromisoformat(ts_str)
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - entry_time).total_seconds() / 86400)
            decay_rate = self.config.get("age_decay_rate", 0.003)
            age_factor = math.exp(-decay_rate * age_days)
        except (ValueError, TypeError):
            age_factor = 0.5

        # Access boost: frequently recalled facts resist decay
        access_count = entry.get("access_count", 0)
        access_boost = min(1.5, 1.0 + 0.05 * access_count)

        # Verification boost
        verification_boost = 1.0
        verified_at = entry.get("last_verified")
        if verified_at:
            try:
                v_time = datetime.fromisoformat(verified_at)
                if v_time.tzinfo is None:
                    v_time = v_time.replace(tzinfo=timezone.utc)
                v_age_days = max(0, (now - v_time).total_seconds() / 86400)
                verification_boost = max(1.0, 1.3 * math.exp(-0.01 * v_age_days))
            except (ValueError, TypeError):
                pass

        # Tier-aware decay adjustment
        tier = entry.get("tier", "contextual")
        tier_factors = {
            "core": 1.5,         # Identity-defining — maximum resistance
            "durable": 1.2,      # Resist decay — slow fade
            "contextual": 1.0,   # Normal decay
            "ephemeral": 0.7,    # Accelerated decay
        }
        tier_factor = tier_factors.get(tier, 1.0)

        # Provenance weight
        provenance_weights = {
            "lesson": 1.2, "debrief": 1.1, "explicit": 1.0,
            "observation": 0.9, "observed": 0.9,
            "reconstructed": 0.7, "consolidated": 1.1,
            "bootstrap": 1.0,
        }
        provenance = entry.get("provenance", entry.get("source", ""))
        provenance_factor = provenance_weights.get(provenance, 1.0)

        return min(1.0, base * age_factor * access_boost * verification_boost * provenance_factor * tier_factor)

    def _current_session_id(self) -> str | None:
        """Get the current session ID, if a session is active."""
        if self._current_session is not None:
            return self._current_session.session_id
        return None

    def learn(self, fact: str, confidence: float = 0.8,
              project: str = "global", source: str = "explicit",
              replaces: str | None = None,
              impact: float = 0.5,
              tier: str = "contextual") -> dict:
        """Store a fact in memory. Returns the entry.

        Deduplicates by content_hash. If a fact with the same hash exists,
        uses source authority tiers to decide whether to supersede.

        Args:
            replaces: content_hash or substring of fact to supersede.
            impact: Salience Chord — structured impact score 0.ABC (default 0.500).
        """
        now_ts = datetime.now(timezone.utc).isoformat()
        project = project.strip().lower()
        c_hash = self._content_hash(fact, project)

        # Check for existing fact with same hash (exact dedup).
        # BEGIN IMMEDIATE so the read + confidence/authority merge is
        # atomic — two concurrent learns of the same fact can't lose
        # each other's updates. The event (dual-write, issue #20) is
        # emitted AFTER the transaction commits — the log must only ever
        # describe committed truth.
        dedup_result: dict | None = None
        dedup_update: dict | None = None
        with self.db.write_transaction() as conn:
            existing = self.db.get_fact_by_id(c_hash)
            if existing and existing.get("project", "global") == project:
                # Compare source authority tiers
                new_tier = SOURCE_TIERS.get(source, 1)
                existing_tier = SOURCE_TIERS.get(existing.get("source", ""), 1)
                if new_tier > existing_tier:
                    # New fact has higher authority — upgrade the existing row
                    # in place. (Never supersede a row by its own hash: the
                    # follow-up INSERT OR IGNORE would no-op on the duplicate
                    # primary key, leaving a self-superseded tombstone that is
                    # invisible to every active query.)
                    new_conf = max(existing.get("confidence", 0.5), confidence)
                    new_impact = max(existing.get("impact", 0.5), impact)
                    conn.execute(
                        """UPDATE facts SET source = ?, confidence = ?,
                           base_confidence = ?, last_accessed = ?,
                           access_count = access_count + 1, impact = ?
                           WHERE id = ?""",
                        (source, new_conf, new_conf, now_ts, new_impact, c_hash),
                    )
                    dedup_update = {"source": source, "confidence": new_conf,
                                    "base_confidence": new_conf,
                                    "impact": new_impact}
                    dedup_result = dict(existing, source=source,
                                        confidence=new_conf,
                                        base_confidence=new_conf,
                                        impact=new_impact)
                else:
                    # Equal or lower authority — update confidence/access only
                    new_conf = max(existing.get("confidence", 0.5), confidence)
                    new_impact = max(existing.get("impact", 0.5), impact)
                    conn.execute(
                        """UPDATE facts SET confidence = ?, base_confidence = ?,
                           last_accessed = ?, access_count = access_count + 1,
                           impact = ? WHERE id = ?""",
                        (new_conf, new_conf, now_ts, new_impact, c_hash),
                    )
                    dedup_update = {"confidence": new_conf,
                                    "base_confidence": new_conf,
                                    "impact": new_impact}
                    dedup_result = dict(existing, confidence=new_conf,
                                        impact=new_impact)
        if dedup_result is not None:
            self._emit_store_event("fact.update", c_hash, dedup_update)
            return dedup_result

        # Semantic dedup: if embeddings available, check for near-duplicate
        # by meaning. SKIPPED when the caller passed `replaces`: an explicit
        # supersession request means the new text must land as its own fact
        # (a similar-enough revision would otherwise be merged into the very
        # fact it's meant to supersede, keeping the longer OLD text — the
        # update would silently vanish; hit by persona onboarding re-runs).
        emb = self.embeddings
        if emb is not None and existing is None and replaces is None:
            try:
                # Embedding compute + similarity search happen OUTSIDE the
                # write lock (model inference can take tens of ms; cold
                # start can take seconds). The merge itself re-reads the
                # candidate inside BEGIN IMMEDIATE so the read-modify-write
                # is atomic.
                new_vec = emb.embed(fact)
                # Search for semantically similar existing facts
                similar = emb.semantic_search(fact, limit=3)
                for sim_id, sim_score in similar:
                    if sim_score < 0.85:  # High threshold — only merge near-duplicates
                        break
                    with self.db.write_transaction() as conn:
                        sim_fact = self.db.get_fact_by_id(sim_id)
                        if sim_fact is None or sim_fact.get("forgotten") or sim_fact.get("superseded_by"):
                            continue
                        if sim_fact.get("project", "global") != project:
                            continue
                        # Found a semantic near-duplicate — merge into existing
                        new_conf = max(sim_fact.get("confidence", 0.5), confidence)
                        new_impact = max(sim_fact.get("impact", 0.5), impact)
                        # Keep the longer/more detailed fact text
                        keep_new = len(fact) > len(sim_fact.get("fact", ""))
                        if keep_new:
                            conn.execute(
                                """UPDATE facts SET fact = ?, confidence = ?, base_confidence = ?,
                                   last_accessed = ?, access_count = access_count + 1,
                                   impact = ? WHERE id = ?""",
                                (fact, new_conf, new_conf, now_ts, new_impact, sim_id),
                            )
                            # Update the embedding with the new text
                            emb.store_embedding(sim_id, new_vec, created_at=now_ts)
                        else:
                            conn.execute(
                                """UPDATE facts SET confidence = ?, base_confidence = ?,
                                   last_accessed = ?, access_count = access_count + 1,
                                   impact = ? WHERE id = ?""",
                                (new_conf, new_conf, now_ts, new_impact, sim_id),
                            )
                    merge_update = {"confidence": new_conf,
                                    "base_confidence": new_conf,
                                    "impact": new_impact}
                    if keep_new:
                        merge_update["fact"] = fact
                    self._emit_store_event("fact.update", sim_id, merge_update)
                    return dict(sim_fact, confidence=new_conf, impact=new_impact)
            except Exception as e:
                # Semantic dedup failure shouldn't prevent learning
                self._note_embed_failure("learn.semantic_dedup", e)

        # Determine provenance from source
        provenance = source if source in (
            "observation", "explicit", "lesson", "debrief",
            "reconstructed", "consolidated", "bootstrap",
        ) else "observed"

        entry = {
            "id": c_hash,
            "fact": fact,
            "confidence": confidence,
            "base_confidence": confidence,
            "project": project,
            "source": source,
            "provenance": provenance,
            "impact": impact,
            "tier": tier,
            "created_at": now_ts,
            "last_accessed": now_ts,
            "access_count": 0,
        }
        sid = self._current_session_id()
        if sid:
            entry["session_id"] = sid

        # Supersession + insert run in one write transaction so the fact
        # row and its personality_views row (unified mode) commit
        # atomically — observers never see one without the other.
        superseded_id: str | None = None
        with self.db.write_transaction():
            if replaces:
                superseded = self._find_fact_by_hash_or_text(replaces, project)
                if superseded is not None:
                    superseded_id = superseded.get(
                        "id", superseded.get("content_hash", ""))
                    entry["supersedes"] = superseded_id
                    self.db.supersede_fact(superseded_id, c_hash)
            self.db.insert_fact(entry)

        # Dual-write (issue #20): the add, plus the supersession of the
        # replaced fact when one was found.
        if superseded_id:
            self._emit_store_event("fact.update", superseded_id,
                                   {"superseded_by": c_hash})
        event_data = {
            k: v for k, v in entry.items()
            # Local statistics (access counters) are never evented — they
            # are recomputed locally (design doc).
            if k not in ("id", "supersedes", "last_accessed", "access_count")
            and v is not None
        }
        self._emit_store_event("fact.add", c_hash, event_data)

        # Auto-embed new facts for semantic search
        if self.embeddings is not None:
            try:
                vec = self.embeddings.embed(fact)
                self.embeddings.store_embedding(c_hash, vec, created_at=now_ts)
                self.db.conn.commit()
            except Exception as e:
                # Non-blocking — embedding failure shouldn't break learn()
                self._note_embed_failure("learn.auto_embed", e)

        if self._current_session is not None:
            self._current_session.facts_created += 1

        # Auto-generate calibration probes for facts with specific details
        try:
            self.auto_generate_probes(fact, c_hash)
        except Exception as e:
            logger.warning("probe generation failed for %s: %s", c_hash, e)

        # Sync to remote after every write (cross-machine persistence)
        self._sync_to_remote("observe")

        return entry

    def _find_fact_by_hash_or_text(self, query: str, project: str) -> dict | None:
        """Find a fact by content_hash or substring match."""
        result = self.db.get_fact_by_id(query)
        if result:
            return result
        return self.db.find_fact_by_text(query)

    def supersede(self, existing: dict, new_hash: str,
                  now_ts: str | None = None) -> None:
        """Mark an existing fact as superseded by a new one."""
        fact_id = existing.get("id", existing.get("content_hash", ""))
        if fact_id:
            self.db.supersede_fact(fact_id, new_hash)
            self.db.conn.commit()
            self._emit_store_event("fact.update", fact_id,
                                   {"superseded_by": new_hash})

    def observe(self, summary: str, project: str = "global",
                impact: float = 0.5, source: str = "observation") -> dict | None:
        """Tiered observation from a conversation turn.

        Classifies the observation into ephemeral/contextual/durable and sets
        confidence, impact, and tier accordingly. Suppresses near-duplicate
        observations when embeddings are available.
        """
        self._turn_count += 1

        if not summary or summary.lower() in ("no new facts", "nothing new", ""):
            return None

        # Phase 3b: record this turn's identity signature before processing
        self._record_turn_signature(summary)

        from null_memory.classifier import classify_observation

        # Check semantic novelty if embeddings available
        semantic_novelty = None
        emb = self.embeddings
        if emb is not None:
            try:
                results = emb.semantic_search(summary, limit=1)
                if results:
                    semantic_novelty = results[0][1]  # Top similarity score
            except Exception as e:
                self._note_embed_failure("observe.novelty", e)

        tier = classify_observation(
            summary,
            semantic_novelty=semantic_novelty,
            identity_terms=self.identity.get("identity_terms"),
            agent_name=self.identity.get("name"),
        )

        # Suppress near-duplicates entirely
        if semantic_novelty is not None and semantic_novelty > 0.92:
            return None

        entry = self.learn(
            summary,
            confidence=tier.confidence,
            project=project,
            source=source,
            impact=tier.impact,
            tier=tier.tier,
        )
        # Phase 3b S2: live fire — observe is a "learn" event in Nebula
        if entry and entry.get("id"):
            self._emit_nebula_event(
                kind="learn" if tier.tier != "ephemeral" else "observe",
                fact_id=entry["id"],
                intensity=tier.impact,
            )
        return entry


    # ── Decisions ──

    def decide(self, decision: str, reasoning: str,
               project: str = "global") -> dict:
        """Log a decision with reasoning.

        Auto-links the decision (stored as a high-confidence fact) to any
        facts that were recalled earlier in this session, creating a
        lightweight knowledge graph.

        Phase 3a: pre-commit semantic similarity check against the mistakes
        table. If a past mistake resembles this decision, we attach a warning
        to the return dict (does NOT block — Pete still decides).
        """
        # Phase 3b: turn signature for continuous identity
        self._record_turn_signature(f"decided: {decision} because {reasoning}")

        now_ts = datetime.now(timezone.utc).isoformat()

        # ── Pre-decide mistake surfacing ──
        mistake_warning = None
        emb_pre = self.embeddings
        if emb_pre is not None:
            try:
                decision_vec = emb_pre.embed(f"{decision} {reasoning}")
                rows = self.db.conn.execute(
                    "SELECT id, mistake, why FROM mistakes WHERE archived = 0 ORDER BY id DESC LIMIT 50"
                ).fetchall()
                best = None
                best_sim = 0.0
                for mid, mistake_text, why_text in rows:
                    text = f"{mistake_text} {why_text or ''}"
                    try:
                        mv = emb_pre.embed(text)
                        sim = float(emb_pre.cosine_similarity(decision_vec, mv))
                    except Exception:
                        continue
                    if sim > best_sim:
                        best_sim = sim
                        best = (mid, mistake_text, why_text)
                # Threshold 0.70 — tuned so exact paraphrase hits, loose
                # semantic overlap doesn't cry wolf.
                if best is not None and best_sim >= 0.70:
                    mistake_warning = {
                        "similarity": round(best_sim, 3),
                        "mistake": best[1],
                        "why": best[2],
                        "mistake_id": best[0],
                    }
            except Exception as e:
                self._note_embed_failure("decide.mistake_surfacing", e)

        # Capture reasoning trace: fact IDs recalled this session that informed this decision
        trace = list(self._session_recalled_ids[-15:]) if self._session_recalled_ids else []

        entry = {
            "decision": decision,
            "reasoning": reasoning,
            "project": project.strip().lower(),
            "trace": trace,
            "created_at": now_ts,
        }
        if mistake_warning:
            entry["mistake_warning"] = mistake_warning
        sid = self._current_session_id()
        if sid:
            entry["session_id"] = sid

        decision_id = self.db.insert_decision(entry)

        # Write to cross-instance decision feed
        feed_entry = dict(entry)
        feed_entry["status"] = "provisional"
        feed_id = self.db.insert_decision_feed(feed_entry)
        self.db.conn.commit()

        # Dual-write (issue #20). The decision feed is cross-instance
        # coordination state, not knowledge — only the decision is evented.
        self._emit_store_event("decision.add", decision_id, {
            "decision": decision,
            "reasoning": reasoning,
            "project": entry["project"],
            "session_id": entry.get("session_id"),
            "trace": trace,
            "created_at": now_ts,
        })

        # Embed decision for semantic cross-instance matching
        emb = self.embeddings
        if emb is not None:
            try:
                vec = emb.embed(f"{decision} {reasoning}")
                emb.store_embedding(f"d_{feed_id}", vec)
                self.db.conn.commit()
            except Exception as e:
                self._note_embed_failure("decide.embed", e)

        if self._current_session is not None:
            self._current_session.decisions_created += 1

        # Auto-link: connect recently recalled facts to this decision's fact
        # Store the decision text as a fact too, then link recalled facts to it
        if self._session_recalled_ids:
            decision_hash = self._content_hash(decision, project.strip().lower())
            decision_fact = self.db.get_fact_by_id(decision_hash)
            if decision_fact is None:
                # The decision isn't stored as a fact yet — that happens in debrief
                # Link recalled facts to each other instead (they informed this decision)
                recent_ids = self._session_recalled_ids[-10:]  # Last 10 recalled
                for fid in recent_ids:
                    for other_id in recent_ids:
                        if fid != other_id:
                            self.db.add_relationship(fid, other_id)
                self.db.conn.commit()

        self._sync_to_remote("decide")

        # Phase 3b S2: fire from decision point along trace edges
        decision_hash = self._content_hash(decision, project.strip().lower())
        self._emit_nebula_event(
            kind="decide",
            fact_id=decision_hash,
            related_ids=trace[-10:] if trace else [],
            intensity=0.9,
        )
        return entry

    def record_outcome(self, decision_query: str, outcome: str,
                       success: bool | None = None,
                       project: str | None = None) -> dict | None:
        """Record the outcome of a prior decision. Closes the learning loop.

        Args:
            decision_query: Keyword search to find the decision
            outcome: What actually happened
            success: True/False/None (unknown)
            project: Optional project filter

        Returns:
            Outcome record, or None if decision not found
        """
        decision = self.db.find_decision(decision_query, project=project)
        if decision is None:
            return None

        result = self.db.insert_outcome(
            decision_id=decision["id"],
            outcome=outcome,
            success=success,
        )
        self._emit_store_event("outcome.add", result["id"], {
            "decision_id": decision["id"],
            "outcome": outcome,
            "success": success,
            "recorded_at": result["recorded_at"],
        })

        # Learn from the outcome
        decision_text = decision.get("decision", "")
        success_str = "succeeded" if success else ("failed" if success is False else "unknown result")
        lesson = f"Decision outcome: '{decision_text[:60]}' — {success_str}: {outcome}"
        proj = decision.get("project", "global")
        self.learn(lesson, confidence=0.9, project=proj, source="observation")

        return result

    # ── Mistakes (Negative Knowledge) ──

    def mistake(self, what: str, why: str,
                project: str = "global", confidence: float = 0.95) -> dict:
        """Record a mistake — what went wrong and why. Never pruned by GC."""
        # Phase 3b: owning a mistake is strong identity signal
        self._record_turn_signature(f"mistake: {what} — why: {why}")
        now_ts = datetime.now(timezone.utc).isoformat()
        entry = {
            "mistake": what,
            "why": why,
            "project": project.strip().lower(),
            "confidence": confidence,
            "created_at": now_ts,
        }
        sid = self._current_session_id()
        if sid:
            entry["session_id"] = sid

        mistake_id = self.db.insert_mistake(entry)
        self.db.conn.commit()
        entry["id"] = mistake_id
        self._emit_store_event("mistake.add", mistake_id, {
            "mistake": what,
            "why": why,
            "project": entry["project"],
            "confidence": confidence,
            "session_id": entry.get("session_id"),
            "created_at": now_ts,
        })

        # Embed mistake for proactive similarity surfacing + Phase 5.3
        # project into Nebula so the red flash lands on a real point.
        emb = self.embeddings
        if emb is not None:
            try:
                vec = emb.embed(f"{what} {why}")
                emb.store_embedding(f"m_{mistake_id}", vec)
                self.db.conn.commit()
                self._project_mistake_viz(mistake_id, vec)
            except Exception as e:
                self._note_embed_failure("mistake.embed", e)

        if self._current_session is not None:
            self._current_session.mistakes_created += 1

        self._sync_to_remote("mistake")

        # Phase 3b S2: red flash in Nebula — now lands on a real point
        self._emit_nebula_event(
            kind="mistake",
            fact_id=f"m_{mistake_id}",
            intensity=1.0,
        )
        return entry


    # ── Reflections (Self-Assessment) ──

    def reflect(self, went_well: str, missed: str,
                do_differently: str, project: str = "global") -> dict:
        """Record a session reflection. Never pruned by GC."""
        # Phase 3b: reflections carry strong identity signal
        self._record_turn_signature(
            f"reflect: went well={went_well} missed={missed} differently={do_differently}"
        )
        now_ts = datetime.now(timezone.utc).isoformat()
        entry = {
            "went_well": went_well,
            "missed": missed,
            "do_differently": do_differently,
            "project": project.strip().lower(),
            "created_at": now_ts,
        }
        sid = self._current_session_id()
        if sid:
            entry["session_id"] = sid

        reflection_id = self.db.insert_reflection(entry)
        self.db.conn.commit()
        self._emit_store_event("reflection.add", reflection_id, {
            "went_well": went_well,
            "missed": missed,
            "do_differently": do_differently,
            "project": entry["project"],
            "session_id": entry.get("session_id"),
            "created_at": now_ts,
        })

        self._sync_to_remote("reflect")

        # Pattern detection
        self._detect_reflection_patterns()

        return entry

    def _detect_reflection_patterns(self) -> None:
        """If 3+ reflections mention the same issue in 'missed' or 'do_differently', flag in identity."""
        reflections = self.db.get_reflections(limit=10)
        if len(reflections) < 3:
            return

        word_counts: dict[str, int] = {}
        for r in reflections[-10:]:
            words = set()
            for field_name in ("missed", "do_differently"):
                text = r.get(field_name, "").lower()
                words.update(w for w in text.split() if w not in _STOP_WORDS and len(w) > 3)
            for w in words:
                word_counts[w] = word_counts.get(w, 0) + 1

        anti_patterns = self.identity.get("anti_patterns", [])
        for word, count in word_counts.items():
            if count >= 3:
                pattern = f"Recurring reflection theme: '{word}' (mentioned in {count} reflections)"
                if not any(word in p for p in anti_patterns):
                    anti_patterns.append(pattern)
                    self.identity["anti_patterns"] = anti_patterns

    # ── Forget ──

    def forget(self, query: str | None = None,
               fact_id: str | None = None) -> dict | None:
        """Soft-delete a fact. Returns the forgotten fact or None.

        Args:
            query: fuzzy text match — best recall hit wins, UNLESS the
                runner-up scores within FORGET_NEAR_TIE_RATIO of the top
                match, in which case ForgetAmbiguousError is raised with
                both candidates instead of guessing (near-duplicates are
                exactly where fuzzy matching deletes the wrong fact).
            fact_id: exact fact id — takes precedence over query; no
                fuzzy fallback. Returns None if the id doesn't exist or
                is already forgotten.
        """
        if fact_id:
            fact = self.db.get_fact_by_id(fact_id)
            if fact is None:
                return None
            if self.db.forget_fact(fact_id):
                # Tombstone event — references the entity id, never a fuzzy
                # match (issue #20).
                self._emit_store_event("fact.forget", fact_id, {})
                return dict(fact)
            return None

        if not query:
            return None

        results = self.recall(query, limit=5, include_mistakes=False, _emit_event=False)
        candidates = [
            r for r in results
            if r.get("_type") == "fact" and r.get("_score") is not None
        ]
        if not candidates:
            return None

        top = candidates[0]
        if len(candidates) > 1:
            runner_up = candidates[1]
            top_score = top.get("_score") or 0.0
            runner_score = runner_up.get("_score") or 0.0
            if top_score > 0 and (runner_score / top_score) >= FORGET_NEAR_TIE_RATIO:
                raise ForgetAmbiguousError([top, runner_up])

        fid = top.get("id", top.get("content_hash"))
        if fid and self.db.forget_fact(fid):
            self._emit_store_event("fact.forget", fid, {})
            return top
        return None

    # ── Emotional Anchors (Phase 2a — schema v13, unified DB) ──

    def anchor(self, query: str, anchor_type: str,
               note: str = "") -> dict | None:
        """Tag a fact as an emotional anchor. ``query`` can be a 12-char
        fact id or text — best recall match wins.

        Anchors are load-bearing: they never decay (effective_confidence = 1.0),
        surface first in briefing, and get a 2× recall boost.
        """
        if not self.db.anchors_supported():
            # Capability gate, not a store-flavor gate: per-seat worker
            # stores carry the anchor columns via the structural heal, and
            # onboarding (issue #27) needs anchors on seats. Only a legacy
            # store whose facts table truly lacks the columns refuses.
            raise RuntimeError(
                "Emotional anchors require the v13 anchor columns "
                "(unified DB, or a per-seat store healed to the unified "
                "layout). Run migrate_v3 and restart."
            )
        if anchor_type not in self.db.ANCHOR_TYPES:
            raise ValueError(
                f"anchor_type must be one of {self.db.ANCHOR_TYPES}"
            )
        # Resolve query → fact id
        fact = self.db.get_fact_by_id(query)
        if fact is None:
            results = self.recall(query, limit=1, include_mistakes=False, _emit_event=False)
            if not results:
                return None
            fact = results[0]
        fact_id = fact.get("id")
        if not fact_id:
            return None
        if self.db.set_anchor(fact_id, anchor_type, note):
            self.db.conn.commit()
            self._emit_store_event("fact.anchor", fact_id,
                                   {"anchor_type": anchor_type, "note": note})
            fact = self.db.get_fact_by_id(fact_id)
            # Phase 3b S2: golden anchor pulse in Nebula
            self._emit_nebula_event(
                kind="anchor", fact_id=fact_id, intensity=1.2,
            )
            return fact
        return None

    def get_anchors(self, anchor_type: str | None = None) -> list[dict]:
        return self.db.get_anchors(anchor_type=anchor_type)

    def verify_identity(self) -> dict:
        """Run the four-proof identity check (Phase 2c + Phase 3b).

        1. Memory access — the code word can be retrieved
        2. Shared experience — continuity probe pass rate
        3. Behavioral continuity — latest session drift vs baseline
        4. Mid-session continuity — per-turn drift within this session

        Returns {verdict, proofs, details}. verdict ∈ {pass, ambiguous, fail}.
        """
        proofs = {"memory_access": None, "shared_experience": None,
                  "behavioral_continuity": None,
                  "mid_session_continuity": None}

        # Proof 1 — code word. The secret lives only in the database (it
        # was scrubbed from source pre-launch), so the proof is: recall
        # for the code-word LABEL surfaces the same fact the identity
        # payload's anchored lookup returns. No literal here also means
        # this survives code-word rotation untouched.
        from null_memory.identity_payload import _fetch_code_word
        code_word_fact = _fetch_code_word(self.db.conn)
        code_word_results = self.recall(
            "identity verification code word", limit=3, _emit_event=False
        )
        proofs["memory_access"] = bool(code_word_fact) and any(
            (r.get("fact") or "") == code_word_fact
            for r in code_word_results
        )

        # Proof 2 — continuity probes
        probe_run = self.run_continuity_probes()
        proofs["shared_experience"] = (
            probe_run["score"] >= 0.66 if probe_run["total"] else None
        )

        # Proof 3 — drift (uses the same helper briefing uses)
        drift_line = self._identity_drift_line()
        if drift_line:
            proofs["behavioral_continuity"] = "drift detected" not in drift_line
        else:
            proofs["behavioral_continuity"] = None  # Insufficient data

        # Proof 4 — mid-session continuity (per-turn drift this session)
        mid = self.mid_session_drift_state()
        if len(self._turn_signatures) < self.MIN_TURNS_BEFORE_DRIFT_CHECK:
            proofs["mid_session_continuity"] = None
        else:
            proofs["mid_session_continuity"] = mid is None

        # Verdict — require ≥2 non-None proofs to declare pass/fail; fewer
        # signals means we can't honestly call it (AMBIGUOUS).
        non_null = [v for v in proofs.values() if v is not None]
        if len(non_null) < 2:
            verdict = "ambiguous"
        elif all(non_null):
            verdict = "pass"
        elif not any(non_null):
            verdict = "fail"
        else:
            verdict = "ambiguous"  # mixed signals

        return {
            "verdict": verdict,
            "proofs": proofs,
            "details": {
                "probes": probe_run,
                "drift": drift_line,
                "mid_session_turns": len(self._turn_signatures),
                "mid_session_drift": mid,
            },
        }


    # ── Contradictions ──

    def check_contradiction(self, new_fact: str, project: str = "global") -> dict | None:
        """Check if a new fact contradicts existing knowledge.

        Strategy:
        1. If embeddings available: find semantically similar facts (>0.6 similarity),
           then check for negation signals between the similar pair.
        2. Fallback: keyword overlap + negation pair detection (original approach).
        """
        new_words_filtered = set(new_fact.lower().split()) - _STOP_WORDS
        if len(new_words_filtered) < 3:
            return None

        negation_signals = {
            "don't", "doesn't", "didn't", "not", "no", "never", "none",
            "without", "remove", "disable", "skip", "stop", "can't",
            "won't", "shouldn't", "isn't", "aren't", "wasn't", "weren't",
            "false", "incorrect", "wrong", "fail",
        }
        positive_signals = {
            "do", "does", "did", "always", "all", "every",
            "add", "enable", "include", "start", "can",
            "will", "should", "is", "are", "was", "were",
            "true", "correct", "right", "pass",
        }

        project = project.strip().lower()

        def _has_negation_asymmetry(words_a: set[str], words_b: set[str]) -> bool:
            """Check if one set has negation signals the other lacks —
            OR pairs a negation against a positive-signal asymmetry. The
            positive-signal check catches cases like 'we should do X' vs
            'we should not do X' where 'should' appears on both sides but
            the negation on one side flips the claim."""
            neg_a = words_a & negation_signals
            neg_b = words_b & negation_signals
            pos_a = words_a & positive_signals
            pos_b = words_b & positive_signals
            # One side negates, the other makes a positive claim: contradiction.
            if neg_a and pos_b:
                return True
            if neg_b and pos_a:
                return True
            # One has negation, the other doesn't (no positive either).
            if neg_a and not neg_b:
                return True
            if neg_b and not neg_a:
                return True
            # Both have negation but different ones (less reliable).
            if neg_a and neg_b and neg_a != neg_b:
                return True
            return False

        # Semantic approach: find similar facts, then check for negation
        emb = self.embeddings
        if emb is not None:
            try:
                similar = emb.semantic_search(new_fact, limit=5)
                for sim_id, sim_score in similar:
                    if sim_score < 0.6:
                        break
                    entry = self.db.get_fact_by_id(sim_id)
                    if entry is None or entry.get("forgotten") or entry.get("superseded_by"):
                        continue
                    if entry.get("project", "global") not in (project, "global"):
                        continue
                    entry_words = set(entry["fact"].lower().split()) - _STOP_WORDS
                    if _has_negation_asymmetry(new_words_filtered, entry_words):
                        return entry
            except Exception as e:
                # Fall through to keyword approach
                self._note_embed_failure("contradict.semantic", e)

        # Keyword fallback: overlap + negation pair detection
        facts = self.db.get_active_facts()
        for entry in facts:
            if entry.get("project", "global") not in (project, "global"):
                continue

            entry_words = set(entry["fact"].lower().split()) - _STOP_WORDS
            overlap = new_words_filtered & entry_words
            if len(overlap) < max(2, len(new_words_filtered) * 0.4):
                continue

            if _has_negation_asymmetry(new_words_filtered, entry_words):
                return entry

        return None

    # ── Proactive Mistake Surfacing ──

    def check_mistake_similarity(self, text: str,
                                 project: str = "global") -> dict | None:
        """Check if text is semantically similar to a past mistake.

        Returns the matching mistake dict if similarity > 0.65, else None.
        """
        emb = self.embeddings
        if emb is None:
            return None

        try:
            results = emb.search_by_prefix(text, "m_", limit=1)
            if not results:
                return None

            top_id, similarity = results[0]
            if similarity < 0.65:
                return None

            # Extract mistake ID from embedding key (m_{id})
            mistake_id_str = top_id[2:]  # Strip "m_" prefix
            try:
                mistake_id = int(mistake_id_str)
            except ValueError:
                return None

            mistake = self.db.get_mistake_by_id(mistake_id)
            if mistake is None:
                return None

            # Project filter — only warn about same project or global
            mistake_proj = mistake.get("project", "global")
            if mistake_proj != "global" and mistake_proj != project.strip().lower():
                return None

            mistake["_similarity"] = similarity
            return mistake
        except Exception:
            return None

    # ── Decision Replay ──

    def replay_similar_decision(self, text: str,
                                project: str = "global") -> dict | None:
        """Find a past decision similar to the current context and replay its trace.

        Returns the decision with its reasoning chain (the facts that were
        in context when the decision was made). This enables judgment
        reconstruction — not just what was decided, but how.
        """
        # Try semantic search first
        emb = self.embeddings
        if emb is not None:
            try:
                results = emb.search_by_prefix(text, "d_", limit=3)
                for emb_id, similarity in results:
                    if similarity < 0.5:
                        continue
                    feed_id_str = emb_id[2:]
                    try:
                        feed_id = int(feed_id_str)
                    except ValueError:
                        continue
                    # Get the feed entry to find session_id
                    feed = self.db.conn.execute(
                        "SELECT * FROM decision_feed WHERE id = ?", (feed_id,)
                    ).fetchone()
                    if not feed:
                        continue
                    # Find the matching decision with trace
                    decisions = self.db.conn.execute(
                        """SELECT * FROM decisions
                           WHERE decision = ? AND session_id = ?
                           ORDER BY created_at DESC LIMIT 1""",
                        (dict(feed)["decision"], dict(feed).get("session_id")),
                    ).fetchone()
                    if decisions:
                        d = dict(decisions)
                        try:
                            d["trace"] = json.loads(d.get("trace") or "[]")
                        except (json.JSONDecodeError, TypeError):
                            d["trace"] = []
                        # Resolve trace fact IDs to actual fact text
                        d["trace_facts"] = []
                        for fid in d["trace"][:10]:
                            fact = self.db.get_fact_by_id(fid)
                            if fact:
                                d["trace_facts"].append(fact["fact"][:120])
                        d["_similarity"] = similarity
                        return d
            except Exception:
                pass

        # Keyword fallback
        results = self.db.find_similar_decisions_with_traces(text, project, limit=1)
        if results:
            d = results[0]
            d["trace_facts"] = []
            for fid in d.get("trace", [])[:10]:
                fact = self.db.get_fact_by_id(fid)
                if fact:
                    d["trace_facts"].append(fact["fact"][:120])
            return d

        return None

    # ── Cross-Instance Decision Awareness ──

    def check_prior_decisions(self, text: str,
                              project: str = "global") -> dict | None:
        """Check if text relates to a decision made in another session.

        Searches the decision feed using semantic similarity (if embeddings)
        or keyword fallback. Excludes decisions from the current session.
        Returns the most relevant prior decision, or None.
        """
        sid = self._current_session_id()

        # Try semantic search first
        emb = self.embeddings
        if emb is not None:
            try:
                results = emb.search_by_prefix(text, "d_", limit=3)
                for emb_id, similarity in results:
                    if similarity < 0.55:
                        continue
                    # Extract feed ID
                    feed_id_str = emb_id[2:]
                    try:
                        feed_id = int(feed_id_str)
                    except ValueError:
                        continue
                    # Fetch the decision
                    row = self.db.conn.execute(
                        "SELECT * FROM decision_feed WHERE id = ?",
                        (feed_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    d = dict(row)
                    # Skip if from current session
                    if sid and d.get("session_id") == sid:
                        continue
                    # Project filter
                    d_proj = d.get("project", "global")
                    if d_proj != "global" and d_proj != project.strip().lower():
                        continue
                    d["_similarity"] = similarity
                    return d
            except Exception:
                pass

        # Keyword fallback
        try:
            results = self.db.search_decision_feed(
                text, project=project, exclude_session=sid, limit=1,
            )
            if results:
                return results[0]
        except Exception:
            pass

        return None

    # ── Proactive Insight Pushing ──

    def find_relevant_insights(self, text: str, project: str = "global",
                               threshold: float = 0.55,
                               min_impact: float = 0.6) -> list[dict]:
        """Find high-impact facts semantically related to the current observation.

        Unlike recall (which answers queries), insights are *unprompted contributions* —
        knowledge Atlas pushes because it's relevant, not because Pete asked.

        Returns up to 2 insights, or empty list. Deduplicates by:
        - Facts already recalled in this session (_session_recalled_ids)
        - Topics already surfaced as insights (_insight_topics_surfaced)
        - Semantic similarity to previously surfaced insights (>0.8 = same topic)

        Lowered thresholds vs v1: 0.55 similarity (was 0.6), 0.6 impact (was 0.7)
        to surface more relevant context while topic dedup prevents noise.
        """
        emb = self.embeddings
        if emb is None:
            return []

        try:
            results = emb.semantic_search(text, limit=10)
            proj = project.strip().lower() if project else None

            insights = []
            for fid, similarity in results:
                if similarity < threshold:
                    continue
                if len(insights) >= 2:
                    break

                # Skip facts already in session context
                if fid in self._session_recalled_ids:
                    continue

                fact = self.db.get_fact_by_id(fid)
                if fact is None:
                    continue
                if fact.get("forgotten") or fact.get("archived") or fact.get("superseded_by"):
                    continue

                # Must be meaningful impact
                impact = fact.get("impact", 0.5)
                if impact < min_impact:
                    continue

                # Project filter
                fact_proj = fact.get("project", "global")
                if proj and fact_proj not in (proj, "global"):
                    continue

                # Topic dedup — don't surface the same topic twice in a session
                fact_text = fact.get("fact", "")
                if self._is_topic_already_surfaced(fact_text, emb):
                    continue

                fact["_similarity"] = similarity
                insights.append(fact)
                self._insight_topics_surfaced.append(fact_text)

            return insights

        except Exception:
            return []

    def _is_topic_already_surfaced(self, text: str, emb) -> bool:
        """Check if this topic was already surfaced as an insight this session."""
        if not self._insight_topics_surfaced:
            return False
        try:
            text_vec = emb.embed(text)
            for prev in self._insight_topics_surfaced[-20:]:  # Check last 20
                prev_vec = emb.embed(prev)
                sim = float(emb.cosine_similarity(text_vec, prev_vec))
                if sim > 0.8:  # Same topic
                    return True
        except Exception as e:
            self._note_embed_failure("insight.topic_dedup", e)
        return False

    # ── Exemplars ──

    def load_exemplars(self) -> list[dict]:
        """Load calibration exemplars from database."""
        return self.db.get_exemplars()

    def add_exemplar(self, scenario: str, user_text: str, agent_text: str = "",
                     calibration: str = "", tags: list[str] | None = None) -> dict:
        """Add a new calibration exemplar.

        Args:
            user_text: What the user said (column was named ``pete`` pre-v13).
            agent_text: How the agent responded (was ``atlas`` pre-v13).
        """
        entry = {
            "scenario": scenario,
            "user_text": user_text,
            "agent_text": agent_text,
            "calibration": calibration,
            "tags": tags or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        exemplar_id = self.db.insert_exemplar(entry)
        self.db.conn.commit()
        self._emit_store_event("exemplar.add", exemplar_id, dict(entry))
        return entry

    def find_exemplars(self, query: str, limit: int = 3) -> list[dict]:
        """Find relevant exemplars by keyword/tag match (word boundary)."""
        exemplars = self.load_exemplars()
        if not exemplars:
            return []

        query_lower = query.lower()
        tokens = [_strip_punctuation(t) for t in query_lower.split() if _strip_punctuation(t)]
        tokens = [t for t in tokens if t not in _STOP_WORDS] or tokens

        scored: list[tuple[float, dict]] = []
        for ex in exemplars:
            score = 0.0
            searchable = (
                ex.get("scenario", "") + " " +
                ex.get("user_text", ex.get("pete", "")) + " " +
                ex.get("calibration", "") + " " +
                " ".join(ex.get("tags", []))
            ).lower()
            searchable_words = {_strip_punctuation(w) for w in searchable.split() if _strip_punctuation(w)}

            for t in tokens:
                if t in searchable_words:
                    score += 1.0
            if score > 0:
                scored.append((score, ex))

        scored.sort(key=lambda x: -x[0])
        return [ex for _, ex in scored[:limit]]


    # ── Briefing ──

    # ── Continuous Identity (Phase 3b — per-turn drift) ──

    TURN_SIGNATURE_BUFFER = 20           # cap on in-session history
    MID_SESSION_DRIFT_THRESHOLD = 0.35   # cosine-distance alarm threshold
    MIN_TURNS_BEFORE_DRIFT_CHECK = 4     # need a baseline of ≥3 prior turns


    def _record_turn_signature(self, text: str) -> None:
        """Compute + buffer this turn's identity signature. Also checks drift
        against the in-session baseline and, if sharp, sets a one-shot warning
        for the next handler response.

        Cheap per-turn: one fastembed call. Non-fatal on any failure — must
        not break the primary action (observe / learn / decide / etc.).
        """
        if not getattr(self.db, "unified", False):
            return  # Continuous identity only in unified mode
        emb = self.embeddings
        if emb is None or not text or not text.strip():
            return
        try:
            import numpy as np
            vec = emb.embed(text.strip()[:4000]).astype(np.float32)
            self._turn_signatures.append(vec)
            if len(self._turn_signatures) > self.TURN_SIGNATURE_BUFFER:
                self._turn_signatures = self._turn_signatures[-self.TURN_SIGNATURE_BUFFER:]
            self._check_mid_session_drift()
        except Exception as e:
            self._note_embed_failure("turn_signature", e)

    def _check_mid_session_drift(self) -> None:
        """If the most recent turn diverges sharply from the in-session
        baseline, stage a warning for the next MCP response."""
        sigs = self._turn_signatures
        if len(sigs) < self.MIN_TURNS_BEFORE_DRIFT_CHECK:
            return
        try:
            import numpy as np
            recent = sigs[-1]
            baseline = np.mean(np.stack(sigs[:-1]), axis=0)
            rn = float(np.linalg.norm(recent))
            bn = float(np.linalg.norm(baseline))
            if rn == 0 or bn == 0:
                return
            cos = float(np.dot(recent, baseline) / (rn * bn))
            distance = 1.0 - cos
            if distance >= self.MID_SESSION_DRIFT_THRESHOLD:
                if (self._mid_session_drift_warning is None
                        or not self._mid_session_drift_surfaced):
                    self._mid_session_drift_warning = {
                        "distance": round(distance, 3),
                        "turn_index": len(sigs),
                        "baseline_size": len(sigs) - 1,
                    }
                    self._mid_session_drift_surfaced = False
            else:
                # Back in range — clear the stale warning so future drift re-fires.
                if (self._mid_session_drift_warning is not None
                        and self._mid_session_drift_surfaced):
                    self._mid_session_drift_warning = None
                    self._mid_session_drift_surfaced = False
        except Exception:
            pass

    def consume_mid_session_drift_warning(self) -> dict | None:
        """Return the pending mid-session drift warning (if any) and mark it
        surfaced so it doesn't repeat on the next turn."""
        if self._mid_session_drift_warning is None:
            return None
        if self._mid_session_drift_surfaced:
            return None  # Already shown — don't repeat
        self._mid_session_drift_surfaced = True
        return dict(self._mid_session_drift_warning)

    def mid_session_drift_state(self) -> dict | None:
        """Read-only view of the current drift state (for verify_identity)."""
        if self._mid_session_drift_warning is None:
            return None
        return dict(self._mid_session_drift_warning)

    def _identity_drift_line(self) -> str:
        """Return a one-line drift summary for briefing, or ''.

        Needs at least 3 past sessions with identity_vector populated.
        Returns a distance-labeled string: 'consistent' (<0.15),
        'normal variance' (0.15-0.3), or 'drift detected' (>0.3).
        """
        if not getattr(self.db, "unified", False):
            return ""
        rows = self.db.conn.execute(
            """SELECT identity_vector, created_at FROM session_fingerprints
               WHERE personality = ? AND identity_vector IS NOT NULL
               ORDER BY created_at DESC LIMIT 11""",
            (self.personality,),
        ).fetchall()
        if len(rows) < 3:
            return ""
        last_vec = rows[0][0]
        # Baseline = sessions [1:] (exclude the one we're measuring)
        try:
            import numpy as np
            baseline_rows = rows[1:]
            vecs = []
            weights = []
            n = len(baseline_rows)
            for i, r in enumerate(baseline_rows):
                try:
                    v = np.frombuffer(r[0], dtype=np.float32)
                    if v.size == 0:
                        continue
                    vecs.append(v)
                    weights.append(n - i)
                except Exception:
                    continue
            if len(vecs) < 2:
                return ""
            w = np.array(weights, dtype=np.float32).reshape(-1, 1)
            baseline = (np.stack(vecs) * w).sum(axis=0) / w.sum()
            from null_memory.fingerprint import identity_drift
            distance = identity_drift(last_vec, baseline)
            if distance is None:
                return ""
            if distance < 0.15:
                verdict = "voice consistent"
            elif distance < 0.3:
                verdict = "normal variance"
            else:
                verdict = "⚠ drift detected"
            return f"Identity drift: {verdict} (cosine dist {distance:.2f} across {len(vecs)} prior sessions)"
        except Exception:
            return ""


    # ── Identity ──

    def set_name(self, name: str) -> None:
        self.identity["name"] = name
        self.save_identity()

    def format_identity(self) -> str:
        """Format identity for display."""
        lines = [f"[Null] {self.name}"]
        ws = self.identity.get("working_style", {})
        if ws:
            lines.append("\nWorking style:")
            for k, v in ws.items():
                lines.append(f"  {k}: {v}")
        prefs = self.identity.get("user_preferences", {})
        if prefs:
            lines.append("\nUser preferences:")
            for k, v in prefs.items():
                lines.append(f"  {k}: {v}")
        anti = self.identity.get("anti_patterns", [])
        if anti:
            lines.append("\nAnti-patterns:")
            for a in anti:
                lines.append(f"  - {a}")
        return "\n".join(lines)

    # ── Debrief (Deep Session Summary) ──

    def debrief(self, summary: str, decisions_made: list[str] | None = None,
                lessons: list[str] | None = None,
                identity_updates: dict[str, str] | None = None,
                project: str = "global") -> dict:
        """Deep session debrief — captures rich context, not just facts."""
        results = {"facts": 0, "decisions": 0, "identity_updated": False}

        if summary:
            self.learn(summary, confidence=0.95, project=project, source="debrief")
            results["facts"] += 1

        if decisions_made:
            for d in decisions_made:
                if " — " in d:
                    decision, reasoning = d.split(" — ", 1)
                elif " because " in d.lower():
                    idx = d.lower().rfind(" because ")
                    decision = d[:idx]
                    reasoning = d[idx + 9:]
                else:
                    decision = d
                    reasoning = "recorded during session debrief"
                self.decide(decision.strip(), reasoning.strip(), project=project)
                results["decisions"] += 1

        if lessons:
            for lesson in lessons:
                self.learn(lesson, confidence=0.95, project=project, source="lesson")
                results["facts"] += 1

        if identity_updates:
            for key, value in identity_updates.items():
                if key == "anti_pattern":
                    anti = self.identity.setdefault("anti_patterns", [])
                    if value not in anti:
                        anti.append(value)
                elif key == "capability":
                    caps = self.identity.setdefault("capabilities", [])
                    if value not in caps:
                        caps.append(value)
                else:
                    ws = self.identity.setdefault("working_style", {})
                    ws[key] = value
            self.save_identity()
            results["identity_updated"] = True

        # Lifecycle boundary — flush any debounced remote sync now.
        self._sync_to_remote("debrief", immediate=True)

        return results

    # ── Sync ──

    def sync(self) -> str:
        """Flush state and save. Runs GC if over max."""
        self.save_identity()

        # Save project contexts
        for name, data in self.projects.items():
            safe_name = self._sanitize_name(name)
            self._ensure_dir()
            path = os.path.join(self.agent_dir, "projects", f"{safe_name}.json")
            self._atomic_write_json(path, data)

        # Auto-consolidate
        consolidate_msg = ""
        fact_count = self.db.count_facts()
        if fact_count > 200:
            c_result = self.consolidate()
            if c_result["consolidated"] or c_result["strengthened"] or c_result["faded"]:
                consolidate_msg = (
                    f" Consolidated: {c_result['consolidated']} merged, "
                    f"{c_result['strengthened']} strengthened, {c_result['faded']} faded."
                )

        # Run GC if over max
        max_facts = self.config.get("max_facts", 5000)
        max_env = os.environ.get("NULL_MAX_FACTS", "")
        if max_env:
            try:
                max_facts = int(max_env)
            except ValueError:
                pass

        gc_msg = ""
        if self.db.count_facts() > max_facts:
            gc_result = self.gc(max_facts)
            gc_msg = f" GC: archived {gc_result['archived']}, merged {gc_result['merged']}."

        return (
            f"[Null] {self.name} signing off. "
            f"{self.db.count_facts()} facts, {self.db.count_mistakes()} mistakes, "
            f"{self.db.count_decisions()} decisions saved.{consolidate_msg}{gc_msg}"
        )

    # ── Session Lifecycle ──

    def start_session(self, project: str = "global", git_cwd: str | None = None) -> Session:
        """Start a new session. Called lazily on first tool use.

        The foreground does ZERO git work: the session record + active
        pointer (and, when a prior crash was detected, the crashed record +
        old-pointer retirement — all plain file I/O) are written
        synchronously, and ALL git (repo init, prior-crash commit, project
        git-state capture) is pushed onto a daemon thread. A slow disk /
        large repo / push therefore never blocks the MCP tool response. Git
        state and crash markers land asynchronously a moment later; the
        session object is mutated in place.
        """
        if self._session_manager is None:
            self._session_manager = SessionManager(
                self.agent_dir, personality=self.personality)

        # Prior-crash bookkeeping. The FILE half (crashed record + active-
        # pointer retirement) is cheap local I/O and MUST happen now, BEFORE
        # start_session writes the NEW session's active pointer — run on the
        # background thread it unlinked the new pointer after the fact and
        # destroyed crash detection for every session following a crash.
        # Only the git commit is deferred.
        prior_crash = self._prior_crash
        self._prior_crash = None
        if prior_crash is not None:
            self._prior_crash_record = prior_crash
            try:
                self._session_manager.mark_crashed_files(prior_crash)
            except Exception:
                pass

        # Foreground: create the session with NO git (defer_git=True).
        session = self._session_manager.start_session(
            project=project, git_cwd=git_cwd, defer_git=True,
        )
        self._current_session = session
        self._emit_store_event("session.open", session.session_id, {
            "project": project,
            "started_at": session.started_at,
        })

        def _git_init_and_crash() -> None:
            try:
                # Repo must exist before any commit (crash-marker/auto-close
                # commits silently no-op on a non-repo).
                self._session_manager.ensure_repo()
                if prior_crash is not None:
                    self._session_manager.commit_crash_marker(prior_crash)
                self._session_manager.finish_session_git(session, git_cwd=git_cwd)
            except Exception:
                pass

        t = threading.Thread(target=_git_init_and_crash, daemon=True)
        t.start()
        self._session_git_thread = t

        # Bounded atexit join: a process exiting right after session start
        # must not lose the repo init / crash-marker commit on the daemon
        # thread. Registered once; joins whatever thread is current at exit.
        if not self._session_git_atexit_registered:
            import atexit
            atexit.register(self._join_session_git_thread)
            self._session_git_atexit_registered = True

        return session

    def _join_session_git_thread(self, timeout: float = 5.0) -> None:
        """Bounded atexit join of the deferred session-start git thread.

        The thread is a daemon — without this, a process exit right after
        session start can lose the repo init / crash-marker commits."""
        t = self._session_git_thread
        if t is not None and t.is_alive():
            t.join(timeout)

    def end_session(self, summary: str = "") -> bool:
        """End the current session and commit to git. Returns True if committed."""
        if self._current_session is None or self._session_manager is None:
            return False
        # The deferred session-start thread lazily inits the repo. If it hasn't
        # finished, the commit below races it and intermittently returns
        # committed=False (flaky under load — bit test_end_session /
        # test_close_commits_to_git on CI). Join it first so the repo is ready.
        self._join_session_git_thread()
        session_id = self._current_session.session_id
        committed = self._session_manager.end_session(self._current_session, summary=summary)
        self._current_session = None
        self._emit_store_event("session.close", session_id,
                               {"summary": summary})
        return committed

    def checkpoint(self, note: str = "") -> bool:
        """Mid-session checkpoint: save session state and commit."""
        if self._current_session is None or self._session_manager is None:
            return False
        # Same race as end_session: don't commit before the deferred repo-init
        # thread has finished, or the commit can no-op to committed=False.
        self._join_session_git_thread()
        self._current_session.touch()
        committed = self._session_manager.checkpoint_commit(self._current_session, note=note)
        # Lifecycle boundary — flush any debounced remote sync now.
        self._sync_to_remote("checkpoint", immediate=True)
        return committed

    def _auto_extract_exemplars(self) -> int:
        """Auto-extract calibration exemplars from session decisions and mistakes.

        Creates up to 3 exemplars per session from the most interesting
        judgment calls — moments where Atlas made a choice with reasoning.
        Mistakes prioritized over decisions (higher learning value).
        """
        sid = self._current_session_id()
        if not sid:
            return 0

        now_ts = datetime.now(timezone.utc).isoformat()
        project = self._current_session.project if self._current_session else "global"
        created = 0

        # Get mistakes with reasoning from this session
        mistakes = self.db.conn.execute(
            "SELECT mistake, why FROM mistakes WHERE session_id = ? AND why IS NOT NULL AND why != ''",
            (sid,),
        ).fetchall()

        # Get decisions with reasoning from this session
        decisions = self.db.conn.execute(
            "SELECT decision, reasoning FROM decisions WHERE session_id = ? AND reasoning IS NOT NULL AND reasoning != ''",
            (sid,),
        ).fetchall()

        # Mistakes first (higher learning value), then decisions by reasoning length
        entries = []
        extracted: list[tuple[int, dict]] = []
        for m in mistakes:
            entries.append(("mistake", m[0], m[1]))
        decisions_sorted = sorted(decisions, key=lambda d: len(d[1] or ""), reverse=True)
        for d in decisions_sorted:
            entries.append(("decision", d[0], d[1]))

        for entry_type, text, reasoning in entries[:3]:
            if entry_type == "mistake":
                exemplar_entry = {
                    "scenario": f"[{project}] {self.name} made a mistake",
                    "user_text": text[:200],
                    "agent_text": f"Made mistake: {text[:100]}",
                    "calibration": f"Why: {reasoning[:200]}",
                    "tags": [project, "mistake", "auto-extracted"],
                    "created_at": now_ts,
                }
            else:
                exemplar_entry = {
                    "scenario": f"[{project}] {self.name} made a decision",
                    "user_text": f"Context: {project} work",
                    "agent_text": text[:200],
                    "calibration": reasoning[:200],
                    "tags": [project, "decision", "auto-extracted"],
                    "created_at": now_ts,
                }
            exemplar_id = self.db.insert_exemplar(exemplar_entry)
            extracted.append((exemplar_id, exemplar_entry))
            created += 1

        if created:
            self.db.conn.commit()
            # Events follow the commit — the log only describes committed
            # truth (issue #20).
            for exemplar_id, exemplar_entry in extracted:
                self._emit_store_event("exemplar.add", exemplar_id,
                                       exemplar_entry)

        return created

    def close(self, summary: str = "", went_well: str = "",
              missed: str = "", do_differently: str = "",
              decisions_made: list[str] | None = None,
              lessons: list[str] | None = None,
              identity_updates: dict[str, str] | None = None,
              project: str = "global") -> dict:
        """Atomic session close: debrief + reflect + sync + git commit."""
        results = {"debrief": {}, "reflected": False, "synced": "", "committed": False}

        if summary or decisions_made or lessons or identity_updates:
            results["debrief"] = self.debrief(
                summary=summary,
                decisions_made=decisions_made,
                lessons=lessons,
                identity_updates=identity_updates,
                project=project,
            )

        if went_well or missed or do_differently:
            self.reflect(went_well, missed, do_differently, project=project)
            results["reflected"] = True

        results["synced"] = self.sync()

        # Auto-extract exemplars from session decisions and mistakes
        try:
            exemplars_created = self._auto_extract_exemplars()
            results["exemplars_created"] = exemplars_created
        except Exception:
            pass

        # Compute and store session fingerprint
        if self._current_session is not None:
            try:
                from null_memory.fingerprint import compute_fingerprint
                fp = compute_fingerprint(self, self._current_session)
                self.db.insert_fingerprint({
                    "session_id": fp.session_id,
                    "project": fp.project,
                    "duration_minutes": fp.duration_minutes,
                    "facts_count": fp.facts_count,
                    "decisions_count": fp.decisions_count,
                    "mistakes_count": fp.mistakes_count,
                    "tier_dist": fp.tier_dist,
                    "topic_vector": fp.topic_vector,
                    "outcome": fp.outcome,
                    "tags": fp.tags,
                    "energy_arc": fp.energy_arc,
                    "highlights": fp.highlights,
                    "created_at": fp.created_at,
                    "identity_vector": fp.identity_vector,
                    "identity_model": fp.identity_model,
                })
                self.db.conn.commit()
                results["fingerprint"] = fp.outcome
            except Exception:
                pass

        results["committed"] = self.end_session(summary=summary or project)

        # Lifecycle boundary — flush any debounced remote sync now.
        self._sync_to_remote("close", immediate=True)

        return results

    def detect_gaps(self) -> dict:
        """Detect gaps in memory coverage. Used by briefing()."""
        if self._session_manager is None:
            return {}
        return self._session_manager.detect_gaps()

    # ── Catchup (reconstruct knowledge from evidence) ──

    def catchup_from_git(self, project: str = "global",
                         since: str = "", git_cwd: str | None = None) -> list[dict]:
        """Reconstruct knowledge from git commit history."""
        cwd = git_cwd or os.getcwd()

        cmd = ["log", "--oneline", "--no-merges"]
        if since:
            cmd.extend(["--since", since])
        else:
            if self._session_manager:
                last = self._session_manager.last_completed_session()
                if last and last.git_head:
                    cmd = ["log", "--oneline", "--no-merges", f"{last.git_head}..HEAD"]

        from null_memory.session import _run_git
        result = _run_git(cmd, cwd=cwd)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        lines = result.stdout.strip().splitlines()
        facts_created = []

        groups: dict[str, list[str]] = {}
        for line in lines:
            parts = line.split(" ", 1)
            if len(parts) < 2:
                continue
            msg = parts[1]
            prefix = "misc"
            if ":" in msg:
                prefix = msg.split(":")[0].strip().lower()
            groups.setdefault(prefix, []).append(msg)

        for prefix, messages in groups.items():
            if len(messages) == 1:
                fact_text = f"[reconstructed from git] {messages[0]}"
            else:
                summary_text = f"{len(messages)} commits"
                sample = "; ".join(m[:60] for m in messages[:3])
                if len(messages) > 3:
                    sample += f" (+{len(messages) - 3} more)"
                fact_text = f"[reconstructed from git] {prefix}: {summary_text} — {sample}"

            entry = self.learn(
                fact_text,
                confidence=0.6,
                project=project,
                source="reconstructed",
            )
            facts_created.append(entry)

        return facts_created

    def catchup_manual(self, facts: list[str],
                       project: str = "global") -> list[dict]:
        """Store manually provided catchup facts with reconstructed provenance."""
        created = []
        for fact_text in facts:
            entry = self.learn(
                f"[reconstructed] {fact_text}",
                confidence=0.7,
                project=project,
                source="reconstructed",
            )
            created.append(entry)
        return created

    # ── Verification ──

    def verify_fact(self, query: str) -> dict | None:
        """Find the best matching fact and mark it as verified."""
        results = self.recall(query, limit=1, include_mistakes=False, _emit_event=False)
        if not results:
            return None

        fact = results[0]
        fact_id = fact.get("id", fact.get("content_hash"))
        if fact_id:
            sid = self._current_session_id()
            self.db.verify_fact(fact_id, sid)
            self.db.conn.commit()
            # Return updated entry
            return self.db.get_fact_by_id(fact_id)
        return None

    # ── Export / Import ──

    # Stable-id dedup keys per entity kind (issue #26). Facts carry a
    # content-hash id natively; decisions/mistakes/reflections only have
    # machine-local AUTOINCREMENT rowids, so exports stamp a content-hash
    # "uid" over these grouping keys (the exact keys used in the live
    # 2026-06-11 duplicate-merge repair). Import dedups by uid; legacy
    # export files without uid fall back to the same hash computed from
    # the row content — old exports stay importable.
    _UID_KEYS: ClassVar[dict[str, tuple[str, ...]]] = {
        "decisions": ("decision", "reasoning", "created_at"),
        "mistakes": ("mistake", "why", "created_at"),
        "reflections": ("went_well", "missed", "do_differently", "project"),
    }

    @classmethod
    def _entity_uid(cls, kind_key: str, entry: dict, personality: str) -> str:
        """Content-hash uid for a decision/mistake/reflection row.
        ``kind_key`` is the export-format key (decisions/mistakes/
        reflections). The personality is part of the grouping key —
        callers must pass the personality the row is (or will be)
        stored under so both sides of a dedup hash identically."""
        parts: list[str] = []
        for field_name in cls._UID_KEYS[kind_key]:
            value = entry.get(field_name) or ""
            if field_name == "project":
                value = (value or "global").strip().lower()
            parts.append(str(value))
        parts.append(personality or "")
        raw = json.dumps(parts, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _with_uids(self, kind_key: str, entries: list[dict]) -> list[dict]:
        """Stamp each exported row with its stable uid (issue #26)."""
        out = []
        for entry in entries:
            entry = dict(entry)
            entry["uid"] = self._entity_uid(
                kind_key, entry,
                entry.get("personality") or self.personality,
            )
            out.append(entry)
        return out

    def export_all(self) -> dict:
        """Export full memory as a portable dict."""
        return {
            "version": "2.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "identity": self.identity,
            "knowledge": self.db.get_all_facts(),
            "decisions": self._with_uids("decisions", self.db.get_decisions()),
            "mistakes": self._with_uids("mistakes", self.db.get_mistakes()),
            "reflections": self._with_uids(
                "reflections", self.db.get_reflections()),
            "projects": self.projects,
        }

    # Canonical entity-kind names (singular, matching CLI/docs usage)
    # → export-format keys. Plurals and the legacy "knowledge" key are
    # accepted as aliases. (ClassVar — not a dataclass field.)
    EXPORT_KINDS: ClassVar[dict[str, str]] = {
        "fact": "knowledge",
        "decision": "decisions",
        "mistake": "mistakes",
        "reflection": "reflections",
    }

    @staticmethod
    def _is_code_word_fact(entry: dict) -> bool:
        """Match the identity code-word fact by the same criteria as
        identity_payload._fetch_code_word: the code_word anchor label,
        falling back to the descriptive "code word:" text pattern for
        legacy stores."""
        if entry.get("anchor_type") == "code_word":
            return True
        return "code word:" in (entry.get("fact") or "").lower()

    def export_scoped(self, projects: list[str] | None = None,
                      kinds: list[str] | None = None,
                      include_identity: bool = False,
                      since: str | None = None) -> dict:
        """Scoped export — the onboarding-packet v0 (ORG_TOPOLOGY.md).

        Same wire format as export_all() (so `null import` works
        unchanged), filtered down to a working set:

          projects: only entities whose project is in this list
          kinds: only these entity kinds (fact/decision/mistake/
              reflection; plurals accepted); excluded kinds export as
              empty lists so the shape stays stable
          include_identity: identity content (identity dict, anchored
              facts, the code-word fact) is EXCLUDED by default — a
              spoke receives zero identity content. Setting this True
              re-includes it; callers must warn loudly if the code word
              is present.
          since: only entities created at/after this point (same syntax
              as recall's since: ISO8601, "7d", "yesterday", ...)

        Adds a "packet" metadata block (generated-at, source
        personality, filters applied); import_from() only reads known
        keys, so the extra block is ignored on the receiving side.
        """
        # Normalize filters
        proj_set = {p.strip().lower() for p in projects if p.strip()} if projects else None
        if kinds is not None:
            wanted_keys = set()
            for k in kinds:
                name = k.strip().lower()
                canonical = name[:-1] if name.endswith("s") and name != "knowledge" else name
                if name == "knowledge":
                    canonical = "fact"
                key = self.EXPORT_KINDS.get(canonical)
                if key is None:
                    raise ValueError(
                        f"unknown kind {k!r} — expected one of: "
                        f"{', '.join(self.EXPORT_KINDS)}"
                    )
                wanted_keys.add(key)
        else:
            wanted_keys = set(self.EXPORT_KINDS.values())

        since_dt = self._parse_since(since) if since else None
        since_str = since_dt.isoformat() if since_dt else None

        def _keep(entry: dict) -> bool:
            if proj_set is not None and (entry.get("project") or "global").lower() not in proj_set:
                return False
            if since_str and (entry.get("created_at") or "") < since_str:
                return False
            return True

        facts = []
        if "knowledge" in wanted_keys:
            for entry in self.db.get_all_facts():
                if not _keep(entry):
                    continue
                if not include_identity:
                    # Zero identity content: no anchors, and the code
                    # word NEVER leaves the hub without the override.
                    if entry.get("anchor_type") or self._is_code_word_fact(entry):
                        continue
                facts.append(entry)

        decisions = self._with_uids(
            "decisions", [e for e in self.db.get_decisions() if _keep(e)]) \
            if "decisions" in wanted_keys else []
        mistakes = self._with_uids(
            "mistakes", [e for e in self.db.get_mistakes() if _keep(e)]) \
            if "mistakes" in wanted_keys else []
        reflections = self._with_uids(
            "reflections", [e for e in self.db.get_reflections() if _keep(e)]) \
            if "reflections" in wanted_keys else []

        if proj_set is not None:
            projects_meta = {
                name: data for name, data in self.projects.items()
                if name.strip().lower() in proj_set
            }
        else:
            projects_meta = self.projects

        code_word_count = sum(1 for e in facts if self._is_code_word_fact(e))

        key_to_kind = {v: k for k, v in self.EXPORT_KINDS.items()}

        return {
            "version": "2.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "packet": {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source_personality": self.personality,
                "source_name": self.name,
                "filters": {
                    "projects": sorted(proj_set) if proj_set is not None else None,
                    "kinds": sorted(key_to_kind[k] for k in wanted_keys),
                    "since": since,
                    "include_identity": include_identity,
                },
                "code_word_count": code_word_count,
            },
            "identity": self.identity if include_identity else {},
            "knowledge": facts,
            "decisions": decisions,
            "mistakes": mistakes,
            "reflections": reflections,
            "projects": projects_meta,
        }

    def _existing_uids(self, kind_key: str) -> set[str]:
        """Hash this store's existing rows of a kind with the same
        grouping keys imports use (issue #26). Rows are hashed with the
        personality they are stored under (the scoped reads already
        filter unified stores to this connection's personality)."""
        if kind_key == "decisions":
            rows = self.db.get_decisions()
        elif kind_key == "mistakes":
            rows = self.db.get_mistakes(include_archived=True)
        else:
            rows = self.db.get_reflections(include_archived=True)
        return {
            self._entity_uid(
                kind_key, r, r.get("personality") or self.db.personality)
            for r in rows
        }

    def _incoming_uid(self, kind_key: str, entry: dict) -> str:
        """uid an incoming export row will be stored under. Trust the
        export-stamped uid only when it was minted for the same
        personality this store records (inserts always attribute rows to
        the importing connection's personality); otherwise recompute so
        re-imports stay idempotent."""
        store_personality = self.db.personality
        uid = entry.get("uid")
        if uid and (entry.get("personality") or store_personality) == store_personality:
            return uid
        # Legacy export without uid (or cross-personality import):
        # content-hash fallback over the same grouping keys.
        return self._entity_uid(kind_key, entry, store_personality)

    @staticmethod
    def format_import_report(counts: dict[str, dict[str, int]]) -> str:
        """One-line per-kind imported/skipped report (issue #26)."""
        parts = []
        for kind_key, label in (("knowledge", "facts"),
                                ("decisions", "decisions"),
                                ("mistakes", "mistakes"),
                                ("reflections", "reflections")):
            c = counts.get(kind_key, {"imported": 0, "skipped": 0})
            parts.append(
                f"{c['imported']} new {label} "
                f"({c['skipped']} already present)"
            )
        return "Imported: " + ", ".join(parts)

    @classmethod
    def import_from(cls, data: dict, agent_dir: str | None = None) -> AgentMemory:
        """Import memory from a portable export.

        Idempotent for EVERY entity kind (issue #26): rows whose stable
        id already exists in this store are skipped — facts by their
        content-hash id, decisions/mistakes/reflections by the export
        "uid" (content-hash fallback for legacy export files without
        uids). The live incident this fixes: the first real two-store
        merge deduped facts but blindly re-inserted decisions (213→424),
        mistakes (46→92), and reflections (115→230).

        Per-kind counts land on the returned instance as
        ``last_import_counts`` ({kind: {"imported": n, "skipped": m}});
        ``format_import_report`` renders them.
        """
        if agent_dir is None:
            # Honor NULL_DIR like load() does — without this, a sandboxed
            # `null import` would write to the real ~/.null.
            agent_dir = os.environ.get(
                "NULL_DIR", os.path.join(os.path.expanduser("~"), ".null"))

        os.makedirs(agent_dir, exist_ok=True)

        mem = cls(agent_dir=agent_dir)
        packet_identity = data.get("identity") or {}
        if packet_identity:
            mem.identity = packet_identity
            mem.save_identity()
        else:
            # Knowledge-only packet (scoped export with
            # include_identity=False — the onboarding-packet flow): the
            # sender's identity is deliberately absent, so the recipient's
            # identity.json must survive untouched. Live incident: athena's
            # onboarded identity (working_style, escalation rules, color)
            # was wiped to {} by importing her own onboarding packet.
            ident_path = os.path.join(agent_dir, "identity.json")
            if os.path.isfile(ident_path):
                try:
                    with open(ident_path, "r", encoding="utf-8") as f:
                        mem.identity = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

        counts: dict[str, dict[str, int]] = {
            k: {"imported": 0, "skipped": 0}
            for k in ("knowledge", "decisions", "mistakes", "reflections")
        }

        # Import knowledge — facts have stable content-hash ids natively.
        seen_fact_ids = {
            r[0] for r in mem.db.conn.execute("SELECT id FROM facts")
        }
        for entry in data.get("knowledge", []):
            c_hash = entry.get("content_hash", entry.get("id", ""))
            proj = entry.get("project", "global")
            if not c_hash:
                c_hash = cls._content_hash(entry.get("fact", ""), proj)
            if c_hash in seen_fact_ids:
                counts["knowledge"]["skipped"] += 1
                continue
            seen_fact_ids.add(c_hash)
            counts["knowledge"]["imported"] += 1
            ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
            mem.db.insert_fact({
                "id": c_hash,
                "fact": entry.get("fact", ""),
                "confidence": entry.get("confidence", 0.8),
                "base_confidence": entry.get("base_confidence", entry.get("confidence", 0.8)),
                "project": entry.get("project", "global"),
                "source": entry.get("source", "observation"),
                "provenance": entry.get("provenance", entry.get("source", "observation")),
                "impact": entry.get("impact", 0.5),
                "session_id": entry.get("session_id"),
                "created_at": ts,
                "last_accessed": entry.get("last_accessed"),
                "access_count": entry.get("access_count", 0),
                "last_verified": entry.get("last_verified"),
                "verified_by": entry.get("verified_by"),
                "superseded_by": entry.get("superseded_by"),
                "archived": entry.get("archived", False),
            })

        # Import decisions
        seen_uids = mem._existing_uids("decisions")
        for entry in data.get("decisions", []):
            uid = mem._incoming_uid("decisions", entry)
            if uid in seen_uids:
                counts["decisions"]["skipped"] += 1
                continue
            seen_uids.add(uid)
            counts["decisions"]["imported"] += 1
            ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
            mem.db.insert_decision({
                "decision": entry.get("decision", ""),
                "reasoning": entry.get("reasoning", ""),
                "project": entry.get("project", "global"),
                "session_id": entry.get("session_id"),
                "created_at": ts,
            })

        # Import mistakes
        seen_uids = mem._existing_uids("mistakes")
        for entry in data.get("mistakes", []):
            uid = mem._incoming_uid("mistakes", entry)
            if uid in seen_uids:
                counts["mistakes"]["skipped"] += 1
                continue
            seen_uids.add(uid)
            counts["mistakes"]["imported"] += 1
            ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
            mem.db.insert_mistake({
                "mistake": entry.get("mistake", ""),
                "why": entry.get("why", ""),
                "project": entry.get("project", "global"),
                "session_id": entry.get("session_id"),
                "created_at": ts,
            })

        # Import reflections
        seen_uids = mem._existing_uids("reflections")
        for entry in data.get("reflections", []):
            uid = mem._incoming_uid("reflections", entry)
            if uid in seen_uids:
                counts["reflections"]["skipped"] += 1
                continue
            seen_uids.add(uid)
            counts["reflections"]["imported"] += 1
            ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
            mem.db.insert_reflection({
                "went_well": entry.get("went_well", ""),
                "missed": entry.get("missed", ""),
                "do_differently": entry.get("do_differently", ""),
                "project": entry.get("project", "global"),
                "session_id": entry.get("session_id"),
                "created_at": ts,
            })

        mem.db.conn.commit()

        # Write projects
        mem.projects = data.get("projects", {})
        mem._ensure_dir()
        for name, proj_data in mem.projects.items():
            safe_name = cls._sanitize_name(name)
            path = os.path.join(agent_dir, "projects", f"{safe_name}.json")
            mem._atomic_write_json(path, proj_data)

        mem.last_import_counts = counts
        return mem

    # ── Status ──

    def status(self) -> str:
        """Return memory stats."""
        lines = [f"[Null] {self.name} — Memory Status"]
        lines.append(f"  Facts: {self.db.count_facts()}")
        lines.append(f"  Mistakes: {self.db.count_mistakes()}")
        lines.append(f"  Reflections: {self.db.count_reflections()}")
        lines.append(f"  Decisions: {self.db.count_decisions()}")
        lines.append(f"  Projects: {', '.join(self.projects.keys()) or 'none'}")
        lines.append(f"  Turns this session: {self._turn_count}")
        lines.append(f"  Token budget: {self._token_budget_used}/{self.token_budget} used")

        # Tier breakdown
        diag = self.db.diagnose()
        tiers = diag.get("tiers", {})
        if tiers:
            tier_parts = [f"{t}={c}" for t, c in sorted(tiers.items())]
            lines.append(f"  Tiers: {', '.join(tier_parts)}")

        # Show archived/forgotten counts
        total = self.db.count_facts(active_only=False)
        active = self.db.count_facts(active_only=True)
        if total > active:
            lines.append(f"  Archived/forgotten: {total - active}")

        # Outcome tracking
        outcome_count = self.db.count_outcomes()
        if outcome_count > 0:
            lines.append(f"  Decision outcomes: {outcome_count}")

        # Embedding stats
        emb = self.embeddings
        if emb is not None:
            stats = emb.stats()
            active = self.db.count_facts()
            embedded = stats["total_embeddings"]
            pct = (embedded / active * 100) if active > 0 else 0
            lines.append(f"  Embeddings: {embedded}/{active} ({pct:.0f}%) — {stats['model_name']}")
        else:
            lines.append("  Embeddings: disabled (pip install null-memory[embeddings])")

        # Swallowed embedding failures (P0-6) — silent semantic degradation
        embed_failures = int(self.db.get_meta("embed_failures") or 0)
        if embed_failures:
            last = self.db.get_meta("embed_failures_last") or "unknown"
            lines.append(f"  Embedding failures: {embed_failures} (last: {last})")

        # Sync error status
        sync_log = os.path.join(self.agent_dir, "sync_errors.log")
        if os.path.isfile(sync_log):
            try:
                with open(sync_log, "r", encoding="utf-8") as f:
                    error_lines = f.readlines()
                if error_lines:
                    lines.append(f"  Sync errors: {len(error_lines)} (see ~/.null/sync_errors.log)")
            except OSError:
                pass

        # Instance presence — who else is live on this store
        inst_line = self.instances_line()
        if inst_line:
            lines.append(f"  {inst_line}")

        return "\n".join(lines)

    # ── Diagnostics ──

    def diagnose(self) -> dict[str, Any]:
        """Run diagnostics on memory health."""
        findings = self.db.diagnose()
        findings["embed_failures"] = int(self.db.get_meta("embed_failures") or 0)
        findings["embed_failures_last"] = self.db.get_meta("embed_failures_last")
        return findings

    def fix_hygiene(self, dry_run: bool = False) -> dict[str, int]:
        """Fix common data quality issues."""
        return self.db.fix_hygiene(dry_run=dry_run)

