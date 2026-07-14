"""Null Multiverse — multi-personality memory architecture.

Central coordination layer for multiple Null personality instances.
Each personality is a full AgentMemory with its own memory.db, identity,
state, and momentum. The MultiverseManager coordinates them through
a shared SQLite registry (multiverse.db).

Atlas is the manager. All others are workers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Any

from null_memory.fsutil import force_rmtree


# ── Registry authority (issue #23) ─────────────────────────────────────
#
# Two registry mechanisms exist post unified-migration. The decision
# (approved, 2026-06-11):
#
#   1. The unified store's `personalities` table is AUTHORITATIVE for who
#      exists (name, role, focus, active). It carries NO paths — it is
#      portable, syncs with the store across machines, and is seeded /
#      repaired by the structural heal.
#   2. Seat directories are NEVER stored as the source of truth. They are
#      DERIVED at read time from the local hub base by convention — see
#      resolve_personality_dir(). State that syncs across machines must
#      never carry machine-local assumptions (multiverse.db once stored
#      machine-absolute dirs; after a macOS↔Windows sync every registered
#      dir was a dead path on the other OS).
#   3. multiverse.db is legacy/compat: still read (and written on
#      registration for back-compat), but its `dir` column is a HINT
#      only. New rows store the dir RELATIVE to the hub base; absolute
#      rows are legacy and are relativized opportunistically when
#      touched. A stored dir that does not exist locally falls back to
#      the derived conventional path — this self-heals the cross-machine
#      dead-path case.
#   4. Listing is the UNION: unified personalities table first, then
#      multiverse.db legacy rows it doesn't already cover — a seat is
#      visible once its row exists in EITHER place reachable locally.
#
# See also docs/design/ORG_TOPOLOGY.md (registry authority amendment).


def resolve_personality_dir(hub_base: str, name: str) -> str:
    """Derive a personality's store directory from the local hub base.

    The single convention every registry-dir-following path must use —
    every multiverse consumer (recall fan-out, broadcast, bootstrap,
    get_personality, list) routes here via ``_resolve_dir``. Candidates,
    in order — first existing wins:

      1. ``<hub>/<name>``                 — primary-personality layout
                                            (e.g. <hub>/atlas; generalizes
                                            AgentMemory.load()'s atlas
                                            special case to any primary)
      2. ``<hub>/personalities/<name>``   — worker-seat layout
      3. ``<hub>``                        — flat pre-migration layout,
                                            only when the hub base itself
                                            holds a store

    When none exists yet, returns (2) — the conventional location where
    create() would put a new seat.
    """
    primary = os.path.join(hub_base, name)
    if os.path.isdir(primary):
        return primary
    worker = os.path.join(hub_base, "personalities", name)
    if os.path.isdir(worker):
        return worker
    if os.path.isfile(os.path.join(hub_base, "memory.db")) or os.path.isfile(
        os.path.join(hub_base, "identity.json")
    ):
        return hub_base
    return worker


def _looks_machine_absolute(path: str) -> bool:
    """True for absolute paths from ANY platform (POSIX ``/…``, Windows
    drive ``C:\\…`` / ``C:/…``, UNC ``\\\\…``). os.path.isabs only
    understands the current platform, but multiverse.db rows travel
    across platforms — a Windows-origin row read on macOS must still be
    recognized as a (dead) absolute legacy path."""
    if os.path.isabs(path):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")


# ── Schema ──

MULTIVERSE_SCHEMA_VERSION = 1

_MULTIVERSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Personality registry
CREATE TABLE IF NOT EXISTS personalities (
    name TEXT PRIMARY KEY,
    dir TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'worker',
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    bootstrapped_from TEXT,
    description TEXT,
    focus TEXT
);

-- Cross-references: same event, multiple perspectives
CREATE TABLE IF NOT EXISTS xrefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS xref_facts (
    xref_id INTEGER REFERENCES xrefs(id) ON DELETE CASCADE,
    personality TEXT REFERENCES personalities(name),
    fact_id TEXT NOT NULL,
    PRIMARY KEY (xref_id, personality)
);

-- Broadcast log
CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    source TEXT NOT NULL,
    targets TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Dream output queue (Hypnos)
CREATE TABLE IF NOT EXISTS dreams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis TEXT NOT NULL,
    source_facts TEXT,
    confidence REAL DEFAULT 0.4,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_xref_facts_personality ON xref_facts(personality);
CREATE INDEX IF NOT EXISTS idx_broadcasts_source ON broadcasts(source);
CREATE INDEX IF NOT EXISTS idx_dreams_status ON dreams(status);
"""


class MultiverseDB:
    """SQLite connection manager for the multiverse registry."""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.db_path = os.path.join(base_dir, "multiverse.db")
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(self.base_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        self.conn.executescript(_MULTIVERSE_SCHEMA)
        existing = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(MULTIVERSE_SCHEMA_VERSION)),
            )
            self.conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# ── Files that belong to a personality ──

_PERSONALITY_FILES = [
    "memory.db", "memory.db-shm", "memory.db-wal",
    "identity.json", "state.json", "momentum.json",
    "simmering.jsonl", "watching.jsonl",
    "active_session.json", "migration_notes.txt",
]
# Also move any .bak, .merged, and backup files
_PERSONALITY_GLOBS = ["*.jsonl.bak", "*.jsonl.merged", "backup_*.json"]
_PERSONALITY_DIRS = ["sessions", "projects"]


class MultiverseManager:
    """Coordinates multiple Null personality instances."""

    def __init__(self, base_dir: str | None = None):
        self.base_dir = base_dir or os.environ.get(
            "NULL_DIR", os.path.join(os.path.expanduser("~"), ".null")
        )
        self._db = MultiverseDB(self.base_dir)
        self._db.initialize()
        self._personalities: dict[str, Any] = {}  # lazy AgentMemory cache

    @property
    def db(self) -> MultiverseDB:
        return self._db

    # ── Registry plumbing (issue #23 — see authority comment up top) ──

    def _unified_conn(self) -> sqlite3.Connection | None:
        """Open the hub's unified store (the AUTHORITATIVE personality
        registry) when it exists and has the personalities table.
        Fail-soft: any problem returns None and callers fall back to the
        legacy multiverse.db alone."""
        path = os.path.join(self.base_dir, "unified.db")
        if not os.path.isfile(path):
            return None
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='personalities'"
            ).fetchone()
            if has_table is None:
                conn.close()
                return None
            return conn
        except sqlite3.Error:
            return None

    def _unified_rows(self, name: str | None = None) -> list[dict]:
        """Read registry rows from the unified personalities table.
        [] when the hub has no unified store (or on any error)."""
        conn = self._unified_conn()
        if conn is None:
            return []
        try:
            if name is None:
                rows = conn.execute("SELECT * FROM personalities").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM personalities WHERE name = ?", (name,)
                ).fetchall()
            return [{k: r[k] for k in r.keys()} for r in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def _write_unified_registry_row(
        self, name: str, role: str, created_at: str,
        bootstrap_from: str | None, description: str, focus: str,
    ) -> None:
        """Write-through to the authoritative registry (no dir — it
        carries no paths by design). Fail-soft: registration must never
        fail because the hub predates the unified store."""
        conn = self._unified_conn()
        if conn is None:
            return
        try:
            conn.execute(
                """INSERT INTO personalities
                   (name, role, active, created_at, bootstrapped_from,
                    description, focus)
                   VALUES (?, ?, 1, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     role = excluded.role,
                     active = 1,
                     bootstrapped_from = excluded.bootstrapped_from,
                     description = excluded.description,
                     focus = excluded.focus""",
                (name, role, created_at, bootstrap_from, description, focus),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _resolve_dir(self, name: str, stored: str | None) -> str:
        """Resolve a personality's local store dir. The stored
        multiverse.db value is a HINT only: relative hints join against
        the local hub base; absolute hints are honored only when they
        exist locally. Anything else derives the conventional path —
        which self-heals dead machine-absolute rows synced from another
        machine."""
        if stored:
            if _looks_machine_absolute(stored):
                if os.path.isdir(stored):
                    return os.path.normpath(stored)
            else:
                path = os.path.normpath(os.path.join(self.base_dir, stored))
                if os.path.isdir(path):
                    return path
        return resolve_personality_dir(self.base_dir, name)

    def _relativize_stored_dir(self, name: str, stored: str | None) -> None:
        """Opportunistically rewrite a legacy absolute multiverse.db dir
        as hub-relative (forward slashes — the file syncs across
        platforms). A dead absolute path becomes the derived conventional
        path. Absolute paths outside the hub are left alone (deliberate
        out-of-tree placement). Fail-soft."""
        if not stored or not _looks_machine_absolute(stored):
            return
        try:
            if os.path.isdir(stored):
                rel = os.path.relpath(stored, self.base_dir)
                if rel.startswith(".."):
                    return
            else:
                rel = os.path.relpath(
                    resolve_personality_dir(self.base_dir, name),
                    self.base_dir,
                )
                if rel.startswith(".."):
                    return
            self.db.conn.execute(
                "UPDATE personalities SET dir = ? WHERE name = ?",
                (rel.replace(os.sep, "/"), name),
            )
            self.db.conn.commit()
        except (sqlite3.Error, ValueError, OSError):
            pass

    # ── Personality Management ──

    def register(
        self,
        name: str,
        role: str = "worker",
        description: str = "",
        focus: str = "",
        bootstrap_from: str | None = None,
        directory: str | None = None,
    ) -> dict:
        """Register a personality in BOTH registries: the authoritative
        unified personalities table (portable — no paths) and the legacy
        multiverse.db (back-compat — dir stored RELATIVE to the hub base
        going forward; see the registry-authority comment up top)."""
        now = datetime.now(timezone.utc).isoformat()
        if directory is None:
            if name == "atlas":
                directory = os.path.join(self.base_dir, "atlas")
            else:
                directory = os.path.join(self.base_dir, "personalities", name)

        stored_dir = directory
        try:
            rel = os.path.relpath(directory, self.base_dir)
            if not rel.startswith(".."):
                stored_dir = rel.replace(os.sep, "/")
        except ValueError:
            pass  # different drive (Windows) — keep as given

        self.db.conn.execute(
            """INSERT OR REPLACE INTO personalities
               (name, dir, role, active, created_at, bootstrapped_from, description, focus)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
            (name, stored_dir, role, now, bootstrap_from, description, focus),
        )
        self.db.conn.commit()

        # Authoritative registry write-through (fail-soft no-op when the
        # hub has no unified store yet).
        self._write_unified_registry_row(
            name, role, now, bootstrap_from, description, focus)

        return {
            "name": name, "dir": os.path.normpath(directory), "role": role,
            "created_at": now, "bootstrapped_from": bootstrap_from,
            "description": description, "focus": focus,
        }

    def get_personality_info(self, name: str) -> dict | None:
        """Get a personality's registry entry (union read, issue #23).

        The unified personalities table is authoritative for existence /
        role / focus / active; the multiverse.db row contributes legacy
        metadata and a dir hint. The returned ``dir`` is always a
        locally-resolved absolute path (derived by convention when the
        hint is missing or dead)."""
        row = self.db.conn.execute(
            "SELECT * FROM personalities WHERE name = ?", (name,)
        ).fetchone()
        legacy = dict(row) if row else None

        unified_rows = self._unified_rows(name)
        unified = unified_rows[0] if unified_rows else None

        if unified is not None:
            if not unified.get("active"):
                return None
            info = dict(legacy or {})
            info.update(unified)  # authoritative fields win
        else:
            if legacy is None or not legacy.get("active"):
                return None
            info = dict(legacy)

        stored = legacy.get("dir") if legacy else None
        if legacy is not None:
            self._relativize_stored_dir(name, stored)
        info["dir"] = self._resolve_dir(name, stored)
        return info

    def get_personality(self, name: str) -> Any:
        """Get or lazily load an AgentMemory instance for a personality."""
        if name in self._personalities:
            return self._personalities[name]

        info = self.get_personality_info(name)
        if info is None:
            raise ValueError(f"Personality '{name}' not found or inactive")

        from null_memory.agent import AgentMemory
        # Load AS the named personality — the default would be 'atlas',
        # which mislabels every write (instances rows, session records,
        # heal backfills) on a non-atlas store (init-path bleed audit).
        mem = AgentMemory.load(agent_dir=info["dir"], personality=name)
        self._personalities[name] = mem
        return mem

    def list_personalities(self, include_inactive: bool = False) -> list[dict]:
        """List all registered personalities — the UNION (issue #23):
        the authoritative unified personalities table first, then legacy
        multiverse.db rows it doesn't already cover. A seat is visible
        once its row exists in EITHER place reachable locally. Every
        entry carries a locally-derived absolute ``dir``."""
        legacy_by_name: dict[str, dict] = {
            r["name"]: dict(r)
            for r in self.db.conn.execute(
                "SELECT * FROM personalities"
            ).fetchall()
        }

        merged: list[dict] = []
        seen: set[str] = set()
        for row in self._unified_rows():
            entry = dict(legacy_by_name.get(row["name"], {}))
            entry.update(row)  # authoritative fields win
            merged.append(entry)
            seen.add(row["name"])
        for name, legacy in legacy_by_name.items():
            if name not in seen:
                merged.append(dict(legacy))

        out: list[dict] = []
        for entry in merged:
            if not include_inactive and not entry.get("active"):
                continue
            name = entry["name"]
            stored = legacy_by_name.get(name, {}).get("dir")
            if name in legacy_by_name:
                self._relativize_stored_dir(name, stored)
            entry["dir"] = self._resolve_dir(name, stored)
            out.append(entry)
        return out

    def archive(self, name: str) -> bool:
        """Deactivate a personality (keeps data, removes from active rotation)."""
        if name == "atlas":
            raise ValueError("Cannot archive the manager personality")
        cursor = self.db.conn.execute(
            "UPDATE personalities SET active = 0 WHERE name = ?", (name,)
        )
        self.db.conn.commit()
        # Mirror into the authoritative registry (fail-soft).
        unified_changed = 0
        conn = self._unified_conn()
        if conn is not None:
            try:
                unified_changed = conn.execute(
                    "UPDATE personalities SET active = 0 WHERE name = ?",
                    (name,),
                ).rowcount
                conn.commit()
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        self._personalities.pop(name, None)
        return cursor.rowcount > 0 or unified_changed > 0

    def delete(self, name: str, remove_files: bool = False) -> bool:
        """Remove a personality entirely."""
        if name == "atlas":
            raise ValueError("Cannot delete the manager personality")
        info = self.get_personality_info(name)
        if info is None:
            return False

        self.db.conn.execute("DELETE FROM xref_facts WHERE personality = ?", (name,))
        self.db.conn.execute("DELETE FROM personalities WHERE name = ?", (name,))
        self.db.conn.commit()
        # Mirror into the authoritative registry (fail-soft).
        conn = self._unified_conn()
        if conn is not None:
            try:
                conn.execute(
                    "DELETE FROM personalities WHERE name = ?", (name,))
                conn.commit()
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        self._personalities.pop(name, None)

        # Never rmtree the hub base itself — a derived dir can resolve to
        # the flat hub store as a last resort (resolve_personality_dir).
        target = info["dir"]
        if (remove_files and target and os.path.isdir(target)
                and os.path.normpath(target)
                != os.path.normpath(self.base_dir)):
            # A personality store is a git repo; bare rmtree raises
            # PermissionError on Windows on git's read-only loose objects.
            force_rmtree(target, missing_ok=False)
        return True

    # ── Personality Creation ──

    def create(
        self,
        name: str,
        role: str = "worker",
        description: str = "",
        focus: str = "",
        bootstrap_from: str | None = None,
        seed_filter: str | None = None,
    ) -> dict:
        """Create a new personality with optional bootstrap from an existing one."""
        if name == "atlas":
            raise ValueError("Cannot create a personality named 'atlas' — it's reserved")

        existing = self.get_personality_info(name)
        if existing:
            raise ValueError(f"Personality '{name}' already exists")

        # Create directory
        personality_dir = os.path.join(self.base_dir, "personalities", name)
        os.makedirs(personality_dir, exist_ok=True)
        os.makedirs(os.path.join(personality_dir, "sessions"), exist_ok=True)
        os.makedirs(os.path.join(personality_dir, "projects"), exist_ok=True)

        # Generate identity.json
        now = datetime.now(timezone.utc).isoformat()
        identity = {
            "version": "1.0",
            "name": name,
            "role": role,
            "focus": focus,
            "description": description,
            "working_style": {},
            "capabilities": [],
            "anti_patterns": [],
            "created_at": now,
            "updated_at": now,
            "bootstrapped_from": bootstrap_from,
        }
        with open(os.path.join(personality_dir, "identity.json"), "w") as f:
            json.dump(identity, f, indent=2)

        # Initialize empty state/momentum
        with open(os.path.join(personality_dir, "state.json"), "w") as f:
            json.dump({"energy": "medium", "assessment": "", "concerns": [],
                        "optimistic_about": [], "unresolved": "", "written": now}, f, indent=2)
        with open(os.path.join(personality_dir, "momentum.json"), "w") as f:
            json.dump({"active_project": "", "last_decision": "", "next_action": "",
                        "blocked_on": "", "session_summary": "", "updated": now}, f, indent=2)

        # Initialize memory.db via NullDB — AS this personality, never the
        # 'atlas' default (init-path bleed audit: the default personality
        # parameterizes heal backfills and registry seeding).
        from null_memory.db import NullDB
        db = NullDB(personality_dir, personality=name)
        db.initialize()

        # A worker seat's store must know who it is without consulting the
        # hub: ratchet the fresh store to the unified per-store layout
        # (personalities table + personality columns), parameterized by
        # THIS personality. The heal/upgrade path seeds the store's own
        # registry row — never an 'atlas' row (ORG_TOPOLOGY genericity
        # requirement). Stamping UNIFIED_SCHEMA_VERSION keeps future
        # NullDB.initialize() calls on the heal path (issue #3) instead of
        # the legacy migration ladder.
        from null_memory.migrate_v3 import (
            UNIFIED_SCHEMA_VERSION,
            _apply_unified_upgrades,
        )
        _apply_unified_upgrades(db.conn, default_personality=name)
        db.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('schema_version', ?)",
            (str(UNIFIED_SCHEMA_VERSION),),
        )
        # The seat's own registry row carries the caller's role/focus —
        # the heal seeds role='worker' by default; make the requested
        # values authoritative.
        db.conn.execute(
            """INSERT INTO personalities
               (name, role, active, created_at, description, focus)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 role = excluded.role,
                 description = excluded.description,
                 focus = excluded.focus""",
            (name, role, now, description, focus),
        )
        db.conn.commit()

        # Bootstrap facts from source personality
        bootstrapped_count = 0
        if bootstrap_from:
            bootstrapped_count = self._bootstrap_facts(
                source=bootstrap_from, target_dir=personality_dir,
                seed_filter=seed_filter, target_personality=name,
            )

        db.close()

        # Register in multiverse
        info = self.register(
            name=name, role=role, description=description,
            focus=focus, bootstrap_from=bootstrap_from,
            directory=personality_dir,
        )
        info["bootstrapped_facts"] = bootstrapped_count
        return info

    def _bootstrap_facts(
        self, source: str, target_dir: str, seed_filter: str | None = None,
        target_personality: str | None = None,
    ) -> int:
        """Copy facts from source personality to target, with reduced confidence."""
        source_mem = self.get_personality(source)
        all_facts = source_mem.db.get_active_facts()

        # Apply seed filter if specified (format: "project:proj1,proj2")
        if seed_filter:
            filters = {}
            for part in seed_filter.split(","):
                part = part.strip()
                if ":" in part:
                    key, val = part.split(":", 1)
                    filters.setdefault(key.strip(), []).append(val.strip())
                else:
                    filters.setdefault("project", []).append(part)

            if "project" in filters:
                projects = {p.lower() for p in filters["project"]}
                all_facts = [
                    f for f in all_facts
                    if f.get("project", "global").lower() in projects
                ]

        # Write to target's memory.db — scoped to the TARGET personality,
        # not the 'atlas' default (init-path bleed audit).
        from null_memory.db import NullDB
        target_db = NullDB(
            target_dir,
            personality=target_personality or os.path.basename(
                target_dir.rstrip(os.sep)),
        )
        target_db.initialize()

        count = 0
        for fact in all_facts:
            entry = dict(fact)
            entry["confidence"] = min(entry.get("confidence", 0.8), 0.6)
            entry["base_confidence"] = entry["confidence"]
            entry["provenance"] = "bootstrap"
            entry["source"] = "bootstrap"
            entry["access_count"] = 0
            entry["last_accessed"] = None
            entry["last_verified"] = None
            entry["verified_by"] = None
            entry["superseded_by"] = None
            entry["forgotten"] = 0
            entry["archived"] = 0
            target_db.insert_fact(entry)
            count += 1

        target_db.conn.commit()
        target_db.close()
        return count

    # ── Broadcast ──

    def broadcast(
        self,
        event: str,
        source: str = "atlas",
        targets: list[str] | None = None,
    ) -> dict:
        """Broadcast an event to one or more personalities.

        Each target personality records the event through their own observe().
        Returns xref mapping of fact IDs across personalities.

        Events are redacted before replication (P2-17): credential-shaped
        strings and deployment shared secrets are stripped, and an optional
        broadcast classifier can veto the whole event.
        """
        from null_memory.redaction import redact, should_block_broadcast

        if should_block_broadcast(event):
            return {
                "event": None, "targets": [], "xref_id": None,
                "blocked": True,
                "reason": "broadcast classifier vetoed this event",
            }

        identity_terms = None
        try:
            identity_terms = self.get_personality(source).identity.get(
                "identity_terms")
        except (ValueError, KeyError):
            pass
        event, redacted_labels = redact(event, identity_terms)

        now = datetime.now(timezone.utc).isoformat()

        if targets is None:
            # Broadcast to all active workers (union read — issue #23)
            targets = [
                p["name"] for p in self.list_personalities()
                if p.get("role") == "worker"
            ]

        if not targets:
            return {"event": event, "targets": [], "xref_id": None}

        # Log the broadcast
        self.db.conn.execute(
            "INSERT INTO broadcasts (event, source, targets, created_at) VALUES (?, ?, ?, ?)",
            (event, source, json.dumps(targets), now),
        )

        # Create xref entry
        cursor = self.db.conn.execute(
            "INSERT INTO xrefs (event, created_at) VALUES (?, ?)", (event, now)
        )
        xref_id = cursor.lastrowid

        # Record source fact if source personality exists
        fact_ids = {}
        source_mem = None
        try:
            source_mem = self.get_personality(source)
            result = source_mem.observe(event, project="global")
            if result:
                fact_ids[source] = result["id"]
                self.db.conn.execute(
                    "INSERT INTO xref_facts (xref_id, personality, fact_id) VALUES (?, ?, ?)",
                    (xref_id, source, result["id"]),
                )
        except (ValueError, KeyError):
            pass

        # Broadcast to each target
        for target_name in targets:
            try:
                target_mem = self.get_personality(target_name)
                result = target_mem.observe(event, project="global")
                if result:
                    fact_ids[target_name] = result["id"]
                    self.db.conn.execute(
                        "INSERT INTO xref_facts (xref_id, personality, fact_id) VALUES (?, ?, ?)",
                        (xref_id, target_name, result["id"]),
                    )
            except (ValueError, KeyError):
                continue

        self.db.conn.commit()

        # Event-sourced sync (issue #20): the broadcast itself is evented
        # on the source personality's writer log (the per-personality fact
        # replications above each emit their own fact.add through observe()).
        if source_mem is not None:
            try:
                source_mem._emit_store_event("broadcast", xref_id, {
                    "event": event,
                    "source": source,
                    "targets": targets,
                })
            except Exception:  # noqa: BLE001
                pass  # event logging must never break a broadcast

        result = {"event": event, "targets": targets, "xref_id": xref_id, "fact_ids": fact_ids}
        if redacted_labels:
            result["redacted"] = redacted_labels
        return result

    # ── Cross-Personality Recall ──

    def recall(
        self,
        query: str,
        personalities: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Fan-out search across multiple personality databases."""
        if personalities is None:
            # Union read (issue #23) — seats registered in either registry
            personalities = [p["name"] for p in self.list_personalities()]

        all_results: list[dict] = []
        per_personality_limit = max(limit, 5)

        for name in personalities:
            try:
                mem = self.get_personality(name)
                results = mem.recall(query, limit=per_personality_limit)
                for r in results:
                    r["_personality"] = name
                all_results.extend(results)
            except (ValueError, KeyError):
                continue

        # Sort by score descending, take top limit
        all_results.sort(
            key=lambda r: r.get("_score", r.get("confidence", 0.5)),
            reverse=True,
        )
        return all_results[:limit]

    # ── Multiverse Wakeup ──

    def wakeup(self) -> dict:
        """Synthesize state/momentum from all active personalities."""
        from null_memory.wakeup import load_state, load_momentum

        personalities = self.list_personalities()
        summaries = {}

        for p in personalities:
            name = p["name"]
            pdir = p["dir"]
            try:
                state = load_state(agent_dir=pdir)
                momentum = load_momentum(agent_dir=pdir)
                summaries[name] = {
                    "role": p["role"],
                    "focus": p.get("focus", ""),
                    "state": state,
                    "momentum": momentum,
                }
            except Exception:
                summaries[name] = {
                    "role": p["role"],
                    "focus": p.get("focus", ""),
                    "state": {},
                    "momentum": {},
                    "error": "Could not load state",
                }

        return summaries

    # ── Migration ──

    def migrate_flat_to_multiverse(self, dry_run: bool = False) -> dict:
        """Migrate flat ~/.null/ layout to multiverse structure.

        Moves personality files into ~/.null/atlas/ subdirectory.
        Keeps .git at the root level. Idempotent — detects if already migrated.
        """
        atlas_dir = os.path.join(self.base_dir, "atlas")
        result = {"already_migrated": False, "files_moved": [], "dirs_moved": [],
                  "backup_dir": None, "errors": []}

        # Check if already migrated
        if os.path.isdir(atlas_dir) and os.path.isfile(
            os.path.join(atlas_dir, "memory.db")
        ):
            result["already_migrated"] = True
            # Ensure atlas is registered
            if not self.get_personality_info("atlas"):
                self.register("atlas", role="manager", description="Atlas core — the manager",
                              directory=atlas_dir)
            return result

        # Check if there's anything to migrate
        has_flat_files = any(
            os.path.exists(os.path.join(self.base_dir, f))
            for f in _PERSONALITY_FILES
        )
        if not has_flat_files:
            result["errors"].append("No flat personality files found to migrate")
            return result

        if dry_run:
            import fnmatch
            result["files_moved"] = [
                f for f in _PERSONALITY_FILES
                if os.path.exists(os.path.join(self.base_dir, f))
            ]
            for pattern in _PERSONALITY_GLOBS:
                for fname in os.listdir(self.base_dir):
                    if fnmatch.fnmatch(fname, pattern) and os.path.isfile(
                        os.path.join(self.base_dir, fname)
                    ):
                        result["files_moved"].append(fname)
            result["dirs_moved"] = [
                d for d in _PERSONALITY_DIRS
                if os.path.isdir(os.path.join(self.base_dir, d))
            ]
            return result

        # Create backup
        backup_dir = f"{self.base_dir}_backup_{int(datetime.now().timestamp())}"
        try:
            shutil.copytree(
                self.base_dir, backup_dir,
                ignore=shutil.ignore_patterns(".git", "multiverse.db*"),
            )
            result["backup_dir"] = backup_dir
        except Exception as e:
            result["errors"].append(f"Backup failed: {e}")
            return result

        # Create atlas directory
        os.makedirs(atlas_dir, exist_ok=True)

        # Move personality files (explicit list)
        for fname in _PERSONALITY_FILES:
            src = os.path.join(self.base_dir, fname)
            if os.path.exists(src):
                dst = os.path.join(atlas_dir, fname)
                try:
                    shutil.move(src, dst)
                    result["files_moved"].append(fname)
                except Exception as e:
                    result["errors"].append(f"Failed to move {fname}: {e}")

        # Move glob-matched files (.bak, .merged, backup_*)
        import fnmatch
        for pattern in _PERSONALITY_GLOBS:
            for fname in os.listdir(self.base_dir):
                if fnmatch.fnmatch(fname, pattern):
                    src = os.path.join(self.base_dir, fname)
                    if os.path.isfile(src):
                        dst = os.path.join(atlas_dir, fname)
                        try:
                            shutil.move(src, dst)
                            result["files_moved"].append(fname)
                        except Exception as e:
                            result["errors"].append(f"Failed to move {fname}: {e}")

        # Move personality directories
        for dname in _PERSONALITY_DIRS:
            src = os.path.join(self.base_dir, dname)
            if os.path.isdir(src):
                dst = os.path.join(atlas_dir, dname)
                try:
                    if os.path.exists(dst):
                        # Merge contents
                        for item in os.listdir(src):
                            shutil.move(os.path.join(src, item), os.path.join(dst, item))
                        os.rmdir(src)
                    else:
                        shutil.move(src, dst)
                    result["dirs_moved"].append(dname)
                except Exception as e:
                    result["errors"].append(f"Failed to move {dname}: {e}")

        # Create personalities directory
        os.makedirs(os.path.join(self.base_dir, "personalities"), exist_ok=True)

        # Register atlas
        self.register(
            "atlas", role="manager",
            description="Atlas core — the manager",
            directory=atlas_dir,
        )

        return result

    # ── Dreams (Hypnos) ──

    def record_dream(self, hypothesis: str, source_facts: list[dict] | None = None,
                     confidence: float = 0.4) -> int:
        """Record a dream hypothesis from Hypnos."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.db.conn.execute(
            "INSERT INTO dreams (hypothesis, source_facts, confidence, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (hypothesis, json.dumps(source_facts or []), confidence, now),
        )
        self.db.conn.commit()
        return cursor.lastrowid

    def get_pending_dreams(self, limit: int = 10) -> list[dict]:
        """Get pending dream hypotheses."""
        rows = self.db.conn.execute(
            "SELECT * FROM dreams WHERE status = 'pending' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def promote_dream(self, dream_id: int) -> bool:
        """Promote a dream hypothesis to a real fact (via Atlas simmering)."""
        cursor = self.db.conn.execute(
            "UPDATE dreams SET status = 'promoted' WHERE id = ? AND status = 'pending'",
            (dream_id,),
        )
        self.db.conn.commit()
        return cursor.rowcount > 0

    def dismiss_dream(self, dream_id: int) -> bool:
        """Dismiss a dream hypothesis."""
        cursor = self.db.conn.execute(
            "UPDATE dreams SET status = 'dismissed' WHERE id = ? AND status = 'pending'",
            (dream_id,),
        )
        self.db.conn.commit()
        return cursor.rowcount > 0

    # ── Hypnos Dream Engine ──

    def dream(self, max_dreams: int = 5, env_file: str | None = None) -> list[dict]:
        """Run the Hypnos dream cycle.

        1. Collect recent facts from all active personalities
        2. Find tension pairs (similar topics, different conclusions/confidence)
        3. Generate hypotheses via LLM call
        4. Write dreams to multiverse.db + simmering queues
        """
        api_key = self._load_api_key(env_file)
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not found. Set it in environment, "
                "~/.env, or ~/Repos/.env"
            )

        # Collect recent facts from all personalities
        facts_by_personality = self._collect_dream_facts(days=7, min_impact=0.4)
        total_facts = sum(len(v) for v in facts_by_personality.values())
        if total_facts < 10:
            return []  # Not enough material to dream about

        # Build the dream prompt
        prompt = self._build_dream_prompt(facts_by_personality, max_dreams)

        # Call the LLM
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            temperature=0.9,  # Creative, unpredictable
            messages=[{"role": "user", "content": prompt}],
        )

        # Parse hypotheses from response
        raw_text = response.content[0].text
        hypotheses = self._parse_dream_response(raw_text)

        # Record dreams and write to simmering queues
        dreams = []
        for h in hypotheses[:max_dreams]:
            dream_id = self.record_dream(
                hypothesis=h["hypothesis"],
                source_facts=h.get("source_facts", []),
                confidence=0.4,
            )
            dream = {
                "id": dream_id,
                "hypothesis": h["hypothesis"],
                "source_facts": h.get("source_facts", []),
                "confidence": 0.4,
            }
            dreams.append(dream)

            # Write to simmering queues
            self._write_dream_to_simmering(h["hypothesis"], dream_id)

        # Log the dream run as a broadcast
        if dreams:
            now = datetime.now(timezone.utc).isoformat()
            self.db.conn.execute(
                "INSERT INTO broadcasts (event, source, targets, created_at) VALUES (?, ?, ?, ?)",
                (f"[hypnos] Dream cycle: {len(dreams)} hypotheses generated",
                 "hypnos", json.dumps(["atlas"]), now),
            )
            self.db.conn.commit()

        return dreams

    def _load_api_key(self, env_file: str | None = None) -> str | None:
        """Load ANTHROPIC_API_KEY from environment or .env files."""
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return key

        # Check common .env locations
        env_paths = [env_file] if env_file else []
        env_paths.extend([
            os.path.expanduser("~/Repos/.env"),
            os.path.expanduser("~/.env"),
            os.path.join(self.base_dir, ".env"),
        ])

        for path in env_paths:
            if path and os.path.isfile(path):
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ANTHROPIC_API_KEY="):
                            return line.split("=", 1)[1].strip().strip("'\"")
        return None

    def _collect_dream_facts(
        self, days: int = 7, min_impact: float = 0.4,
    ) -> dict[str, list[dict]]:
        """Collect recent facts from all active personalities."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        facts_by_personality: dict[str, list[dict]] = {}
        for p in self.list_personalities():
            name = p["name"]
            try:
                mem = self.get_personality(name)
                # Get active facts created after cutoff with sufficient impact
                all_facts = mem.db.get_active_facts()
                recent = [
                    f for f in all_facts
                    if f.get("created_at", "") >= cutoff
                    and (f.get("impact") or 0.5) >= min_impact
                ]
                if recent:
                    facts_by_personality[name] = recent
            except (ValueError, KeyError):
                continue

        return facts_by_personality

    def _build_dream_prompt(
        self, facts_by_personality: dict[str, list[dict]], max_dreams: int,
    ) -> str:
        """Build the prompt for Hypnos dream generation."""
        sections = []
        for name, facts in facts_by_personality.items():
            fact_lines = []
            for f in facts[:50]:  # Cap at 50 per personality
                conf = f.get("confidence", 0.5)
                proj = f.get("project", "global")
                fact_lines.append(f"  [{conf:.0%}] [{proj}] {f['fact'][:200]}")
            sections.append(f"## {name}\n" + "\n".join(fact_lines))

        facts_text = "\n\n".join(sections)

        return f"""You are Hypnos, the dream engine of a multi-personality memory system.

Below are recent facts from multiple personalities. Each personality has its own perspective and specialization. Your job is to find TENSIONS — places where:
- Two personalities recorded the same event differently
- Confidence levels diverge significantly on related topics
- Patterns appear across time that neither personality noticed
- Facts from different domains connect in non-obvious ways

Do NOT summarize. Do NOT repeat facts. Generate {max_dreams} original HYPOTHESES — things that might be true based on the tensions you find. These are dreams, not certainties. Be speculative. Be surprising. Occasionally be wrong.

Format each hypothesis on its own line, prefixed with [dream]:

[dream] <hypothesis text>

---

{facts_text}"""

    def _parse_dream_response(self, text: str) -> list[dict]:
        """Parse [dream] prefixed lines from LLM response."""
        hypotheses = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if line.startswith("[dream]"):
                hypothesis = line[len("[dream]"):].strip()
                if hypothesis:
                    hypotheses.append({"hypothesis": hypothesis, "source_facts": []})
        return hypotheses

    def _write_dream_to_simmering(self, hypothesis: str, dream_id: int) -> None:
        """Write a dream hypothesis to Atlas and Mnemosyne simmering queues."""
        from null_memory.wakeup import add_simmering

        dream_text = f"[dream #{dream_id}] {hypothesis}"

        # Write to Atlas simmering
        atlas_info = self.get_personality_info("atlas")
        if atlas_info:
            add_simmering(
                question=dream_text,
                context="Generated by Hypnos dream cycle",
                category="strategic",
                agent_dir=atlas_info["dir"],
            )

        # Write to Mnemosyne simmering if it exists
        mnemosyne_info = self.get_personality_info("mnemosyne")
        if mnemosyne_info:
            add_simmering(
                question=dream_text,
                context="Generated by Hypnos dream cycle",
                category="strategic",
                agent_dir=mnemosyne_info["dir"],
            )

    def close(self) -> None:
        """Close all connections."""
        for mem in self._personalities.values():
            try:
                mem.db.close()
            except Exception:
                pass
        self._personalities.clear()
        self.db.close()
