"""Null Memory v3 — Unified Substrate Migration.

Consolidates the multiverse's per-personality DBs into a single SQLite database
where personality is a column, not a directory. Sidecar by design: produces
``~/.null/unified.db`` without touching any live DB. Phase 1b (separate session)
performs the agent.py cutover.

Source DBs (all schema versions handled, v2..v11):
    ~/.null/atlas/memory.db                   (canonical, schema v11)
    ~/.null/personalities/mercury/memory.db   (schema v9)
    ~/.null/personalities/logos/memory.db     (schema v7)
    ~/.null/personalities/cybil/memory.db     (schema v9, empty)
    ~/.null/memory.db                         (schema v2 orphan, attributed to atlas)
    ~/.null/multiverse.db                     (registry, broadcasts, dreams, xrefs)

Target: ~/.null/unified.db, schema v12. See SCHEMA_SQL below for shape.

Idempotent: re-running with --force rebuilds from scratch; without --force,
refuses if migration_complete=1 in meta.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

UNIFIED_SCHEMA_VERSION = 24

NULL_HOME = os.path.expanduser("~/.null")
DEFAULT_UNIFIED_PATH = os.path.join(NULL_HOME, "unified.db")

# Personality migration order. Atlas first because it's the richest source —
# its metadata wins on dedup conflicts (earliest created_at, highest access_count
# still merge correctly regardless of order, but having Atlas first makes
# debug logs easier to read).
PERSONALITY_SOURCES: list[tuple[str, str]] = [
    ("atlas", os.path.join(NULL_HOME, "atlas", "memory.db")),
    ("mercury", os.path.join(NULL_HOME, "personalities", "mercury", "memory.db")),
    ("logos", os.path.join(NULL_HOME, "personalities", "logos", "memory.db")),
    ("cybil", os.path.join(NULL_HOME, "personalities", "cybil", "memory.db")),
]

# Orphan root DB at ~/.null/memory.db — pre-multiverse. Folded into atlas.
ORPHAN_ROOT_DB = os.path.join(NULL_HOME, "memory.db")
ORPHAN_ATTRIBUTED_TO = "atlas"

MULTIVERSE_DB = os.path.join(NULL_HOME, "multiverse.db")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- ── Registry ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS personalities (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL DEFAULT 'worker',
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    bootstrapped_from TEXT,
    description TEXT,
    focus TEXT
);

-- ── Shared truth ─────────────────────────────────────────────────────────
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
    tier TEXT DEFAULT 'contextual',
    -- v13: emotional anchors — load-bearing memories that never decay.
    -- anchor_type ∈ {origin, commitment, loss, joy, turning_point}, NULL = not an anchor.
    anchor_type TEXT,
    anchor_note TEXT,
    anchor_at TEXT,
    -- v20: crystallization lineage. Set by Hypnos Stage 4.5.
    crystallized_from TEXT,    -- parent fact id (on children)
    crystallized_into TEXT     -- JSON list of child ids (on parents)
);
-- idx_facts_anchor created by _apply_unified_upgrades after ALTER TABLE runs.

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    fact,
    content='facts',
    content_rowid='rowid',
    tokenize='unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_trigram USING fts5(
    fact,
    content='facts',
    content_rowid='rowid',
    tokenize='trigram'
);

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

-- ── Per-personality overlay on shared facts ──────────────────────────────
CREATE TABLE IF NOT EXISTS personality_views (
    fact_id TEXT NOT NULL,
    personality TEXT NOT NULL,
    salience_override REAL,
    confidence_override REAL,
    hidden INTEGER DEFAULT 0,
    last_accessed TEXT,
    access_count INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]',
    PRIMARY KEY (fact_id, personality),
    FOREIGN KEY (fact_id) REFERENCES facts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pviews_personality ON personality_views(personality);
CREATE INDEX IF NOT EXISTS idx_pviews_fact ON personality_views(fact_id);

-- ── Attributed records (personality column added) ────────────────────────
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision TEXT NOT NULL,
    reasoning TEXT,
    project TEXT DEFAULT 'global',
    personality TEXT NOT NULL,
    session_id TEXT,
    trace TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    src_id INTEGER  -- original id from source DB, for traceability
);
CREATE INDEX IF NOT EXISTS idx_decisions_personality ON decisions(personality);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);

CREATE TABLE IF NOT EXISTS mistakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mistake TEXT NOT NULL,
    why TEXT,
    project TEXT DEFAULT 'global',
    personality TEXT NOT NULL,
    confidence REAL DEFAULT 0.95,
    session_id TEXT,
    created_at TEXT NOT NULL,
    viz_x REAL,
    viz_y REAL,
    viz_z REAL,
    archived INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mistakes_personality ON mistakes(personality);
CREATE INDEX IF NOT EXISTS idx_mistakes_project ON mistakes(project);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    went_well TEXT,
    missed TEXT,
    do_differently TEXT,
    project TEXT DEFAULT 'global',
    personality TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    archived INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reflections_personality ON reflections(personality);

-- v23: user_text/agent_text — formerly pete/atlas
CREATE TABLE IF NOT EXISTS exemplars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario TEXT,
    user_text TEXT NOT NULL,
    agent_text TEXT,
    calibration TEXT,
    tags TEXT,
    personality TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exemplars_personality ON exemplars(personality);

-- session_id is unique per personality but could collide across; composite PK.
CREATE TABLE IF NOT EXISTS session_fingerprints (
    session_id TEXT NOT NULL,
    personality TEXT NOT NULL,
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
    created_at TEXT NOT NULL,
    -- v13: identity vector — embedding of Atlas behavioral signature this session
    -- (decisions + reasoning + reflections + anchor touches). Enables drift detection.
    identity_vector BLOB,
    identity_model TEXT,
    PRIMARY KEY (session_id, personality)
);
CREATE INDEX IF NOT EXISTS idx_fingerprints_personality ON session_fingerprints(personality);
CREATE INDEX IF NOT EXISTS idx_fingerprints_project ON session_fingerprints(project);

CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    expected TEXT NOT NULL,
    fact_id TEXT,
    probe_type TEXT DEFAULT 'user',
    personality TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_run TEXT,
    last_result TEXT,
    run_count INTEGER DEFAULT 0,
    pass_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_probes_personality ON probes(personality);
CREATE INDEX IF NOT EXISTS idx_probes_type ON probes(probe_type);
CREATE INDEX IF NOT EXISTS idx_probes_fact ON probes(fact_id);

CREATE TABLE IF NOT EXISTS decision_feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    personality TEXT NOT NULL,
    decision TEXT NOT NULL,
    reasoning TEXT,
    project TEXT DEFAULT 'global',
    status TEXT DEFAULT 'provisional',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_feed_personality ON decision_feed(personality);
CREATE INDEX IF NOT EXISTS idx_decision_feed_project ON decision_feed(project);
CREATE INDEX IF NOT EXISTS idx_decision_feed_session ON decision_feed(session_id);

-- decision_outcomes references decisions(id) — but decision IDs are remapped
-- during migration, so we store new_decision_id pointing at the unified row.
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    success INTEGER,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_decision ON decision_outcomes(decision_id);

CREATE TABLE IF NOT EXISTS hypnos_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    personality TEXT NOT NULL,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    action TEXT NOT NULL,
    fact_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_hypnos_personality ON hypnos_journal(personality);
CREATE INDEX IF NOT EXISTS idx_hypnos_run ON hypnos_journal(run_id);
CREATE INDEX IF NOT EXISTS idx_hypnos_started ON hypnos_journal(started_at);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    personality TEXT NOT NULL,
    run_at TEXT NOT NULL,
    score REAL NOT NULL,
    metrics TEXT NOT NULL,
    notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evaluations_personality ON evaluations(personality);

-- ── Shared, derived ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fact_embeddings (
    fact_id TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (fact_id) REFERENCES facts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON fact_embeddings(model);

-- ── Multiverse-wide ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    source TEXT NOT NULL,
    targets TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dreams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis TEXT NOT NULL,
    source_facts TEXT NOT NULL,
    confidence REAL,
    status TEXT DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS xrefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS xref_facts (
    xref_id INTEGER NOT NULL,
    personality TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (xref_id, personality, fact_id),
    FOREIGN KEY (xref_id) REFERENCES xrefs(id) ON DELETE CASCADE
);

-- Migration audit trail
CREATE TABLE IF NOT EXISTS migration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);

-- v19: on-boot identity verification.
-- Each MCP-server boot computes a coherence score (cosine similarity of
-- the bootstrapping payload's identity vector vs the historical centroid)
-- and persists it here. Read by `null doctor` / nebula to surface drift.
CREATE TABLE IF NOT EXISTS session_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    personality TEXT NOT NULL,
    boot_time TEXT NOT NULL,
    coherence_score REAL,
    verified INTEGER DEFAULT 0,
    sample_size INTEGER DEFAULT 0,
    identity_payload_hash TEXT,
    identity_model TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_verifications_session
    ON session_verifications(session_id);
CREATE INDEX IF NOT EXISTS idx_session_verifications_personality
    ON session_verifications(personality);

-- v21: doc_claims — extracted claims from worktree docs (CLAUDE.md,
-- handoff files) about live system state. Used to detect drift between
-- documentation and reality, and to refute stale claims at briefing time.
CREATE TABLE IF NOT EXISTS doc_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_seen_at TEXT NOT NULL,
    status TEXT DEFAULT 'unverified',
    refute_evidence TEXT,
    UNIQUE(source_path, claim_text)
);
CREATE INDEX IF NOT EXISTS idx_doc_claims_status ON doc_claims(status);
CREATE INDEX IF NOT EXISTS idx_doc_claims_source ON doc_claims(source_path);

-- Instance presence registry — every live Null process (MCP server, CLI
-- invocation, daemon) registers itself on AgentMemory.load() so multiple
-- instances sharing this store can see each other (the Atlas-fragmentation
-- primitive). Liveness = last_heartbeat within the configured window
-- (db.INSTANCE_LIVE_WINDOW_MINUTES); rows older than db.INSTANCE_GC_DAYS
-- are garbage-collected on registration. No schema_version bump: this
-- table rides the structural verify/heal machinery below, which is
-- deliberately stamp-agnostic (a v24-stamped store missing it self-heals
-- on the next initialize).
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    hostname TEXT,
    pid INTEGER,
    started_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    personality TEXT,
    transport TEXT,            -- 'mcp' | 'cli'
    project TEXT,              -- last known project
    schema_version_seen INTEGER
);
CREATE INDEX IF NOT EXISTS idx_instances_heartbeat ON instances(last_heartbeat);
"""


# ── Helpers ────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _open_source(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _merge_related_to(a: str | None, b: str | None) -> str:
    """Union two JSON-list strings."""
    def parse(v):
        if not v:
            return []
        try:
            r = json.loads(v)
            return r if isinstance(r, list) else []
        except Exception:
            return []
    merged = list(dict.fromkeys(parse(a) + parse(b)))
    return json.dumps(merged)


# ── Stats container ────────────────────────────────────────────────────────


@dataclass
class MigrationStats:
    facts_inserted: int = 0
    facts_merged: int = 0
    views_inserted: int = 0
    decisions: int = 0
    mistakes: int = 0
    reflections: int = 0
    exemplars: int = 0
    fingerprints: int = 0
    probes: int = 0
    decision_feed: int = 0
    decision_outcomes: int = 0
    hypnos: int = 0
    evaluations: int = 0
    embeddings_copied: int = 0
    embeddings_backfilled: int = 0
    personalities: int = 0
    broadcasts: int = 0
    dreams: int = 0
    xrefs: int = 0
    skipped_personalities: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── Structural verification / self-heal (issue #1) ────────────────────────
#
# meta.schema_version is stamped UNIFIED_SCHEMA_VERSION by
# _apply_unified_upgrades, but the unified base layout (personalities table,
# personality columns) is only created by SCHEMA_SQL — which historically ran
# only via init_unified_db / migrate(). A pre-unified per-personality store
# relocated to the unified path (e.g. synced from another machine) therefore
# ended up stamped v24 with the structural migration never having run, and
# identity boot died with `no such table: personalities`. These helpers
# verify the actual structure (never trusting the stamp) and idempotently
# heal it.

# Tables declaring `personality TEXT NOT NULL` in SCHEMA_SQL that also exist
# in the legacy per-personality layout — i.e. the tables that can pre-exist
# WITHOUT the personality column and need ALTER TABLE + backfill. Derived
# from SCHEMA_SQL; keep in sync.
LEGACY_PERSONALITY_TABLES = (
    "decisions",
    "mistakes",
    "reflections",
    "exemplars",
    "session_fingerprints",
    "probes",
    "decision_feed",
    "hypnos_journal",
    "evaluations",
)

# Unified-only tables that SCHEMA_SQL creates whole (personality column
# included). Verified for existence; healed by re-running SCHEMA_SQL.
UNIFIED_ONLY_PERSONALITY_TABLES = (
    "personalities",
    "personality_views",
    "xref_facts",
    "session_verifications",
    "instances",
)


def _iter_schema_statements(script: str):
    """Split a SQL script into complete statements (trigger-safe via
    sqlite3.complete_statement, which understands BEGIN…END bodies)."""
    stmt = ""
    for line in script.splitlines():
        stmt += line + "\n"
        if sqlite3.complete_statement(stmt):
            yield stmt.strip()
            stmt = ""
    if stmt.strip():
        yield stmt.strip()


def verify_unified_structure(conn: sqlite3.Connection) -> list[str]:
    """Return structural problems with a supposedly-unified store.

    Empty list = the store actually has the unified layout. Checks the real
    structure via sqlite_master/PRAGMA — the meta.schema_version stamp is
    deliberately ignored because it can outrun the structural migration.
    """
    problems: list[str] = []
    for table in UNIFIED_ONLY_PERSONALITY_TABLES:
        if not _table_exists(conn, table):
            problems.append(f"missing table: {table}")
    for table in LEGACY_PERSONALITY_TABLES:
        if not _table_exists(conn, table):
            problems.append(f"missing table: {table}")
        elif "personality" not in _columns(conn, table):
            problems.append(f"missing column: {table}.personality")
    return problems


def heal_unified_structure(
    conn: sqlite3.Connection, default_personality: str = "atlas"
) -> list[str]:
    """Idempotently repair a store whose schema_version stamp outran the
    structural migration (issue #1). Safe to run on every boot:

    1. ALTER TABLE ... ADD COLUMN personality for legacy tables, backfilling
       existing rows with ``default_personality`` (the same attribution the
       real migration gives a single-personality store — cf.
       ORPHAN_ATTRIBUTED_TO).
    2. Re-run SCHEMA_SQL (all CREATE ... IF NOT EXISTS) to create any missing
       unified tables/indexes — after step 1 so personality indexes resolve.
    3. Seed the default personality registry row so identity boot has a
       role/focus source (matches multiverse.py's atlas registration).

    Returns the list of repair actions taken; [] means the store was already
    structurally correct (pure no-op — nothing written).
    """
    problems = verify_unified_structure(conn)
    actions: list[str] = []
    if problems:
        # 1. personality columns on pre-existing legacy tables. SQLite's
        #    ADD COLUMN ... NOT NULL DEFAULT backfills existing rows in the
        #    same statement. Identifier is sanitized — it becomes a SQL
        #    string literal in the DDL (DDL can't take bound parameters).
        safe_default = default_personality.replace("'", "''")
        for table in LEGACY_PERSONALITY_TABLES:
            if _table_exists(conn, table) and "personality" not in _columns(conn, table):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN personality TEXT "
                    f"NOT NULL DEFAULT '{safe_default}'"
                )
                actions.append(
                    f"added {table}.personality (backfilled '{default_personality}')"
                )
        # 2. Missing unified tables/indexes/triggers — SCHEMA_SQL is
        #    IF NOT EXISTS throughout, so existing tables are untouched.
        #    Statement-by-statement, fail-soft: an index referencing a
        #    column some degraded legacy table lacks must not abort the
        #    rest of the heal (matches the upgrade runner's style).
        created = [
            t for t in UNIFIED_ONLY_PERSONALITY_TABLES + LEGACY_PERSONALITY_TABLES
            if not _table_exists(conn, t)
        ]
        for stmt in _iter_schema_statements(SCHEMA_SQL):
            try:
                conn.execute(stmt)
            except sqlite3.Error:
                pass
        actions.extend(
            f"created table {t}" for t in created if _table_exists(conn, t)
        )

    # 3. Seed the personality registry row (also covers healthy stores that
    #    somehow lost their row — INSERT only when absent, so still no-op
    #    on correct stores with the row present).
    if _table_exists(conn, "personalities"):
        row = conn.execute(
            "SELECT 1 FROM personalities WHERE name = ?", (default_personality,)
        ).fetchone()
        if row is None and actions:
            role = "manager" if default_personality == "atlas" else "worker"
            description = (
                "Atlas core — the manager" if default_personality == "atlas" else None
            )
            conn.execute(
                """INSERT OR IGNORE INTO personalities
                   (name, role, active, created_at, description)
                   VALUES (?, ?, 1, ?, ?)""",
                (default_personality, role, _now(), description),
            )
            actions.append(f"seeded personalities row '{default_personality}'")

    if actions:
        # Audit trail: meta record for `null doctor` + migration_log entry.
        try:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('structural_heal_last', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_now(),),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('structural_heal_actions', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(actions),),
            )
            _log(conn, "structural_heal", "; ".join(actions))
        except sqlite3.OperationalError:
            pass
        conn.commit()
    return actions


# ── Core migration ─────────────────────────────────────────────────────────


def init_unified_db(path: str = DEFAULT_UNIFIED_PATH) -> sqlite3.Connection:
    """Create or upgrade unified DB to current schema (idempotent)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    _apply_unified_upgrades(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(UNIFIED_SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _apply_unified_upgrades(
    conn: sqlite3.Connection, default_personality: str = "atlas"
) -> None:
    """Idempotent ALTER TABLE upgrades for unified DBs created before the
    current schema version. Each block is independently safe to re-run."""
    # Structural verification first (issue #1): this function stamps
    # schema_version=UNIFIED_SCHEMA_VERSION at the end, so it must never
    # again leave a store stamped current while the unified base layout
    # (personalities table / personality columns) is missing. No-op on
    # structurally-correct stores.
    heal_unified_structure(conn, default_personality=default_personality)
    # v12 → v13: emotional anchors + identity vectors
    existing_fact_cols = _columns(conn, "facts")
    for col, ddl in [
        ("anchor_type", "ALTER TABLE facts ADD COLUMN anchor_type TEXT"),
        ("anchor_note", "ALTER TABLE facts ADD COLUMN anchor_note TEXT"),
        ("anchor_at", "ALTER TABLE facts ADD COLUMN anchor_at TEXT"),
    ]:
        if col not in existing_fact_cols:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_anchor ON facts(anchor_type)")
    except sqlite3.OperationalError:
        pass

    existing_fp_cols = _columns(conn, "session_fingerprints")
    for col, ddl in [
        ("identity_vector", "ALTER TABLE session_fingerprints ADD COLUMN identity_vector BLOB"),
        ("identity_model", "ALTER TABLE session_fingerprints ADD COLUMN identity_model TEXT"),
    ]:
        if col not in existing_fp_cols:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass

    # v13 → v14: Nebula visualization columns (3D layout + cluster assignment)
    existing_fact_cols = _columns(conn, "facts")
    for col, ddl in [
        ("viz_x", "ALTER TABLE facts ADD COLUMN viz_x REAL"),
        ("viz_y", "ALTER TABLE facts ADD COLUMN viz_y REAL"),
        ("viz_z", "ALTER TABLE facts ADD COLUMN viz_z REAL"),
        ("cluster_id", "ALTER TABLE facts ADD COLUMN cluster_id INTEGER"),
    ]:
        if col not in existing_fact_cols:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_cluster ON facts(cluster_id)")
    except sqlite3.OperationalError:
        pass

    # v14 → v15: Nebula live-firing event stream
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nebula_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                fact_id TEXT,
                personality TEXT,
                related_ids TEXT DEFAULT '[]',
                intensity REAL DEFAULT 1.0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nebula_events_created "
            "ON nebula_events(created_at)"
        )
    except sqlite3.OperationalError:
        pass

    # v16 → v17: Phase 4 outreach — Atlas-initiated contact
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreach_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL,           -- session_gap | anniversary_window | unresolved_mistake | custom
                payload TEXT DEFAULT '{}',    -- JSON params for the kind
                enabled INTEGER DEFAULT 0,    -- default OFF — explicit opt-in
                cooldown_hours REAL DEFAULT 6,
                urgency REAL DEFAULT 0.5,
                last_fired_at TEXT,
                last_fired_detail TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outreaches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id INTEGER,
                personality TEXT,
                channel TEXT NOT NULL,
                subject TEXT,
                body TEXT NOT NULL,
                urgency REAL DEFAULT 0.5,
                delivered INTEGER DEFAULT 1,
                sent_at TEXT NOT NULL,
                acknowledged_at TEXT,
                FOREIGN KEY (trigger_id) REFERENCES outreach_triggers(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreaches_sent ON outreaches(sent_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreaches_trigger ON outreaches(trigger_id)"
        )
    except sqlite3.OperationalError:
        pass

    # v17 → v18: Phase 5.3 — Nebula mistake-points. Mistakes get their
    # own viz coords so red flashes land on real points in the galaxy.
    _add_column_if_missing(conn, "mistakes", "viz_x", "REAL")
    _add_column_if_missing(conn, "mistakes", "viz_y", "REAL")
    _add_column_if_missing(conn, "mistakes", "viz_z", "REAL")

    # v19 → v20: crystallization lineage. Long facts get split into atomic
    # children by Hypnos Stage 4.5; this lineage is preserved on both ends.
    #   crystallized_from = parent fact id (set on children)
    #   crystallized_into = JSON list of child fact ids (set on parents)
    _add_column_if_missing(conn, "facts", "crystallized_from", "TEXT")
    _add_column_if_missing(conn, "facts", "crystallized_into", "TEXT")

    # v20 → v21: doc_claims table — drift detector for worktree docs.
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                claim_text TEXT NOT NULL,
                claim_type TEXT NOT NULL,
                extracted_at TEXT NOT NULL,
                last_verified_at TEXT,
                last_seen_at TEXT NOT NULL,
                status TEXT DEFAULT 'unverified',
                refute_evidence TEXT,
                UNIQUE(source_path, claim_text)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_claims_status "
            "ON doc_claims(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_claims_source "
            "ON doc_claims(source_path)"
        )
    except sqlite3.OperationalError:
        pass

    # v21 → v22: soft-archive flag on mistakes/reflections (product
    # invariant: mistakes are never hard-deleted — fix_hygiene archives
    # instead), plus a one-off repair for self-superseded tombstones the
    # old learn() higher-authority dedup bug left behind (superseded_by
    # pointing at the row's own id, hiding it from every active query).
    _add_column_if_missing(conn, "mistakes", "archived", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "reflections", "archived", "INTEGER DEFAULT 0")
    try:
        conn.execute(
            "UPDATE facts SET superseded_by = NULL WHERE superseded_by = id"
        )
    except sqlite3.OperationalError:
        pass

    # v22 → v23: de-Pete the exemplars table — rename pete→user_text,
    # atlas→agent_text. Idempotent; shared helper with the legacy runner.
    from null_memory.db import _rename_exemplar_columns
    _rename_exemplar_columns(conn)

    # v23 → v24: relationship edges move from the related_to JSON column
    # to a real fact_edges table (P2-16). Idempotent backfill — re-runs
    # pick up JSON merged in from legacy DBs after this migration shipped.
    from null_memory.db import _create_and_backfill_fact_edges
    _create_and_backfill_fact_edges(conn)

    # Instance presence registry (no version bump — covered by the
    # structural heal above, but created explicitly here too so the table
    # exists even if a degraded store made the heal's fail-soft SCHEMA_SQL
    # replay skip it).
    try:
        conn.execute("""
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
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_instances_heartbeat "
            "ON instances(last_heartbeat)"
        )
    except sqlite3.OperationalError:
        pass

    # v18 → v19: on-boot identity verification table. Each MCP boot logs
    # its coherence_score against the historical identity-vector centroid.
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                personality TEXT NOT NULL,
                boot_time TEXT NOT NULL,
                coherence_score REAL,
                verified INTEGER DEFAULT 0,
                sample_size INTEGER DEFAULT 0,
                identity_payload_hash TEXT,
                identity_model TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_verifications_session "
            "ON session_verifications(session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_verifications_personality "
            "ON session_verifications(personality)"
        )
    except sqlite3.OperationalError:
        pass

    # Stamp the current schema version so meta reflects the applied
    # upgrades (doc_audit compares this against UNIFIED_SCHEMA_VERSION;
    # without the stamp it reports a permanent version mismatch).
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(UNIFIED_SCHEMA_VERSION),),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _add_column_if_missing(conn: sqlite3.Connection, table: str,
                            column: str, type_: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
    except sqlite3.OperationalError:
        pass


def _backup_unified_db(target_path: str) -> str:
    """Snapshot an existing unified DB before a --force rebuild destroys it.

    Uses sqlite3's online backup API for a consistent copy (WAL contents
    are folded in, so the sidecar -wal/-shm files don't need copying).
    Falls back to shutil.copy2 of the db + sidecars if the backup API
    fails (e.g. a corrupt database). Returns the backup path.
    """
    import shutil

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = f"{target_path}.bak.{ts}"
    try:
        src = sqlite3.connect(target_path)
        try:
            dst = sqlite3.connect(backup_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error:
        # Corrupt or locked DB — fall back to raw file copies so the
        # bytes survive even if sqlite can't read them.
        shutil.copy2(target_path, backup_path)
        for ext in ("-wal", "-shm"):
            p = target_path + ext
            if os.path.exists(p):
                shutil.copy2(p, backup_path + ext)
    return backup_path


def is_migration_complete(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='migration_complete'").fetchone()
    return bool(row) and row[0] == "1"


def _log(conn: sqlite3.Connection, event: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO migration_log(ts, event, detail) VALUES (?, ?, ?)",
        (_now(), event, detail),
    )


def _migrate_facts(
    src: sqlite3.Connection, dst: sqlite3.Connection, personality: str, stats: MigrationStats
) -> None:
    """Copy facts deduping by id; build personality_views overlay."""
    src_cols = _columns(src, "facts")
    has_tier = "tier" in src_cols
    has_related = "related_to" in src_cols

    rows = src.execute("SELECT * FROM facts").fetchall()
    for row in rows:
        fid = row["id"]
        existing = dst.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()

        if existing is None:
            dst.execute(
                """
                INSERT INTO facts (
                    id, fact, confidence, base_confidence, project, source, provenance,
                    impact, session_id, created_at, last_accessed, access_count,
                    last_verified, verified_by, superseded_by, forgotten, archived,
                    related_to, tier
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fid,
                    row["fact"],
                    row["confidence"],
                    row["base_confidence"],
                    row["project"],
                    row["source"],
                    row["provenance"],
                    row["impact"],
                    row["session_id"],
                    row["created_at"],
                    row["last_accessed"],
                    row["access_count"] or 0,
                    row["last_verified"],
                    row["verified_by"],
                    row["superseded_by"],
                    row["forgotten"] or 0,
                    row["archived"] or 0,
                    row["related_to"] if has_related else "[]",
                    row["tier"] if has_tier else "contextual",
                ),
            )
            stats.facts_inserted += 1
        else:
            # Merge metadata
            new_access = max(existing["access_count"] or 0, row["access_count"] or 0)
            new_conf = max(existing["confidence"] or 0.0, row["confidence"] or 0.0)
            new_created = min(existing["created_at"], row["created_at"])
            new_last_acc = max(
                existing["last_accessed"] or "", row["last_accessed"] or ""
            ) or None
            merged_related = _merge_related_to(
                existing["related_to"], row["related_to"] if has_related else None
            )
            new_tier = existing["tier"] or (row["tier"] if has_tier else None) or "contextual"
            dst.execute(
                """
                UPDATE facts
                SET access_count=?, confidence=?, created_at=?, last_accessed=?,
                    related_to=?, tier=?
                WHERE id=?
                """,
                (new_access, new_conf, new_created, new_last_acc, merged_related, new_tier, fid),
            )
            stats.facts_merged += 1

        # Per-personality overlay
        dst.execute(
            """
            INSERT OR REPLACE INTO personality_views (
                fact_id, personality, last_accessed, access_count, tags
            ) VALUES (?, ?, ?, ?, '[]')
            """,
            (fid, personality, row["last_accessed"], row["access_count"] or 0),
        )
        stats.views_inserted += 1


def _migrate_attributed(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    personality: str,
    stats: MigrationStats,
    decision_id_map: dict[tuple[str, int], int],
) -> None:
    """Copy attributed tables, threading personality through. Also remembers
    decision id remapping so decision_outcomes can be repointed."""
    # decisions
    if _table_exists(src, "decisions"):
        for row in src.execute("SELECT * FROM decisions").fetchall():
            cur = dst.execute(
                """
                INSERT INTO decisions (
                    decision, reasoning, project, personality, session_id, trace,
                    created_at, src_id
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    row["decision"],
                    row["reasoning"],
                    row["project"],
                    personality,
                    row["session_id"],
                    row["trace"] if "trace" in row.keys() else "[]",
                    row["created_at"],
                    row["id"],
                ),
            )
            decision_id_map[(personality, row["id"])] = cur.lastrowid
            stats.decisions += 1

    if _table_exists(src, "mistakes"):
        for row in src.execute("SELECT * FROM mistakes").fetchall():
            dst.execute(
                """
                INSERT INTO mistakes (
                    mistake, why, project, personality, confidence, session_id, created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    row["mistake"],
                    row["why"],
                    row["project"],
                    personality,
                    row["confidence"],
                    row["session_id"],
                    row["created_at"],
                ),
            )
            stats.mistakes += 1

    if _table_exists(src, "reflections"):
        for row in src.execute("SELECT * FROM reflections").fetchall():
            dst.execute(
                """
                INSERT INTO reflections (
                    went_well, missed, do_differently, project, personality,
                    session_id, created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    row["went_well"],
                    row["missed"],
                    row["do_differently"],
                    row["project"],
                    personality,
                    row["session_id"],
                    row["created_at"],
                ),
            )
            stats.reflections += 1

    if _table_exists(src, "exemplars"):
        for row in src.execute("SELECT * FROM exemplars").fetchall():
            # Source DBs may pre- or post-date the v13 pete/atlas →
            # user_text/agent_text rename; accept either column set.
            d = dict(row)
            dst.execute(
                """
                INSERT INTO exemplars (
                    scenario, user_text, agent_text, calibration, tags,
                    personality, created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    d["scenario"],
                    d.get("user_text", d.get("pete")),
                    d.get("agent_text", d.get("atlas")),
                    d["calibration"],
                    d["tags"],
                    personality,
                    d["created_at"],
                ),
            )
            stats.exemplars += 1

    if _table_exists(src, "session_fingerprints"):
        for row in src.execute("SELECT * FROM session_fingerprints").fetchall():
            cols = row.keys()
            dst.execute(
                """
                INSERT OR REPLACE INTO session_fingerprints (
                    session_id, personality, project, duration_minutes, facts_count,
                    decisions_count, mistakes_count, tier_dist, topic_vector, outcome,
                    tags, energy_arc, highlights, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["session_id"],
                    personality,
                    row["project"] if "project" in cols else None,
                    row["duration_minutes"] if "duration_minutes" in cols else None,
                    row["facts_count"] if "facts_count" in cols else 0,
                    row["decisions_count"] if "decisions_count" in cols else 0,
                    row["mistakes_count"] if "mistakes_count" in cols else 0,
                    row["tier_dist"] if "tier_dist" in cols else "{}",
                    row["topic_vector"] if "topic_vector" in cols else None,
                    row["outcome"] if "outcome" in cols else "neutral",
                    row["tags"] if "tags" in cols else "[]",
                    row["energy_arc"] if "energy_arc" in cols else "",
                    row["highlights"] if "highlights" in cols else "[]",
                    row["created_at"],
                ),
            )
            stats.fingerprints += 1

    if _table_exists(src, "probes"):
        for row in src.execute("SELECT * FROM probes").fetchall():
            dst.execute(
                """
                INSERT INTO probes (
                    question, expected, fact_id, probe_type, personality,
                    created_at, last_run, last_result, run_count, pass_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["question"],
                    row["expected"],
                    row["fact_id"],
                    row["probe_type"],
                    personality,
                    row["created_at"],
                    row["last_run"],
                    row["last_result"],
                    row["run_count"] or 0,
                    row["pass_count"] or 0,
                ),
            )
            stats.probes += 1

    if _table_exists(src, "decision_feed"):
        for row in src.execute("SELECT * FROM decision_feed").fetchall():
            dst.execute(
                """
                INSERT INTO decision_feed (
                    session_id, personality, decision, reasoning, project, status, created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    row["session_id"],
                    personality,
                    row["decision"],
                    row["reasoning"],
                    row["project"],
                    row["status"],
                    row["created_at"],
                ),
            )
            stats.decision_feed += 1

    if _table_exists(src, "decision_outcomes"):
        for row in src.execute("SELECT * FROM decision_outcomes").fetchall():
            new_did = decision_id_map.get((personality, row["decision_id"]))
            if new_did is None:
                stats.warnings.append(
                    f"decision_outcome with no parent decision: personality={personality} src_decision_id={row['decision_id']}"
                )
                continue
            dst.execute(
                """
                INSERT INTO decision_outcomes (
                    decision_id, outcome, success, recorded_at
                ) VALUES (?,?,?,?)
                """,
                (new_did, row["outcome"], row["success"], row["recorded_at"]),
            )
            stats.decision_outcomes += 1

    if _table_exists(src, "hypnos_journal"):
        for row in src.execute("SELECT * FROM hypnos_journal").fetchall():
            dst.execute(
                """
                INSERT INTO hypnos_journal (
                    personality, run_id, started_at, stage, action, fact_id, detail
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    personality,
                    row["run_id"],
                    row["started_at"],
                    row["stage"],
                    row["action"],
                    row["fact_id"],
                    row["detail"],
                ),
            )
            stats.hypnos += 1

    if _table_exists(src, "evaluations"):
        for row in src.execute("SELECT * FROM evaluations").fetchall():
            dst.execute(
                """
                INSERT INTO evaluations (personality, run_at, score, metrics, notes)
                VALUES (?,?,?,?,?)
                """,
                (
                    personality,
                    row["run_at"],
                    row["score"],
                    row["metrics"],
                    row["notes"] or "",
                ),
            )
            stats.evaluations += 1


def _migrate_embeddings(
    src: sqlite3.Connection, dst: sqlite3.Connection, stats: MigrationStats
) -> None:
    if not _table_exists(src, "fact_embeddings"):
        return
    valid_fact_ids = {
        row[0] for row in dst.execute("SELECT id FROM facts").fetchall()
    }
    skipped = 0
    for row in src.execute("SELECT * FROM fact_embeddings").fetchall():
        if row["fact_id"] not in valid_fact_ids:
            # Orphan embedding (its fact was deleted upstream). Drop it.
            skipped += 1
            continue
        dst.execute(
            """
            INSERT OR IGNORE INTO fact_embeddings (fact_id, embedding, model, created_at)
            VALUES (?,?,?,?)
            """,
            (row["fact_id"], row["embedding"], row["model"], row["created_at"]),
        )
    if skipped:
        stats.warnings.append(f"dropped {skipped} orphan embeddings (no matching fact)")
    stats.embeddings_copied = dst.execute(
        "SELECT COUNT(*) FROM fact_embeddings"
    ).fetchone()[0]


def _migrate_multiverse(dst: sqlite3.Connection, stats: MigrationStats) -> None:
    src = _open_source(MULTIVERSE_DB)
    if src is None:
        stats.warnings.append("multiverse.db not found")
        return
    try:
        if _table_exists(src, "personalities"):
            for row in src.execute("SELECT * FROM personalities").fetchall():
                dst.execute(
                    """
                    INSERT OR REPLACE INTO personalities (
                        name, role, active, created_at, bootstrapped_from, description, focus
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        row["name"],
                        row["role"],
                        row["active"],
                        row["created_at"],
                        row["bootstrapped_from"],
                        row["description"],
                        row["focus"],
                    ),
                )
                stats.personalities += 1
        if _table_exists(src, "broadcasts"):
            for row in src.execute("SELECT * FROM broadcasts").fetchall():
                dst.execute(
                    """
                    INSERT INTO broadcasts (event, source, targets, created_at)
                    VALUES (?,?,?,?)
                    """,
                    (row["event"], row["source"], row["targets"], row["created_at"]),
                )
                stats.broadcasts += 1
        if _table_exists(src, "dreams"):
            for row in src.execute("SELECT * FROM dreams").fetchall():
                dst.execute(
                    """
                    INSERT INTO dreams (hypothesis, source_facts, confidence, status, created_at)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        row["hypothesis"],
                        row["source_facts"],
                        row["confidence"],
                        row["status"],
                        row["created_at"],
                    ),
                )
                stats.dreams += 1
        if _table_exists(src, "xrefs"):
            xref_id_map: dict[int, int] = {}
            for row in src.execute("SELECT * FROM xrefs").fetchall():
                cur = dst.execute(
                    "INSERT INTO xrefs (event, created_at) VALUES (?,?)",
                    (row["event"], row["created_at"]),
                )
                xref_id_map[row["id"]] = cur.lastrowid
                stats.xrefs += 1
            if _table_exists(src, "xref_facts"):
                for row in src.execute("SELECT * FROM xref_facts").fetchall():
                    new_id = xref_id_map.get(row["xref_id"])
                    if new_id is None:
                        continue
                    dst.execute(
                        """
                        INSERT OR IGNORE INTO xref_facts (xref_id, personality, fact_id)
                        VALUES (?,?,?)
                        """,
                        (new_id, row["personality"], row["fact_id"]),
                    )
    finally:
        src.close()


def _backfill_embeddings(dst: sqlite3.Connection, stats: MigrationStats) -> None:
    """Embed any fact lacking a row in fact_embeddings."""
    try:
        from null_memory.embeddings import EmbeddingEngine
    except Exception as e:
        stats.warnings.append(f"embeddings module unavailable: {e}")
        return

    engine = EmbeddingEngine(dst)
    if not engine.available:
        stats.warnings.append("fastembed not installed; backfill skipped")
        return

    rows = dst.execute(
        """
        SELECT f.id, f.fact FROM facts f
        LEFT JOIN fact_embeddings e ON e.fact_id = f.id
        WHERE e.fact_id IS NULL AND f.archived = 0 AND f.forgotten = 0
        """
    ).fetchall()
    if not rows:
        return

    fact_dicts = [{"id": r["id"], "fact": r["fact"]} for r in rows]
    n = engine.embed_all_facts(fact_dicts)
    stats.embeddings_backfilled = n


# ── Public entry point ─────────────────────────────────────────────────────


def migrate(
    target_path: str = DEFAULT_UNIFIED_PATH,
    *,
    force: bool = False,
    sources: list[tuple[str, str]] | None = None,
    orphan_root_db: str | None = ORPHAN_ROOT_DB,
    multiverse_db: str | None = MULTIVERSE_DB,  # noqa: ARG001
    backfill: bool = True,
) -> MigrationStats:
    """Run the v3 unified-substrate migration.

    Idempotent: pass ``force=True`` to rebuild from scratch.
    Returns MigrationStats for verification.
    """
    if force and os.path.exists(target_path):
        # Never destroy post-migration memory without a recovery path —
        # snapshot the existing DB first.
        backup_path = _backup_unified_db(target_path)
        logger.info("force rebuild: backed up %s to %s", target_path, backup_path)
        print(f"[migrate_v3] existing unified DB backed up to {backup_path}")
        os.remove(target_path)
        for ext in ("-shm", "-wal"):
            p = target_path + ext
            if os.path.exists(p):
                os.remove(p)

    dst = init_unified_db(target_path)
    if is_migration_complete(dst) and not force:
        raise RuntimeError(
            f"Unified DB at {target_path} already has migration_complete=1; "
            "pass force=True to rebuild."
        )

    stats = MigrationStats()
    decision_id_map: dict[tuple[str, int], int] = {}

    src_list = sources if sources is not None else PERSONALITY_SOURCES

    try:
        with dst:
            _log(dst, "start", target_path)
            atlas_src: sqlite3.Connection | None = None
            for personality, db_path in src_list:
                src = _open_source(db_path)
                if src is None:
                    stats.skipped_personalities.append(personality)
                    _log(dst, "skip_personality", f"{personality}: missing {db_path}")
                    continue
                try:
                    _migrate_facts(src, dst, personality, stats)
                    _migrate_attributed(src, dst, personality, stats, decision_id_map)
                    if personality == "atlas":
                        # Only Atlas has embeddings today
                        _migrate_embeddings(src, dst, stats)
                        atlas_src = src  # keep open until orphan handled
                    _log(dst, "personality_done", personality)
                finally:
                    if personality != "atlas":
                        src.close()

            # Fold orphan root DB into Atlas attribution
            if orphan_root_db and os.path.exists(orphan_root_db):
                orphan = _open_source(orphan_root_db)
                if orphan is not None:
                    try:
                        _migrate_facts(orphan, dst, ORPHAN_ATTRIBUTED_TO, stats)
                        _log(dst, "orphan_done", orphan_root_db)
                    finally:
                        orphan.close()

            if atlas_src is not None:
                atlas_src.close()

            _migrate_multiverse(dst, stats)
            _log(dst, "multiverse_done", "")

            if backfill:
                _backfill_embeddings(dst, stats)
                _log(dst, "embeddings_backfilled", str(stats.embeddings_backfilled))

            # Rebuild FTS indexes from facts (idempotent)
            dst.execute("INSERT INTO facts_fts(facts_fts) VALUES('rebuild')")
            dst.execute("INSERT INTO facts_trigram(facts_trigram) VALUES('rebuild')")

            dst.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('migration_complete', '1')"
            )
            dst.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('migration_completed_at', ?)",
                (_now(),),
            )
            _log(dst, "complete", json.dumps(stats.__dict__, default=str))
    finally:
        dst.close()

    return stats


# ── Verification ───────────────────────────────────────────────────────────


def sync_multiverse(
    target_path: str = DEFAULT_UNIFIED_PATH,
    multiverse_db: str = MULTIVERSE_DB,
) -> dict[str, int]:
    """One-shot sync of ``multiverse.db`` into the unified DB.

    Phase 1c.1-lite: MultiverseManager still owns multiverse.db. This copies
    any personality/broadcast/dream/xref changes into unified.db so the
    unified tables stay current until the full cutover. Safe to run while
    MultiverseManager is active — uses INSERT OR REPLACE semantics.
    """
    src = _open_source(multiverse_db)
    if src is None:
        return {"error": "multiverse.db not found", "personalities": 0,
                "broadcasts": 0, "dreams": 0}
    dst = sqlite3.connect(target_path)
    dst.row_factory = sqlite3.Row
    stats = {"personalities": 0, "broadcasts": 0, "dreams": 0, "xrefs": 0}
    try:
        with dst:
            if _table_exists(src, "personalities"):
                for row in src.execute("SELECT * FROM personalities").fetchall():
                    dst.execute(
                        """INSERT OR REPLACE INTO personalities (
                            name, role, active, created_at, bootstrapped_from,
                            description, focus
                        ) VALUES (?,?,?,?,?,?,?)""",
                        (row["name"], row["role"], row["active"],
                         row["created_at"], row["bootstrapped_from"],
                         row["description"], row["focus"]),
                    )
                    stats["personalities"] += 1
            # Broadcasts, dreams, xrefs: append-only in practice; avoid dupes
            # by tracking created_at + event as a soft key.
            existing_bc = {
                (r[0], r[1], r[2]) for r in dst.execute(
                    "SELECT event, source, created_at FROM broadcasts"
                ).fetchall()
            }
            if _table_exists(src, "broadcasts"):
                for row in src.execute("SELECT * FROM broadcasts").fetchall():
                    k = (row["event"], row["source"], row["created_at"])
                    if k in existing_bc:
                        continue
                    dst.execute(
                        "INSERT INTO broadcasts (event, source, targets, created_at) "
                        "VALUES (?,?,?,?)",
                        (row["event"], row["source"], row["targets"],
                         row["created_at"]),
                    )
                    stats["broadcasts"] += 1
            existing_dr = {
                (r[0], r[1]) for r in dst.execute(
                    "SELECT hypothesis, created_at FROM dreams"
                ).fetchall()
            }
            if _table_exists(src, "dreams"):
                for row in src.execute("SELECT * FROM dreams").fetchall():
                    k = (row["hypothesis"], row["created_at"])
                    if k in existing_dr:
                        continue
                    dst.execute(
                        "INSERT INTO dreams (hypothesis, source_facts, confidence, status, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (row["hypothesis"], row["source_facts"],
                         row["confidence"], row["status"], row["created_at"]),
                    )
                    stats["dreams"] += 1
    finally:
        src.close()
        dst.close()
    return stats


def verify(target_path: str = DEFAULT_UNIFIED_PATH) -> dict[str, Any]:
    """Return a dict of verification metrics for the unified DB."""
    dst = sqlite3.connect(target_path)
    dst.row_factory = sqlite3.Row
    try:
        out: dict[str, Any] = {}
        out["schema_version"] = dst.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        out["migration_complete"] = dst.execute(
            "SELECT value FROM meta WHERE key='migration_complete'"
        ).fetchone()[0]
        out["facts"] = dst.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        out["facts_active"] = dst.execute(
            "SELECT COUNT(*) FROM facts WHERE archived=0 AND forgotten=0"
        ).fetchone()[0]
        out["embeddings"] = dst.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0]
        out["personality_views"] = dst.execute(
            "SELECT COUNT(*) FROM personality_views"
        ).fetchone()[0]
        out["per_personality"] = {
            row["personality"]: row["c"]
            for row in dst.execute(
                "SELECT personality, COUNT(*) AS c FROM personality_views GROUP BY personality"
            )
        }
        for tbl in (
            "decisions",
            "mistakes",
            "reflections",
            "exemplars",
            "session_fingerprints",
            "probes",
            "decision_feed",
            "decision_outcomes",
            "hypnos_journal",
            "evaluations",
            "personalities",
            "broadcasts",
            "dreams",
            "xrefs",
            "xref_facts",
        ):
            out[tbl] = dst.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        # Embedding coverage
        out["facts_without_embedding"] = dst.execute(
            """
            SELECT COUNT(*) FROM facts f
            LEFT JOIN fact_embeddings e ON e.fact_id = f.id
            WHERE e.fact_id IS NULL AND f.archived=0 AND f.forgotten=0
            """
        ).fetchone()[0]
        # FTS sync check
        out["fts_count"] = dst.execute("SELECT COUNT(*) FROM facts_fts").fetchone()[0]
        return out
    finally:
        dst.close()
