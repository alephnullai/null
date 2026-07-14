"""SQLite storage backend for Null Memory.

Handles schema creation, connection management, JSONL migration, and query helpers.
Uses FTS5 for full-text search with BM25 ranking, and trigram tokenizer for fuzzy matching.
WAL mode for cross-platform concurrent access.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator


# ── Schema ──

SCHEMA_VERSION = 14

# ── Instance presence registry ──
# An instance is "live" if its heartbeat is younger than this window.
INSTANCE_LIVE_WINDOW_MINUTES = 5
# Registration garbage-collects rows whose heartbeat is older than this.
INSTANCE_GC_DAYS = 7
# Bounded DELETE per registration — presence GC must stay cheap.
_INSTANCE_GC_BATCH = 500

# Created on demand (register_instance) so legacy per-personality stores
# get the table too; the unified schema carries the same definition in
# migrate_v3.SCHEMA_SQL and the structural heal path.
_INSTANCES_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS instances (
        instance_id TEXT PRIMARY KEY,
        hostname TEXT,
        pid INTEGER,
        started_at TEXT NOT NULL,
        last_heartbeat TEXT NOT NULL,
        personality TEXT,
        transport TEXT,
        project TEXT,
        schema_version_seen INTEGER
    )
"""
_INSTANCES_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_instances_heartbeat "
    "ON instances(last_heartbeat)"
)


def _UNIFIED_SCHEMA_VERSION() -> int:
    """Lazy accessor for migrate_v3.UNIFIED_SCHEMA_VERSION (avoids a
    module-level import cycle — migrate_v3 is imported inside methods
    throughout this module for the same reason)."""
    from null_memory.migrate_v3 import UNIFIED_SCHEMA_VERSION
    return UNIFIED_SCHEMA_VERSION

_SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Core knowledge
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    fact TEXT NOT NULL,
    confidence REAL DEFAULT 0.8,
    base_confidence REAL DEFAULT 0.8,
    project TEXT DEFAULT 'global',
    source TEXT DEFAULT 'observation',
    provenance TEXT DEFAULT 'observation',
    impact REAL DEFAULT 0.5,
    session_id TEXT,
    created_at TEXT NOT NULL,
    last_accessed TEXT,
    access_count INTEGER DEFAULT 0,
    last_verified TEXT,
    verified_by TEXT,
    superseded_by TEXT,
    forgotten INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    related_to TEXT DEFAULT '[]',
    tier TEXT DEFAULT 'contextual'
);

-- Relationship edges (P2-16): real edge table replacing the related_to
-- JSON column (kept for back-compat reads of old DBs; no longer written)
CREATE TABLE IF NOT EXISTS fact_edges (
    fact_id TEXT NOT NULL,
    related_id TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY (fact_id, related_id)
);
CREATE INDEX IF NOT EXISTS idx_fact_edges_related ON fact_edges(related_id);

-- Full-text search (keyword + BM25 ranking)
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact,
    content='facts',
    content_rowid='rowid',
    tokenize='unicode61'
);

-- Fuzzy/substring search (trigram)
CREATE VIRTUAL TABLE IF NOT EXISTS facts_trigram USING fts5(
    fact,
    content='facts',
    content_rowid='rowid',
    tokenize='trigram'
);

-- Triggers to keep FTS indexes in sync with facts table
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, fact) VALUES (new.rowid, new.fact);
    INSERT INTO facts_trigram(rowid, fact) VALUES (new.rowid, new.fact);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact) VALUES ('delete', old.rowid, old.fact);
    INSERT INTO facts_trigram(facts_trigram, rowid, fact) VALUES ('delete', old.rowid, old.fact);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE OF fact ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, fact) VALUES ('delete', old.rowid, old.fact);
    INSERT INTO facts_fts(rowid, fact) VALUES (new.rowid, new.fact);
    INSERT INTO facts_trigram(facts_trigram, rowid, fact) VALUES ('delete', old.rowid, old.fact);
    INSERT INTO facts_trigram(rowid, fact) VALUES (new.rowid, new.fact);
END;

-- Decisions
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision TEXT NOT NULL,
    reasoning TEXT,
    project TEXT DEFAULT 'global',
    session_id TEXT,
    trace TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);

-- Mistakes (never pruned by GC; soft-archived only — never hard-deleted)
CREATE TABLE IF NOT EXISTS mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mistake TEXT NOT NULL,
    why TEXT,
    project TEXT DEFAULT 'global',
    confidence REAL DEFAULT 0.95,
    session_id TEXT,
    created_at TEXT NOT NULL,
    archived INTEGER DEFAULT 0
);

-- Reflections (never pruned by GC; soft-archived only — never hard-deleted)
CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    went_well TEXT,
    missed TEXT,
    do_differently TEXT,
    project TEXT DEFAULT 'global',
    session_id TEXT,
    created_at TEXT NOT NULL,
    archived INTEGER DEFAULT 0
);

-- Calibration exemplars (v13: user_text/agent_text — formerly pete/atlas)
CREATE TABLE IF NOT EXISTS exemplars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario TEXT,
    user_text TEXT NOT NULL,
    agent_text TEXT,
    calibration TEXT,
    tags TEXT,
    created_at TEXT NOT NULL
);

-- Decision outcomes (close the learning loop)
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    success INTEGER,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_decision_outcomes_decision ON decision_outcomes(decision_id);

-- Hypnos dream journal (memory maintenance log)
CREATE TABLE IF NOT EXISTS hypnos_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    action TEXT NOT NULL,
    fact_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_hypnos_run ON hypnos_journal(run_id);
CREATE INDEX IF NOT EXISTS idx_hypnos_started ON hypnos_journal(started_at);

-- Session fingerprints (conversation shape tracking)
CREATE TABLE IF NOT EXISTS session_fingerprints (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    duration_minutes REAL,
    facts_count INTEGER DEFAULT 0,
    decisions_count INTEGER DEFAULT 0,
    mistakes_count INTEGER DEFAULT 0,
    tier_dist TEXT DEFAULT '{}',
    topic_vector BLOB,
    outcome TEXT DEFAULT 'neutral',
    tags TEXT DEFAULT '[]',
    energy_arc TEXT DEFAULT '',
    highlights TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fingerprints_project ON session_fingerprints(project);
CREATE INDEX IF NOT EXISTS idx_fingerprints_outcome ON session_fingerprints(outcome);

-- Cross-instance decision feed (multi-Atlas coordination)
CREATE TABLE IF NOT EXISTS decision_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasoning TEXT,
    project TEXT DEFAULT 'global',
    status TEXT DEFAULT 'provisional',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_feed_project ON decision_feed(project);
CREATE INDEX IF NOT EXISTS idx_decision_feed_session ON decision_feed(session_id);
CREATE INDEX IF NOT EXISTS idx_decision_feed_created ON decision_feed(created_at);

-- Calibration probes (verify recall quality over time)
CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    expected TEXT NOT NULL,
    fact_id TEXT,
    probe_type TEXT DEFAULT 'user',
    created_at TEXT NOT NULL,
    last_run TEXT,
    last_result TEXT,
    run_count INTEGER DEFAULT 0,
    pass_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_probes_type ON probes(probe_type);
CREATE INDEX IF NOT EXISTS idx_probes_fact ON probes(fact_id);

-- Evaluation snapshots (track Null health over time)
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    score REAL NOT NULL,
    metrics TEXT NOT NULL,
    notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evaluations_run ON evaluations(run_at);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project);
CREATE INDEX IF NOT EXISTS idx_facts_created ON facts(created_at);
CREATE INDEX IF NOT EXISTS idx_facts_superseded ON facts(superseded_by);
CREATE INDEX IF NOT EXISTS idx_facts_active ON facts(forgotten, archived, superseded_by);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_mistakes_project ON mistakes(project);
CREATE INDEX IF NOT EXISTS idx_mistakes_created ON mistakes(created_at);
CREATE INDEX IF NOT EXISTS idx_reflections_created ON reflections(created_at);
"""


def _rename_exemplar_columns(conn: sqlite3.Connection) -> None:
    """Idempotent v13 migration: exemplars.pete→user_text, atlas→agent_text.

    Shared by the legacy per-personality migration runner and the unified
    upgrade runner (migrate_v3). Safe to call on already-migrated DBs."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(exemplars)").fetchall()}
    except sqlite3.OperationalError:
        return
    if not cols:
        return
    for old, new in (("pete", "user_text"), ("atlas", "agent_text")):
        if old in cols and new not in cols:
            try:
                conn.execute(
                    f"ALTER TABLE exemplars RENAME COLUMN {old} TO {new}"
                )
            except sqlite3.OperationalError:
                pass


def _create_and_backfill_fact_edges(conn: sqlite3.Connection) -> None:
    """Idempotent v14 migration (P2-16): create the fact_edges table and
    backfill it from the legacy related_to JSON column.

    Shared by the legacy migration runner and the unified upgrade runner.
    Safe to re-run: edges insert with OR IGNORE, so JSON written by older
    code (or merged in from legacy DBs) is picked up on the next pass."""
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fact_edges (
                fact_id TEXT NOT NULL,
                related_id TEXT NOT NULL,
                created_at TEXT,
                PRIMARY KEY (fact_id, related_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fact_edges_related "
            "ON fact_edges(related_id)"
        )
    except sqlite3.OperationalError:
        return

    fact_cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
    if "related_to" not in fact_cols:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT id, related_to FROM facts "
        "WHERE related_to IS NOT NULL AND related_to != '[]'"
    ).fetchall()
    for fact_id, related_json in rows:
        try:
            related = json.loads(related_json or "[]")
        except json.JSONDecodeError:
            continue
        for rid in related:
            if isinstance(rid, str) and rid:
                conn.execute(
                    "INSERT OR IGNORE INTO fact_edges "
                    "(fact_id, related_id, created_at) VALUES (?, ?, ?)",
                    (fact_id, rid, now),
                )


class NullDB:
    """SQLite connection manager for Null Memory.

    Supports two modes:
    - **Per-personality DB** (legacy): one ``memory.db`` per personality directory.
    - **Unified DB** (v12): single ``~/.null/unified.db`` with personality as a
      column. Pass ``unified_path`` and ``personality`` to enable.
    """

    def __init__(
        self,
        agent_dir: str,
        unified_path: str | None = None,
        personality: str = "atlas",
    ):
        self.agent_dir = agent_dir
        self.personality = personality
        if unified_path and os.path.exists(unified_path):
            self.unified = True
            self.db_path = unified_path
        else:
            self.unified = False
            self.db_path = os.path.join(agent_dir, "memory.db")
        # Per-thread connections (v12.1): SQLite WAL handles cross-process
        # concurrency, but a SINGLE shared connection across threads meant
        # one thread's commit() could persist another thread's half-finished
        # logical transaction (e.g. HypnosLiveWorker committing mid-learn()).
        # Each thread now gets its own identically-configured connection,
        # resolved lazily through the .conn property so the hundreds of
        # existing `db.conn` call sites keep working unchanged.
        self._local = threading.local()
        # All connections ever handed out, so close() can clean up even
        # connections created by other (now finished) threads.
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        # v3.2 WAL instrumentation — track commit latency and lock contention
        # (shared across threads; counters only, small races are acceptable)
        self._commit_stats = {
            "commits": 0,
            "locked_retries": 0,
            "slow_commits_ms": [],  # commits taking >50ms
            "total_commit_ms": 0.0,
        }

    @property
    def conn(self) -> sqlite3.Connection:
        """Thread-local connection. Each thread lazily gets its own
        connection configured identically (WAL, busy_timeout, row_factory).
        MUST be re-evaluated per access — never cache the result across
        threads."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    def _connect(self) -> sqlite3.Connection:
        if not self.unified:
            os.makedirs(self.agent_dir, exist_ok=True)
        # v3.2 WAL instrumentation — use a Connection subclass that records
        # commit latency and lock-retry counts into self._commit_stats.
        stats = self._commit_stats

        class _InstrumentedConnection(sqlite3.Connection):
            def commit(self_inner):  # type: ignore[override]
                import time as _time
                t0 = _time.perf_counter()
                retries = 0
                while True:
                    try:
                        super().commit()
                        break
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e).lower() and retries < 3:
                            retries += 1
                            _time.sleep(0.1 * retries)
                            continue
                        raise
                elapsed_ms = (_time.perf_counter() - t0) * 1000
                stats["commits"] += 1
                stats["locked_retries"] += retries
                stats["total_commit_ms"] += elapsed_ms
                if elapsed_ms > 50:
                    stats["slow_commits_ms"].append(round(elapsed_ms, 1))
                    if len(stats["slow_commits_ms"]) > 100:
                        stats["slow_commits_ms"] = stats["slow_commits_ms"][-100:]

        conn = sqlite3.connect(
            self.db_path, check_same_thread=False,
            factory=_InstrumentedConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def commit_stats(self) -> dict:
        """Snapshot of commit latency + contention since connection open."""
        s = dict(self._commit_stats)
        if s["commits"]:
            s["avg_commit_ms"] = round(s["total_commit_ms"] / s["commits"], 2)
        else:
            s["avg_commit_ms"] = 0.0
        return s

    def initialize(self) -> None:
        """Create schema if not exists. Runs migrations for schema upgrades."""
        if self.unified:
            # Unified DB schema is owned by migrate_v3.py. Don't apply legacy
            # v11 schema or run v1→v11 migrations on it — but do ratchet the
            # idempotent unified upgrades so columns this codebase expects
            # exist, and heal any self-superseded tombstones. The upgrade
            # runner also structurally verifies the store (issue #1): a
            # pre-unified store stamped with the unified schema_version
            # (e.g. relocated from another machine) self-heals here —
            # personalities table created, personality columns added and
            # backfilled with this store's personality.
            from null_memory.migrate_v3 import _apply_unified_upgrades
            _apply_unified_upgrades(self.conn, default_personality=self.personality)
            self.conn.commit()
            return
        self.conn.executescript(_SCHEMA_SQL)
        # Set schema version if not present
        existing = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            self.conn.commit()
        else:
            current_version = int(existing[0])
            if current_version >= _UNIFIED_SCHEMA_VERSION():
                # Issue #3: a per-personality store stamped with the unified
                # schema_version (e.g. relocated from another machine and
                # served directly via `serve <dir>` — no unified.db, so the
                # unified branch above never runs). The legacy migration
                # ladder sees 24 > SCHEMA_VERSION and does nothing, leaving
                # the unified structure missing. Heal it in place.
                from null_memory.migrate_v3 import heal_unified_structure
                heal_unified_structure(
                    self.conn, default_personality=self.personality
                )
                self.conn.commit()
            elif current_version < SCHEMA_VERSION:
                self._run_migrations(current_version)
        # Indexes that depend on columns added by migrations
        try:
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_tier ON facts(tier)"
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def _run_migrations(self, from_version: int) -> None:
        """Run schema migrations from from_version to SCHEMA_VERSION."""
        if from_version < 2:
            # v2: add related_to column to facts
            try:
                self.conn.execute(
                    "ALTER TABLE facts ADD COLUMN related_to TEXT DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
        if from_version < 3:
            # v3: decision_outcomes table + fact_embeddings table
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS decision_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    success INTEGER,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY (decision_id) REFERENCES decisions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_decision_outcomes_decision
                    ON decision_outcomes(decision_id);
            """)
        if from_version < 4:
            # v4: tier column on facts (ephemeral/contextual/durable)
            try:
                self.conn.execute(
                    "ALTER TABLE facts ADD COLUMN tier TEXT DEFAULT 'contextual'"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_facts_tier ON facts(tier)"
                )
            except sqlite3.OperationalError:
                pass
        if from_version < 5:
            # v5: Hypnos dream journal for memory maintenance logging
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS hypnos_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    action TEXT NOT NULL,
                    fact_id TEXT,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_hypnos_run
                    ON hypnos_journal(run_id);
                CREATE INDEX IF NOT EXISTS idx_hypnos_started
                    ON hypnos_journal(started_at);
            """)
        if from_version < 6:
            # v6: Session fingerprints for conversation pattern matching
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS session_fingerprints (
                    session_id TEXT PRIMARY KEY,
                    project TEXT,
                    duration_minutes REAL,
                    facts_count INTEGER DEFAULT 0,
                    decisions_count INTEGER DEFAULT 0,
                    mistakes_count INTEGER DEFAULT 0,
                    tier_dist TEXT DEFAULT '{}',
                    topic_vector BLOB,
                    outcome TEXT DEFAULT 'neutral',
                    tags TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fingerprints_project
                    ON session_fingerprints(project);
                CREATE INDEX IF NOT EXISTS idx_fingerprints_outcome
                    ON session_fingerprints(outcome);
            """)
        if from_version < 7:
            # v7: Cross-instance decision feed for multi-Atlas coordination
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS decision_feed (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reasoning TEXT,
                    project TEXT DEFAULT 'global',
                    status TEXT DEFAULT 'provisional',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_feed_project
                    ON decision_feed(project);
                CREATE INDEX IF NOT EXISTS idx_decision_feed_session
                    ON decision_feed(session_id);
                CREATE INDEX IF NOT EXISTS idx_decision_feed_created
                    ON decision_feed(created_at);
            """)
        if from_version < 8:
            # v8: Energy arc and highlights in session fingerprints
            try:
                self.conn.execute(
                    "ALTER TABLE session_fingerprints ADD COLUMN energy_arc TEXT DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass
            try:
                self.conn.execute(
                    "ALTER TABLE session_fingerprints ADD COLUMN highlights TEXT DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                pass
        if from_version < 9:
            # v9: Decision trace — reasoning chain (fact IDs) that led to each decision
            try:
                self.conn.execute(
                    "ALTER TABLE decisions ADD COLUMN trace TEXT DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                pass
        if from_version < 10:
            # v10: Calibration probes — verify recall quality over time
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS probes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    fact_id TEXT,
                    probe_type TEXT DEFAULT 'user',
                    created_at TEXT NOT NULL,
                    last_run TEXT,
                    last_result TEXT,
                    run_count INTEGER DEFAULT 0,
                    pass_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_probes_type ON probes(probe_type);
                CREATE INDEX IF NOT EXISTS idx_probes_fact ON probes(fact_id);
            """)
        if from_version < 11:
            # v11: Evaluation snapshots — track Null health over time
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at TEXT NOT NULL,
                    score REAL NOT NULL,
                    metrics TEXT NOT NULL,
                    notes TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_evaluations_run ON evaluations(run_at);
            """)
        if from_version < 12:
            # v12: soft-archive flag on mistakes/reflections (product
            # invariant: mistakes are never hard-deleted) + heal
            # self-superseded tombstones left by the old learn() dedup bug.
            for table in ("mistakes", "reflections"):
                try:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN archived INTEGER DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    pass  # Column already exists
            self.repair_self_superseded()
        if from_version < 13:
            # v13: de-Pete the exemplars table — rename pete→user_text,
            # atlas→agent_text. Data is preserved by RENAME COLUMN.
            _rename_exemplar_columns(self.conn)
        if from_version < 14:
            # v14: relationship edges move from the related_to JSON column
            # to a real fact_edges table (backfilled; JSON kept read-only).
            _create_and_backfill_fact_edges(self.conn)
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        """Close every connection this NullDB has handed out (all threads)."""
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        # Reset this thread's cached handle; other threads' thread-locals
        # now hold closed connections — any further use is a programming
        # error and will raise loudly (better than silent cross-talk).
        self._local.conn = None
        self._local.in_write_txn = False

    @contextmanager
    def write_transaction(self, max_wait_seconds: float = 5.0) -> Iterator[sqlite3.Connection]:
        """Explicit write transaction: BEGIN IMMEDIATE … COMMIT/ROLLBACK.

        Grabs SQLite's write lock up-front so read-modify-write sequences
        (learn() dedup, consolidate(), add_relationship()) are atomic
        against other processes sharing the WAL database. Retries on
        'database is locked/busy' with backoff, bounded by
        ``max_wait_seconds`` (mirrors busy_timeout=5000).

        Re-entrant per thread: a nested call on the same thread yields the
        same connection without opening a second transaction — the
        outermost context commits/rolls back.

        If the thread-local connection already has an implicit (deferred)
        transaction open from earlier autocommit-style writes, that work
        is committed first — connections are per-thread, so this only
        flushes this thread's own pending statements.
        """
        conn = self.conn
        if getattr(self._local, "in_write_txn", False):
            # Nested — the outermost write_transaction owns commit/rollback.
            yield conn
            return

        if conn.in_transaction:
            # Flush this thread's own pending implicit transaction so
            # BEGIN IMMEDIATE doesn't raise "within a transaction".
            conn.commit()

        delay = 0.025
        waited = 0.0
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if ("locked" in msg or "busy" in msg) and waited < max_wait_seconds:
                    time.sleep(delay)
                    waited += delay
                    delay = min(delay * 2, 0.25)
                    continue
                raise

        self._local.in_write_txn = True
        try:
            yield conn
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        else:
            conn.commit()
        finally:
            self._local.in_write_txn = False

    # ── Instance presence registry ──
    # Every live Null process (MCP server, CLI invocation, daemon) keeps a
    # row here so instances sharing one store can see each other. Liveness
    # is heartbeat-based: a row is "live" when last_heartbeat is younger
    # than INSTANCE_LIVE_WINDOW_MINUTES. All methods are best-effort —
    # presence is advisory and must never break the primary action.

    def register_instance(
        self,
        instance_id: str,
        *,
        hostname: str,
        pid: int,
        personality: str,
        transport: str,
        project: str | None = None,
        schema_version_seen: int | None = None,
    ) -> None:
        """Insert this process's presence row (and GC long-dead rows).

        Creates the table on demand so legacy per-personality stores work
        too. The GC DELETE is bounded (_INSTANCE_GC_BATCH) so registration
        stays cheap even on a store with pathological row buildup.
        """
        now = datetime.now(timezone.utc)
        gc_cutoff = (now - timedelta(days=INSTANCE_GC_DAYS)).isoformat()
        with self.write_transaction() as conn:
            conn.execute(_INSTANCES_TABLE_SQL)
            conn.execute(_INSTANCES_INDEX_SQL)
            conn.execute(
                "DELETE FROM instances WHERE rowid IN ("
                "  SELECT rowid FROM instances"
                "  WHERE last_heartbeat < ? LIMIT ?)",
                (gc_cutoff, _INSTANCE_GC_BATCH),
            )
            conn.execute(
                """INSERT OR REPLACE INTO instances
                   (instance_id, hostname, pid, started_at, last_heartbeat,
                    personality, transport, project, schema_version_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    instance_id, hostname, pid, now.isoformat(),
                    now.isoformat(), personality, transport, project,
                    schema_version_seen,
                ),
            )

    def heartbeat_instance(self, instance_id: str,
                           project: str | None = None) -> bool:
        """Refresh this instance's last_heartbeat. Returns False when the
        row is gone (GC'd / table missing) so the caller can re-register."""
        try:
            with self.write_transaction() as conn:
                cur = conn.execute(
                    """UPDATE instances
                       SET last_heartbeat = ?,
                           project = COALESCE(?, project)
                       WHERE instance_id = ?""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        project, instance_id,
                    ),
                )
                return cur.rowcount == 1
        except sqlite3.OperationalError:
            return False

    def get_live_instances(
        self, window_minutes: float = INSTANCE_LIVE_WINDOW_MINUTES,
    ) -> list[dict]:
        """All instances with a heartbeat inside the liveness window,
        oldest-started first. [] when the table doesn't exist yet."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        ).isoformat()
        try:
            rows = self.conn.execute(
                "SELECT * FROM instances WHERE last_heartbeat >= ? "
                "ORDER BY started_at",
                (cutoff,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    @property
    def needs_migration(self) -> bool:
        """True if JSONL files exist but memory.db does not."""
        if os.path.isfile(self.db_path):
            return False
        jsonl_files = ["knowledge.jsonl", "decisions.jsonl", "mistakes.jsonl",
                       "reflections.jsonl"]
        return any(
            os.path.isfile(os.path.join(self.agent_dir, f))
            for f in jsonl_files
        )

    # ── Query Helpers ──

    def insert_fact(self, entry: dict) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO facts
               (id, fact, confidence, base_confidence, project, source,
                provenance, impact, session_id, created_at, last_accessed,
                access_count, last_verified, verified_by, superseded_by,
                forgotten, archived, tier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry["id"],
                entry["fact"],
                entry.get("confidence", 0.8),
                entry.get("base_confidence", entry.get("confidence", 0.8)),
                entry.get("project", "global").strip().lower(),
                entry.get("source", "observation"),
                entry.get("provenance", entry.get("source", "observation")),
                entry.get("impact", 0.5),
                entry.get("session_id"),
                entry["created_at"],
                entry.get("last_accessed"),
                entry.get("access_count", 0),
                entry.get("last_verified"),
                entry.get("verified_by"),
                entry.get("superseded_by"),
                1 if entry.get("forgotten") else 0,
                1 if entry.get("archived") else 0,
                entry.get("tier", "contextual"),
            ),
        )
        if self.unified:
            self.conn.execute(
                """INSERT OR REPLACE INTO personality_views
                   (fact_id, personality, last_accessed, access_count, tags)
                   VALUES (?, ?, ?, ?, '[]')""",
                (
                    entry["id"],
                    self.personality,
                    entry.get("last_accessed") or entry["created_at"],
                    entry.get("access_count", 0),
                ),
            )

    def insert_decision(self, entry: dict) -> int:
        """Insert a decision and return its ID."""
        if self.unified:
            cursor = self.conn.execute(
                """INSERT INTO decisions (decision, reasoning, project, personality,
                   session_id, trace, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry["decision"],
                    entry.get("reasoning", ""),
                    entry.get("project", "global").strip().lower(),
                    self.personality,
                    entry.get("session_id"),
                    json.dumps(entry.get("trace", [])),
                    entry["created_at"],
                ),
            )
            return cursor.lastrowid
        cursor = self.conn.execute(
            """INSERT INTO decisions (decision, reasoning, project, session_id, trace, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entry["decision"],
                entry.get("reasoning", ""),
                entry.get("project", "global").strip().lower(),
                entry.get("session_id"),
                json.dumps(entry.get("trace", [])),
                entry["created_at"],
            ),
        )
        return cursor.lastrowid

    def insert_mistake(self, entry: dict) -> int:
        """Insert a mistake and return its ID."""
        if self.unified:
            cursor = self.conn.execute(
                """INSERT INTO mistakes (mistake, why, project, personality,
                   confidence, session_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry["mistake"],
                    entry.get("why", ""),
                    entry.get("project", "global").strip().lower(),
                    self.personality,
                    entry.get("confidence", 0.95),
                    entry.get("session_id"),
                    entry["created_at"],
                ),
            )
            return cursor.lastrowid
        cursor = self.conn.execute(
            """INSERT INTO mistakes (mistake, why, project, confidence, session_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entry["mistake"],
                entry.get("why", ""),
                entry.get("project", "global").strip().lower(),
                entry.get("confidence", 0.95),
                entry.get("session_id"),
                entry["created_at"],
            ),
        )
        return cursor.lastrowid

    def insert_reflection(self, entry: dict) -> int:
        """Insert a reflection and return its ID."""
        if self.unified:
            cursor = self.conn.execute(
                """INSERT INTO reflections (went_well, missed, do_differently,
                   project, personality, session_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.get("went_well", ""),
                    entry.get("missed", ""),
                    entry.get("do_differently", ""),
                    entry.get("project", "global").strip().lower(),
                    self.personality,
                    entry.get("session_id"),
                    entry["created_at"],
                ),
            )
            return cursor.lastrowid
        cursor = self.conn.execute(
            """INSERT INTO reflections (went_well, missed, do_differently, project, session_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entry.get("went_well", ""),
                entry.get("missed", ""),
                entry.get("do_differently", ""),
                entry.get("project", "global").strip().lower(),
                entry.get("session_id"),
                entry["created_at"],
            ),
        )
        return cursor.lastrowid

    def insert_exemplar(self, entry: dict) -> int:
        tags = entry.get("tags", [])
        tags_json = json.dumps(tags) if isinstance(tags, list) else (tags or "[]")
        # v13: canonical keys are user_text/agent_text; accept legacy
        # pete/atlas keys from old JSONL exports and callers.
        user_text = entry.get("user_text", entry.get("pete", ""))
        agent_text = entry.get("agent_text", entry.get("atlas", ""))
        if self.unified:
            cursor = self.conn.execute(
                """INSERT INTO exemplars (scenario, user_text, agent_text,
                   calibration, tags, personality, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.get("scenario", ""),
                    user_text,
                    agent_text,
                    entry.get("calibration", ""),
                    tags_json,
                    self.personality,
                    entry.get("created_at", datetime.now(timezone.utc).isoformat()),
                ),
            )
            return cursor.lastrowid
        cursor = self.conn.execute(
            """INSERT INTO exemplars (scenario, user_text, agent_text,
               calibration, tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entry.get("scenario", ""),
                user_text,
                agent_text,
                entry.get("calibration", ""),
                tags_json,
                entry.get("created_at", datetime.now(timezone.utc).isoformat()),
            ),
        )
        return cursor.lastrowid

    def get_active_facts(self) -> list[dict]:
        """Get all non-forgotten, non-archived, non-superseded facts."""
        rows = self.conn.execute(
            """SELECT * FROM facts
               WHERE forgotten = 0 AND archived = 0 AND superseded_by IS NULL
               ORDER BY created_at""",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_facts(self) -> list[dict]:
        """Get all facts including archived/forgotten (for export)."""
        rows = self.conn.execute("SELECT * FROM facts ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def _personality_predicate(self, column: str = "personality") -> tuple[str, list]:
        """Scoping fragment for reads of personality-attributed tables
        (decisions, mistakes, reflections, exemplars, probes, evaluations,
        session_fingerprints, decision_feed, hypnos_journal).

        On a **unified** store multiple personalities share one database,
        so reads must filter to this connection's personality — issue #19:
        one personality's briefing/status must not include another's rows.
        The unified schema (and its structural heal) guarantees the column
        exists there. Legacy **per-personality** stores are
        single-personality by construction and may predate the column, so
        no predicate is applied (returns ("", [])).

        Facts are deliberately NOT scoped: within one store they are the
        shared knowledge plane (one trust domain) — see
        memory/briefing_render.py module docstring for the full split.

        Returns (sql_fragment, params); fragment is "" or "{column} = ?",
        the caller composes WHERE/AND.
        """
        if self.unified:
            return f"{column} = ?", [self.personality]
        return "", []

    def get_decisions(self, limit: int = 0) -> list[dict]:
        pred, params = self._personality_predicate()
        query = "SELECT * FROM decisions"
        if pred:
            query += f" WHERE {pred}"
        query += " ORDER BY created_at"
        if limit:
            query += f" LIMIT {limit}"
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def get_mistakes(self, limit: int = 0,
                     include_archived: bool = False) -> list[dict]:
        pred, params = self._personality_predicate()
        conditions = [] if include_archived else ["archived = 0"]
        if pred:
            conditions.append(pred)
        query = "SELECT * FROM mistakes"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at"
        if limit:
            query += f" LIMIT {limit}"
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def get_reflections(self, limit: int = 0,
                        include_archived: bool = False) -> list[dict]:
        pred, params = self._personality_predicate()
        conditions = [] if include_archived else ["archived = 0"]
        if pred:
            conditions.append(pred)
        query = "SELECT * FROM reflections"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at"
        if limit:
            query += f" LIMIT {limit}"
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def get_exemplars(self) -> list[dict]:
        pred, params = self._personality_predicate()
        query = "SELECT * FROM exemplars"
        if pred:
            query += f" WHERE {pred}"
        query += " ORDER BY created_at"
        rows = self.conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Parse tags JSON back to list
            try:
                d["tags"] = json.loads(d.get("tags", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
            result.append(d)
        return result

    # ── Probes ──

    def insert_probe(self, question: str, expected: str,
                     fact_id: str | None = None,
                     probe_type: str = "user") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        if self.unified:
            cursor = self.conn.execute(
                """INSERT INTO probes (question, expected, fact_id, probe_type,
                   personality, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (question, expected, fact_id, probe_type, self.personality, now),
            )
        else:
            cursor = self.conn.execute(
                """INSERT INTO probes (question, expected, fact_id, probe_type, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (question, expected, fact_id, probe_type, now),
            )
        self.conn.commit()
        return {
            "id": cursor.lastrowid,
            "question": question,
            "expected": expected,
            "fact_id": fact_id,
            "probe_type": probe_type,
            "created_at": now,
        }

    def get_probes(self, probe_type: str | None = None) -> list[dict]:
        pred, params = self._personality_predicate()
        conditions = []
        if probe_type:
            conditions.append("probe_type = ?")
            params.insert(0, probe_type)
        if pred:
            conditions.append(pred)
        query = "SELECT * FROM probes"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_probe_result(self, probe_id: int, passed: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE probes SET last_run = ?, last_result = ?,
               run_count = run_count + 1,
               pass_count = pass_count + CASE WHEN ? THEN 1 ELSE 0 END
               WHERE id = ?""",
            (now, "pass" if passed else "fail", passed, probe_id),
        )
        self.conn.commit()

    def get_probes_for_facts(self, fact_ids: list[str]) -> dict[str, list[dict]]:
        """Get probes grouped by fact_id for a list of fact IDs."""
        if not fact_ids:
            return {}
        placeholders = ",".join("?" for _ in fact_ids)
        rows = self.conn.execute(
            f"SELECT * FROM probes WHERE fact_id IN ({placeholders})",
            fact_ids,
        ).fetchall()
        result: dict[str, list[dict]] = {}
        for r in rows:
            d = dict(r)
            fid = d.get("fact_id", "")
            if fid:
                result.setdefault(fid, []).append(d)
        return result

    def delete_probes_for_fact(self, fact_id: str) -> int:
        cursor = self.conn.execute(
            "DELETE FROM probes WHERE fact_id = ?", (fact_id,)
        )
        self.conn.commit()
        return cursor.rowcount

    # ── Evaluations ──

    def insert_evaluation(self, score: float, metrics: dict,
                          notes: str = "") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        metrics_json = json.dumps(metrics)
        if self.unified:
            cursor = self.conn.execute(
                """INSERT INTO evaluations (personality, run_at, score, metrics, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (self.personality, now, score, metrics_json, notes),
            )
        else:
            cursor = self.conn.execute(
                """INSERT INTO evaluations (run_at, score, metrics, notes)
                   VALUES (?, ?, ?, ?)""",
                (now, score, metrics_json, notes),
            )
        self.conn.commit()
        return {"id": cursor.lastrowid, "run_at": now, "score": score,
                "metrics": metrics, "notes": notes}

    def get_evaluations(self, limit: int = 10) -> list[dict]:
        pred, params = self._personality_predicate()
        where = f" WHERE {pred}" if pred else ""
        rows = self.conn.execute(
            f"SELECT * FROM evaluations{where} ORDER BY run_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["metrics"] = json.loads(d.get("metrics", "{}"))
            except (json.JSONDecodeError, TypeError):
                d["metrics"] = {}
            results.append(d)
        return results

    def get_last_evaluation(self) -> dict | None:
        evals = self.get_evaluations(limit=1)
        return evals[0] if evals else None

    def count_probes(self) -> int:
        pred, params = self._personality_predicate()
        where = f" WHERE {pred}" if pred else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM probes{where}", params
        ).fetchone()
        return row[0] if row else 0

    def count_facts(self, active_only: bool = True) -> int:
        # Facts are the shared knowledge plane — never personality-scoped.
        if active_only:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM facts WHERE forgotten = 0 AND archived = 0 AND superseded_by IS NULL"
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()
        return row[0] if row else 0

    def count_decisions(self) -> int:
        pred, params = self._personality_predicate()
        where = f" WHERE {pred}" if pred else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM decisions{where}", params
        ).fetchone()
        return row[0] if row else 0

    def count_mistakes(self) -> int:
        pred, params = self._personality_predicate()
        and_pred = f" AND {pred}" if pred else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM mistakes WHERE archived = 0{and_pred}",
            params,
        ).fetchone()
        return row[0] if row else 0

    def count_reflections(self) -> int:
        pred, params = self._personality_predicate()
        and_pred = f" AND {pred}" if pred else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM reflections WHERE archived = 0{and_pred}",
            params,
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize a query string for FTS5.

        Handles:
        - Hyphens (FTS5 treats - as NOT operator): replace with spaces
        - Special FTS5 operators: quote terms that look like operators
        - Empty/whitespace: return empty string
        """
        if not query or not query.strip():
            return ""
        # Replace hyphens with spaces (prevents NOT interpretation)
        sanitized = query.replace("-", " ")
        # Remove other FTS5 special chars that could cause syntax errors
        for ch in ["(", ")", "{", "}", "^", "~"]:
            sanitized = sanitized.replace(ch, " ")
        # Collapse whitespace
        sanitized = " ".join(sanitized.split())
        return sanitized

    def fts_search(self, query: str, project: str | None = None,
                   include_archived: bool = False,
                   since: str | None = None,
                   limit: int = 30) -> list[dict]:
        """Full-text search using FTS5 with BM25 ranking.

        Returns facts sorted by BM25 relevance, with rank included.
        """
        fts_query = self._sanitize_fts_query(query)
        if not fts_query:
            return []

        conditions = ["f.forgotten = 0", "f.superseded_by IS NULL"]
        params: list[Any] = [fts_query]

        if not include_archived:
            conditions.append("f.archived = 0")

        if project:
            conditions.append("(f.project = ? OR f.project = 'global')")
            params.append(project.strip().lower())

        if since:
            conditions.append("f.created_at > ?")
            params.append(since)

        params.append(limit)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT f.*, facts_fts.rank AS bm25_rank
            FROM facts f
            JOIN facts_fts ON f.rowid = facts_fts.rowid
            WHERE facts_fts MATCH ?
              AND {where}
            ORDER BY facts_fts.rank
            LIMIT ?
        """
        try:
            rows = self.conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            # FTS query syntax error — fall back to empty
            return []

    def trigram_search(self, query: str, project: str | None = None,
                       exclude_ids: set[str] | None = None,
                       limit: int = 10) -> list[dict]:
        """Fuzzy substring search using trigram tokenizer.

        Used as fallback when FTS5 keyword search returns insufficient results.
        Trigram queries need quoted strings for substring matching.
        """
        # Trigram match requires quoted terms for substring matching
        trigram_query = f'"{query}"'
        conditions = ["f.forgotten = 0", "f.superseded_by IS NULL", "f.archived = 0"]
        params: list[Any] = [trigram_query]

        if project:
            conditions.append("(f.project = ? OR f.project = 'global')")
            params.append(project.strip().lower())

        params.append(limit * 2)  # over-fetch to allow filtering

        where = " AND ".join(conditions)
        sql = f"""
            SELECT f.*, facts_trigram.rank AS trigram_rank
            FROM facts f
            JOIN facts_trigram ON f.rowid = facts_trigram.rowid
            WHERE facts_trigram MATCH ?
              AND {where}
            ORDER BY facts_trigram.rank
            LIMIT ?
        """
        try:
            rows = self.conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]
            # Filter out already-found IDs
            if exclude_ids:
                results = [r for r in results if r["id"] not in exclude_ids]
            return results[:limit]
        except sqlite3.OperationalError:
            return []

    def search_mistakes(self, query: str, project: str | None = None,
                        limit: int = 5) -> list[dict]:
        """Search mistakes by keyword (simple LIKE — mistakes are small)."""
        conditions = ["archived = 0"]
        params: list[Any] = []
        pred, pred_params = self._personality_predicate()
        if pred:
            conditions.append(pred)
            params.extend(pred_params)

        terms = query.lower().split()
        for term in terms:
            conditions.append("(LOWER(mistake) LIKE ? OR LOWER(why) LIKE ?)")
            params.extend([f"%{term}%", f"%{term}%"])

        if project:
            conditions.append("(project = ? OR project = 'global')")
            params.append(project.strip().lower())

        params.append(limit)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM mistakes WHERE {where} ORDER BY created_at DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_decision_with_trace(self, decision_id: int) -> dict | None:
        """Get a decision with its reasoning trace (fact IDs that informed it)."""
        row = self.conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["trace"] = json.loads(d.get("trace") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["trace"] = []
        return d

    def find_similar_decisions_with_traces(self, query: str,
                                           project: str | None = None,
                                           limit: int = 3) -> list[dict]:
        """Find past decisions similar to a query, with their reasoning traces."""
        conditions = ["LOWER(decision) LIKE ?"]
        params: list = [f"%{query.lower()}%"]
        if project:
            conditions.append("(project = ? OR project = 'global')")
            params.append(project.strip().lower())
        params.append(limit)
        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT * FROM decisions WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["trace"] = json.loads(d.get("trace") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["trace"] = []
            results.append(d)
        return results

    def get_mistake_by_id(self, mistake_id: int) -> dict | None:
        """Get a single mistake by ID."""
        row = self.conn.execute(
            "SELECT * FROM mistakes WHERE id = ?", (mistake_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_fact_access(self, fact_id: str) -> None:
        """Increment access count and update last_accessed timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE facts SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
            (now, fact_id),
        )

    def update_facts_access_batch(self, fact_ids: list[str]) -> None:
        """Increment access counts for many facts in ONE statement.

        recall() touches every returned fact; doing it per-row meant one
        write per result, amplifying two-instance write contention (P0-6).
        """
        if not fact_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(fact_ids))
        self.conn.execute(
            f"UPDATE facts SET access_count = access_count + 1, "
            f"last_accessed = ? WHERE id IN ({placeholders})",
            [now, *fact_ids],
        )

    # ── Meta counters (health signals) ──

    def bump_meta_counter(self, key: str, by: int = 1) -> None:
        """Atomically increment an integer counter stored in meta."""
        self.conn.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value = CAST(CAST(value AS INTEGER) + ? AS TEXT)""",
            (key, str(by), by),
        )

    def get_meta(self, key: str) -> str | None:
        """Read a meta value, or None if unset."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta value."""
        self.conn.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )

    def forget_fact(self, fact_id: str) -> bool:
        """Soft-delete a fact. Returns True if found."""
        cursor = self.conn.execute(
            "UPDATE facts SET forgotten = 1 WHERE id = ? AND forgotten = 0",
            (fact_id,),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def archive_fact(self, fact_id: str) -> None:
        """Mark a fact as archived."""
        self.conn.execute(
            "UPDATE facts SET archived = 1 WHERE id = ?",
            (fact_id,),
        )

    def supersede_fact(self, fact_id: str, new_hash: str) -> None:
        """Mark a fact as superseded by another."""
        self.conn.execute(
            "UPDATE facts SET superseded_by = ? WHERE id = ?",
            (new_hash, fact_id),
        )

    def repair_self_superseded(self) -> int:
        """Heal self-superseded tombstones (superseded_by = id).

        A pre-v12 learn() bug marked exact-duplicate facts as superseded
        by themselves when a higher-authority source re-learned them,
        hiding them from every active query. Idempotent — returns the
        number of rows resurrected.
        """
        cur = self.conn.execute(
            "UPDATE facts SET superseded_by = NULL WHERE superseded_by = id"
        )
        self.conn.commit()
        return cur.rowcount

    def verify_fact(self, fact_id: str, session_id: str | None = None) -> None:
        """Mark a fact as verified."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE facts SET last_verified = ?, verified_by = ? WHERE id = ?",
            (now, session_id, fact_id),
        )

    # ── Emotional Anchors (schema v13) ──

    ANCHOR_TYPES = ("origin", "commitment", "loss", "joy", "turning_point")

    def anchors_supported(self) -> bool:
        """True when the facts table carries the v13 anchor columns.

        Historically anchors were gated on ``self.unified``, but per-seat
        worker stores have carried the anchor columns since the unified
        structural heal (persona create ratchets every new seat) — the
        unified gate silently disabled anchors on exactly the seats
        onboarding needs them on (issue #27 field report). Capability is
        judged by the actual schema, not the store flavor."""
        if self.unified:
            return True
        cached = getattr(self, "_anchors_supported", None)
        if cached is not None:
            return cached
        try:
            cols = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(facts)").fetchall()}
            supported = "anchor_type" in cols
        except Exception:
            supported = False
        self._anchors_supported = supported
        return supported

    def set_anchor(self, fact_id: str, anchor_type: str,
                   anchor_note: str = "") -> bool:
        """Mark a fact as an emotional anchor.

        Returns True on success, False if the fact is missing or schema lacks
        the anchor columns (legacy per-personality DB)."""
        if not self.anchors_supported():
            return False
        if anchor_type not in self.ANCHOR_TYPES:
            raise ValueError(
                f"anchor_type must be one of {self.ANCHOR_TYPES}, got {anchor_type!r}"
            )
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """UPDATE facts SET anchor_type = ?, anchor_note = ?, anchor_at = ?
               WHERE id = ?""",
            (anchor_type, anchor_note, now, fact_id),
        )
        return cur.rowcount > 0

    def clear_anchor(self, fact_id: str) -> bool:
        if not self.anchors_supported():
            return False
        cur = self.conn.execute(
            "UPDATE facts SET anchor_type = NULL, anchor_note = NULL, anchor_at = NULL WHERE id = ?",
            (fact_id,),
        )
        return cur.rowcount > 0

    def get_anchors(self, anchor_type: str | None = None) -> list[dict]:
        """All currently-anchored facts, newest anchor first."""
        if not self.anchors_supported():
            return []
        if anchor_type:
            rows = self.conn.execute(
                """SELECT * FROM facts WHERE anchor_type = ?
                   AND archived = 0 AND forgotten = 0
                   ORDER BY anchor_at DESC""",
                (anchor_type,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM facts WHERE anchor_type IS NOT NULL
                   AND archived = 0 AND forgotten = 0
                   ORDER BY anchor_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def add_relationship(self, fact_id: str, related_id: str) -> None:
        """Add a relationship edge between two facts (P2-16: edge table;
        a no-op when the source fact doesn't exist, matching the old
        JSON-column behavior)."""
        with self.write_transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if exists is None:
                return
            conn.execute(
                "INSERT OR IGNORE INTO fact_edges (fact_id, related_id, created_at) "
                "VALUES (?, ?, ?)",
                (fact_id, related_id,
                 datetime.now(timezone.utc).isoformat()),
            )

    def get_related_ids(self, fact_id: str) -> list[str]:
        """Get IDs of facts related to this one (edge table)."""
        rows = self.conn.execute(
            "SELECT related_id FROM fact_edges WHERE fact_id = ?", (fact_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def get_session_neighbors(self, fact_id: str, session_id: str,
                              n: int = 2) -> list[dict]:
        """Get facts from the same session, nearest in time to the given fact."""
        if not session_id:
            return []
        rows = self.conn.execute(
            """SELECT * FROM facts
               WHERE session_id = ? AND id != ?
                 AND forgotten = 0 AND archived = 0 AND superseded_by IS NULL
               ORDER BY ABS(JULIANDAY(created_at) - JULIANDAY(
                   (SELECT created_at FROM facts WHERE id = ?)
               ))
               LIMIT ?""",
            (session_id, fact_id, fact_id, n),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_fact_by_id(self, fact_id: str) -> dict | None:
        """Get a single fact by content_hash ID."""
        row = self.conn.execute(
            "SELECT * FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_fact_by_text(self, text: str) -> dict | None:
        """Find a fact by substring match on the fact text."""
        row = self.conn.execute(
            "SELECT * FROM facts WHERE LOWER(fact) LIKE ? AND forgotten = 0 AND superseded_by IS NULL LIMIT 1",
            (f"%{text.lower()}%",),
        ).fetchone()
        return dict(row) if row else None

    # ── Decision Outcomes ──

    def insert_outcome(self, decision_id: int, outcome: str,
                       success: bool | None = None) -> dict:
        """Record an outcome for a decision."""
        now = datetime.now(timezone.utc).isoformat()
        success_int = None if success is None else (1 if success else 0)
        cursor = self.conn.execute(
            """INSERT INTO decision_outcomes (decision_id, outcome, success, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (decision_id, outcome, success_int, now),
        )
        self.conn.commit()
        return {
            "id": cursor.lastrowid,
            "decision_id": decision_id,
            "outcome": outcome,
            "success": success,
            "recorded_at": now,
        }

    def get_outcomes(self, decision_id: int) -> list[dict]:
        """Get all outcomes for a decision."""
        rows = self.conn.execute(
            "SELECT * FROM decision_outcomes WHERE decision_id = ? ORDER BY recorded_at",
            (decision_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_decisions_with_outcomes(self, project: str | None = None,
                                   limit: int = 20) -> list[dict]:
        """Get recent decisions with their outcomes."""
        conditions = []
        params: list = []
        pred, pred_params = self._personality_predicate("d.personality")
        if pred:
            conditions.append(pred)
            params.extend(pred_params)
        if project:
            conditions.append("d.project = ?")
            params.append(project.strip().lower())
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        sql = f"""
            SELECT d.*, GROUP_CONCAT(do.outcome, ' | ') AS outcomes,
                   GROUP_CONCAT(do.success, ',') AS outcome_successes
            FROM decisions d
            LEFT JOIN decision_outcomes do ON d.id = do.decision_id
            {where}
            GROUP BY d.id
            ORDER BY d.created_at DESC
            LIMIT ?
        """
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _decision_query_tokens(query: str) -> list[str]:
        """Tokenize a decision query for fuzzy matching.

        Splits on non-alphanumerics so operator-looking characters
        ('+', '-', ':', …) can never break matching, lowercases, and
        drops trivial words."""
        import re as _re
        stop = {"the", "a", "an", "to", "of", "and", "or", "for", "in",
                "on", "we", "i", "is", "it", "that", "this", "with"}
        tokens = [t for t in _re.split(r"[^a-z0-9]+", query.lower()) if t]
        meaningful = [t for t in tokens if t not in stop]
        return meaningful or tokens

    def _score_decisions(self, query: str, project: str | None = None,
                         scan_limit: int = 100) -> list[tuple[float, dict]]:
        """Score recent decisions by word overlap with the query.

        Case-insensitive substring scoring over the most recent
        ``scan_limit`` decisions: each query token found in decision or
        reasoning text scores 1. Returns [(score_fraction, decision), …]
        sorted by score then recency. Robust against FTS/LIKE operator
        breakage ('+', hyphens, ':') and word-order differences."""
        tokens = self._decision_query_tokens(query)
        if not tokens:
            return []
        conditions = []
        params: list = []
        pred, pred_params = self._personality_predicate()
        if pred:
            conditions.append(pred)
            params.extend(pred_params)
        if project:
            conditions.append("(project = ? OR project = 'global')")
            params.append(project.strip().lower())
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(scan_limit)
        rows = self.conn.execute(
            f"SELECT * FROM decisions {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        scored: list[tuple[float, dict]] = []
        for r in rows:
            d = dict(r)
            haystack = (
                (d.get("decision") or "") + " " + (d.get("reasoning") or "")
            ).lower()
            hits = sum(1 for t in tokens if t in haystack)
            if hits:
                scored.append((hits / len(tokens), d))
        # Stable sort: best score first; rows are already newest-first so
        # ties keep recency order.
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored

    def find_decision(self, query: str, project: str | None = None) -> dict | None:
        """Find a decision by keyword match.

        Two stages:
        1. Exact phrase: case-insensitive substring on decision text.
        2. Fuzzy fallback: token scoring over the last 100 decisions —
           accepts the best match when at least half the meaningful query
           words appear. Handles queries whose words appear out of order
           or separated in the decision text ('Nebula CORS pyproject AGPL')
           and special characters ('Aleph+Null', 'Week-1 P0s') that broke
           the old single-LIKE lookup.
        """
        conditions = ["LOWER(decision) LIKE ?"]
        params: list = [f"%{query.lower()}%"]
        pred, pred_params = self._personality_predicate()
        if pred:
            conditions.append(pred)
            params.extend(pred_params)
        if project:
            conditions.append("(project = ? OR project = 'global')")
            params.append(project.strip().lower())
        where = " AND ".join(conditions)
        row = self.conn.execute(
            f"SELECT * FROM decisions WHERE {where} ORDER BY created_at DESC LIMIT 1",
            params,
        ).fetchone()
        if row:
            return dict(row)

        scored = self._score_decisions(query, project=project)
        if scored and scored[0][0] >= 0.5:
            return scored[0][1]
        return None

    def find_decision_candidates(self, query: str, project: str | None = None,
                                 limit: int = 3) -> list[dict]:
        """Top near-miss decision candidates for a query (any token match).

        Used to surface 'did you mean one of these?' instead of a bare
        'No decision matching' when find_decision() comes up empty."""
        scored = self._score_decisions(query, project=project)
        return [d for _score, d in scored[:limit]]

    def count_outcomes(self) -> int:
        """Count recorded outcomes."""
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM decision_outcomes").fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            return 0  # Table doesn't exist yet

    # ── Hypnos Journal ──

    def insert_hypnos_entry(self, run_id: str, stage: str, action: str,
                            fact_id: str | None = None,
                            detail: str | None = None) -> None:
        """Record a Hypnos journal entry."""
        now = datetime.now(timezone.utc).isoformat()
        if self.unified:
            self.conn.execute(
                """INSERT INTO hypnos_journal (personality, run_id, started_at,
                   stage, action, fact_id, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (self.personality, run_id, now, stage, action, fact_id, detail),
            )
            return
        self.conn.execute(
            """INSERT INTO hypnos_journal (run_id, started_at, stage, action, fact_id, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, now, stage, action, fact_id, detail),
        )

    def get_latest_hypnos_run(self) -> list[dict]:
        """Get all journal entries for the most recent Hypnos run
        (this personality's runs only on unified stores)."""
        pred, params = self._personality_predicate()
        where = f" WHERE {pred}" if pred else ""
        row = self.conn.execute(
            f"SELECT run_id FROM hypnos_journal{where} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return []
        run_id = row[0]
        and_pred = f" AND {pred}" if pred else ""
        rows = self.conn.execute(
            f"SELECT * FROM hypnos_journal WHERE run_id = ?{and_pred} ORDER BY id",
            (run_id, *params),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_hypnos_runs(self, limit: int = 10) -> list[dict]:
        """Get summary of recent Hypnos runs (this personality's runs
        only on unified stores)."""
        pred, params = self._personality_predicate()
        where = f" WHERE {pred}" if pred else ""
        rows = self.conn.execute(
            f"""SELECT run_id, MIN(started_at) AS started_at, COUNT(*) AS entry_count
               FROM hypnos_journal
               {where}
               GROUP BY run_id
               ORDER BY MIN(id) DESC
               LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Decision Feed (Cross-Instance Coordination) ──

    def insert_decision_feed(self, entry: dict) -> int:
        """Write a decision to the cross-instance feed."""
        if self.unified:
            cursor = self.conn.execute(
                """INSERT INTO decision_feed
                   (session_id, personality, decision, reasoning, project, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.get("session_id", "unknown"),
                    self.personality,
                    entry["decision"],
                    entry.get("reasoning", ""),
                    entry.get("project", "global"),
                    entry.get("status", "provisional"),
                    entry["created_at"],
                ),
            )
            return cursor.lastrowid
        cursor = self.conn.execute(
            """INSERT INTO decision_feed
               (session_id, decision, reasoning, project, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                entry.get("session_id", "unknown"),
                entry["decision"],
                entry.get("reasoning", ""),
                entry.get("project", "global"),
                entry.get("status", "provisional"),
                entry["created_at"],
            ),
        )
        return cursor.lastrowid

    def get_decision_feed(self, project: str | None = None,
                          exclude_session: str | None = None,
                          limit: int = 20) -> list[dict]:
        """Get recent decisions from the feed, optionally excluding current session.

        Scoped to this personality on unified stores — the feed shows
        OTHER SESSIONS of the same personality, not other personalities.
        """
        conditions = []
        params: list = []
        pred, pred_params = self._personality_predicate()
        if pred:
            conditions.append(pred)
            params.extend(pred_params)
        if project:
            conditions.append("(project = ? OR project = 'global')")
            params.append(project.strip().lower())
        if exclude_session:
            conditions.append("session_id != ?")
            params.append(exclude_session)
        params.append(limit)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""SELECT * FROM decision_feed {where}
                  ORDER BY created_at DESC LIMIT ?"""
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_decision_feed(self, query: str, project: str | None = None,
                             exclude_session: str | None = None,
                             limit: int = 5) -> list[dict]:
        """Search decision feed by keyword (any term matches)."""
        conditions = []
        params: list = []
        pred, pred_params = self._personality_predicate()
        if pred:
            conditions.append(pred)
            params.extend(pred_params)
        terms = query.lower().split()
        # OR logic — any term matching is enough
        term_conds = []
        for term in terms[:5]:
            term_conds.append("(LOWER(decision) LIKE ? OR LOWER(reasoning) LIKE ?)")
            params.extend([f"%{term}%", f"%{term}%"])
        if term_conds:
            conditions.append("(" + " OR ".join(term_conds) + ")")
        if project:
            conditions.append("(project = ? OR project = 'global')")
            params.append(project.strip().lower())
        if exclude_session:
            conditions.append("session_id != ?")
            params.append(exclude_session)
        params.append(limit)
        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM decision_feed WHERE {where} ORDER BY created_at DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ── Session Fingerprints ──

    def insert_fingerprint(self, fp: dict) -> None:
        """Store a session fingerprint."""
        if self.unified:
            self.conn.execute(
                """INSERT OR REPLACE INTO session_fingerprints
                   (session_id, personality, project, duration_minutes, facts_count,
                    decisions_count, mistakes_count, tier_dist, topic_vector, outcome,
                    tags, energy_arc, highlights, created_at,
                    identity_vector, identity_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fp["session_id"],
                    self.personality,
                    fp.get("project", "global"),
                    fp.get("duration_minutes", 0),
                    fp.get("facts_count", 0),
                    fp.get("decisions_count", 0),
                    fp.get("mistakes_count", 0),
                    json.dumps(fp.get("tier_dist", {})),
                    fp.get("topic_vector"),
                    fp.get("outcome", "neutral"),
                    json.dumps(fp.get("tags", [])),
                    fp.get("energy_arc", ""),
                    json.dumps(fp.get("highlights", [])),
                    fp["created_at"],
                    fp.get("identity_vector"),
                    fp.get("identity_model", ""),
                ),
            )
            return
        self.conn.execute(
            """INSERT OR REPLACE INTO session_fingerprints
               (session_id, project, duration_minutes, facts_count, decisions_count,
                mistakes_count, tier_dist, topic_vector, outcome, tags,
                energy_arc, highlights, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fp["session_id"],
                fp.get("project", "global"),
                fp.get("duration_minutes", 0),
                fp.get("facts_count", 0),
                fp.get("decisions_count", 0),
                fp.get("mistakes_count", 0),
                json.dumps(fp.get("tier_dist", {})),
                fp.get("topic_vector"),
                fp.get("outcome", "neutral"),
                json.dumps(fp.get("tags", [])),
                fp.get("energy_arc", ""),
                json.dumps(fp.get("highlights", [])),
                fp["created_at"],
            ),
        )

    def get_fingerprints(self, project: str | None = None,
                         limit: int = 50) -> list[dict]:
        """Get session fingerprints, optionally filtered by project.
        Scoped to this personality on unified stores."""
        pred, pred_params = self._personality_predicate()
        and_pred = f" AND {pred}" if pred else ""
        where_pred = f" WHERE {pred}" if pred else ""
        if project:
            rows = self.conn.execute(
                f"""SELECT * FROM session_fingerprints
                   WHERE (project = ? OR project = 'global'){and_pred}
                   ORDER BY created_at DESC LIMIT ?""",
                (project.strip().lower(), *pred_params, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT * FROM session_fingerprints{where_pred} "
                f"ORDER BY created_at DESC LIMIT ?",
                (*pred_params, limit),
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["tier_dist"] = json.loads(d.get("tier_dist") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["tier_dist"] = {}
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
            results.append(d)
        return results

    # ── Diagnostics (for null doctor) ──

    def diagnose(self) -> dict[str, Any]:
        """Run diagnostics on memory health. Returns dict of findings."""
        findings: dict[str, Any] = {}

        # Total counts
        findings["total_facts"] = self.count_facts(active_only=False)
        findings["active_facts"] = self.count_facts(active_only=True)
        findings["decisions"] = self.count_decisions()
        findings["mistakes"] = self.count_mistakes()
        findings["reflections"] = self.count_reflections()

        # Archived/forgotten/superseded
        row = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE archived = 1"
        ).fetchone()
        findings["archived_facts"] = row[0] if row else 0

        row = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE forgotten = 1"
        ).fetchone()
        findings["forgotten_facts"] = row[0] if row else 0

        row = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE superseded_by IS NOT NULL"
        ).fetchone()
        findings["superseded_facts"] = row[0] if row else 0

        # Project name variants
        rows = self.conn.execute(
            "SELECT DISTINCT project FROM facts UNION SELECT DISTINCT project FROM decisions"
        ).fetchall()
        projects = [r[0] for r in rows]
        findings["projects"] = projects

        # Test data detection (archived rows are already handled)
        row = self.conn.execute(
            """SELECT COUNT(*) FROM mistakes
               WHERE (mistake LIKE '%test mistake%' OR mistake LIKE '%test reason%')
                 AND archived = 0"""
        ).fetchone()
        findings["test_mistakes"] = row[0] if row else 0

        row = self.conn.execute(
            """SELECT COUNT(*) FROM reflections
               WHERE went_well = 'went well' AND missed = 'was missed'
                 AND archived = 0"""
        ).fetchone()
        findings["test_reflections"] = row[0] if row else 0

        row = self.conn.execute(
            """SELECT COUNT(*) FROM facts
               WHERE project = 'myproject'
                 AND archived = 0 AND forgotten = 0"""
        ).fetchone()
        findings["test_facts"] = row[0] if row else 0

        # Tier breakdown
        try:
            tier_rows = self.conn.execute(
                """SELECT tier, COUNT(*) FROM facts
                   WHERE forgotten = 0 AND archived = 0 AND superseded_by IS NULL
                   GROUP BY tier"""
            ).fetchall()
            findings["tiers"] = {row[0] or "contextual": row[1] for row in tier_rows}
        except sqlite3.OperationalError:
            findings["tiers"] = {}

        # Stale facts (no access in 60+ days)
        row = self.conn.execute(
            """SELECT COUNT(*) FROM facts
               WHERE access_count = 0
                 AND forgotten = 0 AND archived = 0 AND superseded_by IS NULL
                 AND created_at < datetime('now', '-60 days')"""
        ).fetchone()
        findings["stale_facts"] = row[0] if row else 0

        return findings

    def fix_hygiene(self, dry_run: bool = False) -> dict[str, int]:
        """Fix common data quality issues. Returns counts of fixes applied.

        Mistakes and reflections are NEVER hard-deleted (product invariant)
        — test data is soft-archived instead, so it stays in the table but
        is excluded from active queries.
        """
        fixes: dict[str, int] = {
            "test_mistakes_archived": 0,
            "test_reflections_archived": 0,
            "test_facts_archived": 0,
            "projects_normalized": 0,
        }

        if dry_run:
            # Just count what would change
            row = self.conn.execute(
                "SELECT COUNT(*) FROM mistakes WHERE mistake LIKE '%test mistake%' AND archived = 0"
            ).fetchone()
            fixes["test_mistakes_archived"] = row[0] if row else 0
            row = self.conn.execute(
                "SELECT COUNT(*) FROM reflections WHERE went_well = 'went well' AND missed = 'was missed' AND archived = 0"
            ).fetchone()
            fixes["test_reflections_archived"] = row[0] if row else 0
            row = self.conn.execute(
                "SELECT COUNT(*) FROM facts WHERE project = 'myproject'"
            ).fetchone()
            fixes["test_facts_archived"] = row[0] if row else 0
            return fixes

        # Archive test mistakes (soft — never DELETE from mistakes)
        cursor = self.conn.execute(
            "UPDATE mistakes SET archived = 1 WHERE mistake LIKE '%test mistake%' AND archived = 0"
        )
        fixes["test_mistakes_archived"] = cursor.rowcount

        # Archive test reflections (soft — never DELETE from reflections)
        cursor = self.conn.execute(
            "UPDATE reflections SET archived = 1 WHERE went_well = 'went well' AND missed = 'was missed' AND archived = 0"
        )
        fixes["test_reflections_archived"] = cursor.rowcount

        # Archive test facts
        cursor = self.conn.execute(
            "UPDATE facts SET archived = 1 WHERE project = 'myproject'"
        )
        fixes["test_facts_archived"] = cursor.rowcount

        self.conn.commit()
        return fixes


# ── Migration ──

def _load_jsonl(path: str) -> list[dict]:
    """Load a JSONL file, skipping malformed lines."""
    if not os.path.isfile(path):
        return []
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def migrate_jsonl_to_sqlite(
    agent_dir: str, personality: str = "atlas",
) -> dict[str, int]:
    """Migrate JSONL files to SQLite database.

    Returns dict with counts of migrated entries per table.
    Renames old JSONL files to .jsonl.bak for recovery.

    ``personality`` scopes the store's heal/backfill attribution — pass
    the store's own personality when migrating a non-atlas store
    (init-path bleed audit); the default is back-compat for the legacy
    flat-layout atlas store.
    """
    db = NullDB(agent_dir, personality=personality)
    db.initialize()

    counts: dict[str, int] = {}

    # Migrate knowledge
    knowledge = _load_jsonl(os.path.join(agent_dir, "knowledge.jsonl"))
    # Deduplicate by content_hash (last-write-wins, matching JSONL behavior)
    seen: dict[str, dict] = {}
    for entry in knowledge:
        content_hash = entry.get("content_hash", "")
        if content_hash:
            seen[content_hash] = entry
        else:
            # Generate hash for entries without one
            import hashlib
            h = hashlib.sha256(entry.get("fact", "").strip().lower().encode()).hexdigest()[:12]
            entry["content_hash"] = h
            seen[h] = entry

    for entry in seen.values():
        ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
        db.insert_fact({
            "id": entry.get("content_hash", ""),
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
    counts["facts"] = len(seen)

    # Migrate decisions
    decisions = _load_jsonl(os.path.join(agent_dir, "decisions.jsonl"))
    for entry in decisions:
        ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
        db.insert_decision({
            "decision": entry.get("decision", ""),
            "reasoning": entry.get("reasoning", ""),
            "project": entry.get("project", "global"),
            "session_id": entry.get("session_id"),
            "created_at": ts,
        })
    counts["decisions"] = len(decisions)

    # Migrate mistakes
    mistakes = _load_jsonl(os.path.join(agent_dir, "mistakes.jsonl"))
    for entry in mistakes:
        ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
        db.insert_mistake({
            "mistake": entry.get("mistake", ""),
            "why": entry.get("why", ""),
            "project": entry.get("project", "global"),
            "confidence": entry.get("confidence", 0.95),
            "session_id": entry.get("session_id"),
            "created_at": ts,
        })
    counts["mistakes"] = len(mistakes)

    # Migrate reflections
    reflections = _load_jsonl(os.path.join(agent_dir, "reflections.jsonl"))
    for entry in reflections:
        ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
        db.insert_reflection({
            "went_well": entry.get("went_well", ""),
            "missed": entry.get("missed", ""),
            "do_differently": entry.get("do_differently", ""),
            "project": entry.get("project", "global"),
            "session_id": entry.get("session_id"),
            "created_at": ts,
        })
    counts["reflections"] = len(reflections)

    # Migrate exemplars
    exemplars = _load_jsonl(os.path.join(agent_dir, "exemplars.jsonl"))
    for entry in exemplars:
        db.insert_exemplar(entry)
    counts["exemplars"] = len(exemplars)

    db.conn.commit()

    # Rename JSONL files to .bak
    for filename in ["knowledge.jsonl", "decisions.jsonl", "mistakes.jsonl",
                     "reflections.jsonl", "exemplars.jsonl", "archive.jsonl"]:
        src = os.path.join(agent_dir, filename)
        if os.path.isfile(src):
            dst = src + ".bak"
            os.rename(src, dst)

    db.close()
    return counts


def merge_jsonl_into_sqlite(
    agent_dir: str, personality: str = "atlas",
) -> dict[str, int]:
    """Merge any JSONL entries not yet in SQLite.

    Used to reconcile split-brain state when old instances wrote to JSONL
    while new instances wrote to SQLite. Additive only — never overwrites
    existing SQLite data.

    After merge, renames JSONL files to .jsonl.merged for audit trail.
    Returns counts of new entries merged per table.

    ``personality`` scopes the store's heal/backfill attribution (see
    migrate_jsonl_to_sqlite) — back-compat default for the atlas store.
    """
    import hashlib

    db = NullDB(agent_dir, personality=personality)
    db.initialize()

    counts: dict[str, int] = {"facts": 0, "decisions": 0, "mistakes": 0,
                               "reflections": 0, "exemplars": 0}

    # Merge knowledge — deduplicate by content_hash
    knowledge = _load_jsonl(os.path.join(agent_dir, "knowledge.jsonl"))
    for entry in knowledge:
        content_hash = entry.get("content_hash", "")
        if not content_hash:
            proj = entry.get("project", "global").strip().lower()
            fact_text = entry.get("fact", "").strip().lower()
            content_hash = hashlib.sha256(
                f"{proj}:{fact_text}".encode()
            ).hexdigest()[:12]

        # Skip if already in SQLite
        if db.get_fact_by_id(content_hash) is not None:
            continue

        ts = entry.get("created_at", entry.get("ts", datetime.now(timezone.utc).isoformat()))
        db.insert_fact({
            "id": content_hash,
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
        counts["facts"] += 1

    # Merge decisions — deduplicate by exact decision text + timestamp
    decisions = _load_jsonl(os.path.join(agent_dir, "decisions.jsonl"))
    existing_decisions = {
        (d["decision"], d["created_at"])
        for d in db.get_decisions()
    }
    for entry in decisions:
        ts = entry.get("created_at", entry.get("ts", ""))
        key = (entry.get("decision", ""), ts)
        if key in existing_decisions:
            continue
        db.insert_decision({
            "decision": entry.get("decision", ""),
            "reasoning": entry.get("reasoning", ""),
            "project": entry.get("project", "global"),
            "session_id": entry.get("session_id"),
            "created_at": ts or datetime.now(timezone.utc).isoformat(),
        })
        existing_decisions.add(key)
        counts["decisions"] += 1

    # Merge mistakes — deduplicate by exact text + timestamp
    mistakes = _load_jsonl(os.path.join(agent_dir, "mistakes.jsonl"))
    existing_mistakes = {
        (m["mistake"], m["created_at"])
        for m in db.get_mistakes()
    }
    for entry in mistakes:
        ts = entry.get("created_at", entry.get("ts", ""))
        key = (entry.get("mistake", ""), ts)
        if key in existing_mistakes:
            continue
        db.insert_mistake({
            "mistake": entry.get("mistake", ""),
            "why": entry.get("why", ""),
            "project": entry.get("project", "global"),
            "confidence": entry.get("confidence", 0.95),
            "session_id": entry.get("session_id"),
            "created_at": ts or datetime.now(timezone.utc).isoformat(),
        })
        existing_mistakes.add(key)
        counts["mistakes"] += 1

    # Merge reflections — deduplicate by exact text + timestamp
    reflections = _load_jsonl(os.path.join(agent_dir, "reflections.jsonl"))
    existing_reflections = {
        (r.get("went_well", ""), r["created_at"])
        for r in db.get_reflections()
    }
    for entry in reflections:
        ts = entry.get("created_at", entry.get("ts", ""))
        key = (entry.get("went_well", ""), ts)
        if key in existing_reflections:
            continue
        db.insert_reflection({
            "went_well": entry.get("went_well", ""),
            "missed": entry.get("missed", ""),
            "do_differently": entry.get("do_differently", ""),
            "project": entry.get("project", "global"),
            "session_id": entry.get("session_id"),
            "created_at": ts or datetime.now(timezone.utc).isoformat(),
        })
        existing_reflections.add(key)
        counts["reflections"] += 1

    # Merge exemplars — deduplicate by user_text (legacy JSONL key: pete)
    exemplars = _load_jsonl(os.path.join(agent_dir, "exemplars.jsonl"))
    existing_exemplar_texts = {
        e.get("user_text", e.get("pete", "")) for e in db.get_exemplars()
    }
    for entry in exemplars:
        user_text = entry.get("user_text", entry.get("pete", ""))
        if user_text in existing_exemplar_texts:
            continue
        db.insert_exemplar(entry)
        existing_exemplar_texts.add(user_text)
        counts["exemplars"] += 1

    db.conn.commit()

    # Rename JSONL files to .merged (different suffix from .bak to distinguish)
    has_new_data = any(v > 0 for v in counts.values())
    if has_new_data:
        for filename in ["knowledge.jsonl", "decisions.jsonl", "mistakes.jsonl",
                         "reflections.jsonl", "exemplars.jsonl"]:
            src = os.path.join(agent_dir, filename)
            if os.path.isfile(src):
                dst = src + ".merged"
                os.rename(src, dst)

    db.close()
    return counts
