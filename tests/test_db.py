"""Tests for SQLite storage backend (db.py)."""

import json
import os
import tempfile

import pytest

from null_memory.db import NullDB, migrate_jsonl_to_sqlite


@pytest.fixture
def db(tmp_path):
    """Create a fresh NullDB instance with schema initialized."""
    d = NullDB(str(tmp_path))
    d.initialize()
    return d


@pytest.fixture
def sample_fact():
    return {
        "id": "abc123def456",
        "fact": "Aleph uses tree-sitter for AST parsing across 6 languages",
        "confidence": 0.9,
        "base_confidence": 0.9,
        "project": "aleph",
        "source": "observation",
        "provenance": "observation",
        "impact": 0.85,
        "session_id": "sess-001",
        "created_at": "2026-03-23T10:00:00+00:00",
    }


class TestSchema:
    def test_initialize_creates_tables(self, db):
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in tables}
        assert "facts" in names
        assert "decisions" in names
        assert "mistakes" in names
        assert "reflections" in names
        assert "exemplars" in names
        assert "meta" in names

    def test_initialize_creates_fts_tables(self, db):
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in tables}
        assert "facts_fts" in names
        assert "facts_trigram" in names

    def test_initialize_idempotent(self, db):
        db.initialize()
        db.initialize()
        count = db.count_facts()
        assert count == 0

    def test_schema_version_set(self, db):
        row = db.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == "14"

    def test_wal_mode(self, db):
        row = db.conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"


class TestFactCRUD:
    def test_insert_and_get(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        result = db.get_fact_by_id("abc123def456")
        assert result is not None
        assert result["fact"] == sample_fact["fact"]
        assert result["confidence"] == 0.9
        assert result["project"] == "aleph"

    def test_insert_normalizes_project(self, db, sample_fact):
        sample_fact["project"] = "  Aleph  "
        db.insert_fact(sample_fact)
        db.conn.commit()
        result = db.get_fact_by_id("abc123def456")
        assert result["project"] == "aleph"

    def test_insert_duplicate_ignored(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.insert_fact(sample_fact)  # Same id — should be ignored
        db.conn.commit()
        assert db.count_facts() == 1

    def test_get_active_facts(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.insert_fact({
            **sample_fact,
            "id": "forgotten1",
            "fact": "forgotten fact",
            "forgotten": True,
        })
        db.insert_fact({
            **sample_fact,
            "id": "archived1",
            "fact": "archived fact",
            "archived": True,
        })
        db.conn.commit()
        active = db.get_active_facts()
        assert len(active) == 1
        assert active[0]["id"] == "abc123def456"

    def test_forget_fact(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        assert db.forget_fact("abc123def456") is True
        assert db.count_facts() == 0  # Active count
        assert db.count_facts(active_only=False) == 1  # Still in DB

    def test_forget_nonexistent(self, db):
        assert db.forget_fact("doesnotexist") is False

    def test_archive_fact(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        db.archive_fact("abc123def456")
        db.conn.commit()
        assert db.count_facts() == 0
        assert db.count_facts(active_only=False) == 1

    def test_supersede_fact(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        db.supersede_fact("abc123def456", "new_hash_789")
        db.conn.commit()
        result = db.get_fact_by_id("abc123def456")
        assert result["superseded_by"] == "new_hash_789"

    def test_repair_self_superseded(self, db, sample_fact):
        """A self-superseded tombstone (old learn() dedup bug) must be
        resurrected, while legitimate supersessions stay untouched."""
        db.insert_fact(sample_fact)
        db.insert_fact(dict(sample_fact, id="legit_superseded",
                            fact="Old fact replaced by a newer one"))
        db.conn.commit()
        db.supersede_fact("abc123def456", "abc123def456")  # artificial tombstone
        db.supersede_fact("legit_superseded", "some_other_hash")
        db.conn.commit()
        assert db.count_facts() == 0  # tombstone hides the fact

        healed = db.repair_self_superseded()
        assert healed == 1
        assert db.get_fact_by_id("abc123def456")["superseded_by"] is None
        assert db.count_facts() == 1
        # Legit supersession untouched
        assert db.get_fact_by_id("legit_superseded")["superseded_by"] == "some_other_hash"
        # Idempotent
        assert db.repair_self_superseded() == 0

    def test_v12_migration_heals_self_superseded(self, tmp_path, sample_fact):
        """Existing DBs with self-superseded tombstones get healed when the
        schema migration runner brings them to v12."""
        d = NullDB(str(tmp_path))
        d.initialize()
        d.insert_fact(sample_fact)
        d.conn.execute("UPDATE facts SET superseded_by = id")
        # Rewind to pre-v12 so initialize() runs the migration
        d.conn.execute("UPDATE meta SET value = '11' WHERE key = 'schema_version'")
        d.conn.commit()
        d.close()

        d2 = NullDB(str(tmp_path))
        d2.initialize()
        assert d2.get_fact_by_id(sample_fact["id"])["superseded_by"] is None
        assert d2.count_facts() == 1
        d2.close()

    def test_verify_fact(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        db.verify_fact("abc123def456", "sess-002")
        db.conn.commit()
        result = db.get_fact_by_id("abc123def456")
        assert result["last_verified"] is not None
        assert result["verified_by"] == "sess-002"

    def test_update_access(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        db.update_fact_access("abc123def456")
        db.update_fact_access("abc123def456")
        db.conn.commit()
        result = db.get_fact_by_id("abc123def456")
        assert result["access_count"] == 2
        assert result["last_accessed"] is not None

    def test_find_fact_by_text(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        result = db.find_fact_by_text("tree-sitter")
        assert result is not None
        assert "tree-sitter" in result["fact"]

    def test_find_fact_by_text_not_found(self, db, sample_fact):
        db.insert_fact(sample_fact)
        db.conn.commit()
        result = db.find_fact_by_text("nonexistent query")
        assert result is None


class TestFTSSearch:
    @pytest.fixture(autouse=True)
    def populate(self, db):
        facts = [
            ("hash1", "Aleph uses tree-sitter for AST parsing across 6 languages", "aleph", 0.9),
            ("hash2", "PostgreSQL is the primary database for the website", "global", 0.8),
            ("hash3", "Redis caching improves API response time significantly", "global", 0.7),
            ("hash4", "We decided on a monorepo structure for the project", "aleph", 0.85),
            ("hash5", "The Rust accelerator branch has Phase 1 scaffold", "aleph", 0.75),
        ]
        for fid, fact, project, conf in facts:
            db.insert_fact({
                "id": fid,
                "fact": fact,
                "confidence": conf,
                "base_confidence": conf,
                "project": project,
                "created_at": "2026-03-23T10:00:00+00:00",
            })
        db.conn.commit()
        self.db = db

    def test_keyword_search(self):
        results = self.db.fts_search("database")
        assert len(results) >= 1
        assert any("database" in r["fact"].lower() for r in results)

    def test_keyword_search_multiple_terms(self):
        results = self.db.fts_search("tree-sitter parsing")
        assert len(results) >= 1
        assert any("tree-sitter" in r["fact"] for r in results)

    def test_search_with_project_filter(self):
        results = self.db.fts_search("parsing", project="aleph")
        assert len(results) >= 1
        for r in results:
            assert r["project"] in ("aleph", "global")

    def test_search_no_results(self):
        results = self.db.fts_search("quantum entanglement")
        assert len(results) == 0

    def test_bm25_rank_present(self):
        results = self.db.fts_search("database")
        assert len(results) > 0
        assert "bm25_rank" in results[0]

    def test_search_excludes_forgotten(self):
        self.db.forget_fact("hash2")
        results = self.db.fts_search("database")
        assert not any(r["id"] == "hash2" for r in results)

    def test_search_excludes_superseded(self):
        self.db.supersede_fact("hash1", "new_hash")
        self.db.conn.commit()
        results = self.db.fts_search("tree-sitter")
        assert not any(r["id"] == "hash1" for r in results)

    def test_search_with_since(self):
        results = self.db.fts_search("database", since="2026-03-22T00:00:00+00:00")
        assert len(results) >= 1
        results = self.db.fts_search("database", since="2027-01-01T00:00:00+00:00")
        assert len(results) == 0


class TestTrigramSearch:
    @pytest.fixture(autouse=True)
    def populate(self, db):
        facts = [
            ("hash1", "The architecture of the monorepo is well-defined"),
            ("hash2", "PostgreSQL database setup requires migration scripts"),
            ("hash3", "Redis caching layer handles session persistence"),
        ]
        for fid, fact in facts:
            db.insert_fact({
                "id": fid,
                "fact": fact,
                "confidence": 0.8,
                "base_confidence": 0.8,
                "project": "global",
                "created_at": "2026-03-23T10:00:00+00:00",
            })
        db.conn.commit()
        self.db = db

    def test_trigram_substring(self):
        results = self.db.trigram_search("archit")
        assert len(results) >= 1
        assert any("architecture" in r["fact"].lower() for r in results)

    def test_trigram_typo_tolerance(self):
        results = self.db.trigram_search("databas")
        assert len(results) >= 1
        assert any("database" in r["fact"].lower() for r in results)

    def test_trigram_excludes_ids(self):
        results = self.db.trigram_search("archit", exclude_ids={"hash1"})
        assert not any(r["id"] == "hash1" for r in results)

    def test_trigram_with_project(self):
        results = self.db.trigram_search("archit", project="global")
        assert len(results) >= 1


class TestDecisionsMistakesReflections:
    def test_insert_and_get_decisions(self, db):
        db.insert_decision({
            "decision": "Use SQLite for storage",
            "reasoning": "Zero dependencies, FTS5 built-in",
            "project": "null",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        decisions = db.get_decisions()
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "Use SQLite for storage"

    def test_insert_and_get_mistakes(self, db):
        db.insert_mistake({
            "mistake": "Forgot to call null_observe",
            "why": "MCP server wasn't connected",
            "project": "global",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        mistakes = db.get_mistakes()
        assert len(mistakes) == 1
        assert "null_observe" in mistakes[0]["mistake"]

    def test_insert_and_get_reflections(self, db):
        db.insert_reflection({
            "went_well": "Shipped v0.5.0",
            "missed": "Should have tested on Windows",
            "do_differently": "Test cross-platform earlier",
            "project": "null",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        reflections = db.get_reflections()
        assert len(reflections) == 1
        assert reflections[0]["went_well"] == "Shipped v0.5.0"

    def test_decision_normalizes_project(self, db):
        db.insert_decision({
            "decision": "test",
            "project": "  NULL  ",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        assert db.get_decisions()[0]["project"] == "null"

    def test_mistake_search(self, db):
        db.insert_mistake({
            "mistake": "Forgot to call null_observe",
            "why": "Discipline issue",
            "project": "global",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.insert_mistake({
            "mistake": "Wrong parser for Java files",
            "why": "Generic fallback was incorrect",
            "project": "aleph",
            "created_at": "2026-03-23T11:00:00+00:00",
        })
        db.conn.commit()
        results = db.search_mistakes("observe")
        assert len(results) == 1
        assert "observe" in results[0]["mistake"]


class TestExemplars:
    def test_insert_and_get(self, db):
        db.insert_exemplar({
            "scenario": "identity_check",
            "user_text": "who are you?",
            "agent_text": "I'm your persistent AI collaborator.",
            "calibration": "Respond with confidence, no hedging.",
            "tags": ["identity", "verification"],
        })
        db.conn.commit()
        exemplars = db.get_exemplars()
        assert len(exemplars) == 1
        assert exemplars[0]["user_text"] == "who are you?"
        assert exemplars[0]["tags"] == ["identity", "verification"]

    def test_legacy_migration_renames_pete_atlas_columns(self, tmp_path):
        """v12→v13 renames exemplars.pete→user_text, atlas→agent_text and
        preserves existing row data."""
        import sqlite3
        from null_memory.db import NullDB

        agent_dir = str(tmp_path / "legacy")
        os.makedirs(agent_dir)
        db_path = os.path.join(agent_dir, "memory.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '12');
            CREATE TABLE exemplars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario TEXT,
                pete TEXT NOT NULL,
                atlas TEXT,
                calibration TEXT,
                tags TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO exemplars (scenario, pete, atlas, calibration, tags, created_at)
            VALUES ('s', 'old user words', 'old agent words', 'cal', '[]',
                    '2026-01-01T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()

        db = NullDB(agent_dir)
        db.initialize()
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(exemplars)").fetchall()}
        assert "user_text" in cols and "agent_text" in cols
        assert "pete" not in cols and "atlas" not in cols
        exemplars = db.get_exemplars()
        assert exemplars[0]["user_text"] == "old user words"
        assert exemplars[0]["agent_text"] == "old agent words"
        # Round-trip with new helper API
        db.insert_exemplar({"user_text": "new q", "agent_text": "new a",
                            "created_at": "2026-01-02T00:00:00+00:00"})
        db.conn.commit()
        assert len(db.get_exemplars()) == 2
        db.close()

    def test_unified_upgrade_renames_pete_atlas_columns(self, tmp_path):
        """Unified v22→v23 upgrade renames exemplar columns, preserves data,
        and is idempotent."""
        import sqlite3
        from null_memory.migrate_v3 import _apply_unified_upgrades

        path = str(tmp_path / "unified.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE facts (id TEXT PRIMARY KEY, fact TEXT,
                superseded_by TEXT);
            CREATE TABLE mistakes (id INTEGER PRIMARY KEY, mistake TEXT);
            CREATE TABLE reflections (id INTEGER PRIMARY KEY, went_well TEXT);
            CREATE TABLE session_fingerprints (session_id TEXT PRIMARY KEY);
            CREATE TABLE exemplars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario TEXT,
                pete TEXT NOT NULL,
                atlas TEXT,
                calibration TEXT,
                tags TEXT,
                personality TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO exemplars (scenario, pete, atlas, calibration, tags,
                                   personality, created_at)
            VALUES ('s', 'unified user words', 'unified agent words', 'cal',
                    '[]', 'atlas', '2026-01-01T00:00:00+00:00');
        """)
        conn.commit()
        _apply_unified_upgrades(conn)
        _apply_unified_upgrades(conn)  # idempotent
        cols = {r[1] for r in conn.execute("PRAGMA table_info(exemplars)").fetchall()}
        assert "user_text" in cols and "agent_text" in cols
        assert "pete" not in cols and "atlas" not in cols
        row = conn.execute(
            "SELECT user_text, agent_text FROM exemplars"
        ).fetchone()
        assert row == ("unified user words", "unified agent words")
        conn.close()

    def test_insert_accepts_legacy_pete_atlas_keys(self, db):
        """Old JSONL exports use pete/atlas keys; still accepted."""
        db.insert_exemplar({
            "scenario": "legacy",
            "pete": "legacy user text",
            "atlas": "legacy agent text",
            "calibration": "works",
        })
        db.conn.commit()
        exemplars = db.get_exemplars()
        assert exemplars[0]["user_text"] == "legacy user text"
        assert exemplars[0]["agent_text"] == "legacy agent text"

    def test_tags_stored_as_json(self, db):
        db.insert_exemplar({
            "user_text": "push it",
            "tags": ["execution", "immediate"],
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        # Verify raw storage is JSON string
        row = db.conn.execute("SELECT tags FROM exemplars LIMIT 1").fetchone()
        assert json.loads(row[0]) == ["execution", "immediate"]


class TestDiagnostics:
    def test_diagnose_empty(self, db):
        findings = db.diagnose()
        assert findings["total_facts"] == 0
        assert findings["active_facts"] == 0
        assert findings["test_mistakes"] == 0

    def test_diagnose_detects_test_data(self, db):
        db.insert_mistake({
            "mistake": "test mistake",
            "why": "test reason",
            "project": "global",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.insert_reflection({
            "went_well": "went well",
            "missed": "was missed",
            "do_differently": "do different",
            "project": "global",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        findings = db.diagnose()
        assert findings["test_mistakes"] >= 1
        assert findings["test_reflections"] >= 1

    def test_fix_hygiene_archives_test_data(self, db):
        db.insert_mistake({
            "mistake": "test mistake",
            "why": "test reason",
            "project": "global",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.insert_mistake({
            "mistake": "Real mistake about production",
            "why": "Actual reason",
            "project": "global",
            "created_at": "2026-03-23T11:00:00+00:00",
        })
        db.conn.commit()
        fixes = db.fix_hygiene()
        assert fixes["test_mistakes_archived"] == 1
        # Active queries exclude the archived row...
        assert db.count_mistakes() == 1
        assert len(db.get_mistakes()) == 1
        # ...but the row is still in the table (mistakes are never deleted)
        total = db.conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
        assert total == 2
        # Idempotent — re-run archives nothing new
        assert db.fix_hygiene()["test_mistakes_archived"] == 0

    def test_fix_hygiene_archives_test_reflections(self, db):
        db.insert_reflection({
            "went_well": "went well",
            "missed": "was missed",
            "do_differently": "do different",
            "project": "global",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        fixes = db.fix_hygiene()
        assert fixes["test_reflections_archived"] == 1
        assert db.count_reflections() == 0
        total = db.conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        assert total == 1  # soft-archived, never deleted

    def test_fix_hygiene_dry_run(self, db):
        db.insert_mistake({
            "mistake": "test mistake",
            "why": "test reason",
            "project": "global",
            "created_at": "2026-03-23T10:00:00+00:00",
        })
        db.conn.commit()
        fixes = db.fix_hygiene(dry_run=True)
        assert fixes["test_mistakes_archived"] == 1
        assert db.count_mistakes() == 1  # Not actually archived


class TestMigration:
    def _write_jsonl(self, path: str, entries: list[dict]):
        with open(path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")

    def test_migrate_knowledge(self, tmp_path):
        self._write_jsonl(str(tmp_path / "knowledge.jsonl"), [
            {
                "fact": "Aleph uses tree-sitter",
                "confidence": 0.9,
                "content_hash": "abc123",
                "project": "Aleph",
                "ts": "2026-03-23T10:00:00+00:00",
                "created_at": "2026-03-23T10:00:00+00:00",
            },
            {
                "fact": "Null uses SQLite now",
                "confidence": 0.85,
                "content_hash": "def456",
                "project": "null",
                "ts": "2026-03-23T11:00:00+00:00",
                "created_at": "2026-03-23T11:00:00+00:00",
            },
        ])

        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["facts"] == 2

        db = NullDB(str(tmp_path))
        assert db.count_facts() == 2
        # Verify project was normalized
        fact = db.get_fact_by_id("abc123")
        assert fact["project"] == "aleph"
        db.close()

    def test_migrate_decisions(self, tmp_path):
        self._write_jsonl(str(tmp_path / "decisions.jsonl"), [
            {
                "decision": "Use SQLite",
                "reasoning": "Zero deps",
                "project": "null",
                "ts": "2026-03-23T10:00:00+00:00",
                "created_at": "2026-03-23T10:00:00+00:00",
            },
        ])
        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["decisions"] == 1

    def test_migrate_mistakes(self, tmp_path):
        self._write_jsonl(str(tmp_path / "mistakes.jsonl"), [
            {
                "mistake": "Forgot observe",
                "why": "Discipline",
                "project": "global",
                "ts": "2026-03-23T10:00:00+00:00",
                "created_at": "2026-03-23T10:00:00+00:00",
            },
        ])
        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["mistakes"] == 1

    def test_migrate_reflections(self, tmp_path):
        self._write_jsonl(str(tmp_path / "reflections.jsonl"), [
            {
                "went_well": "Shipped on time",
                "missed": "Windows testing",
                "do_differently": "Test earlier",
                "project": "null",
                "ts": "2026-03-23T10:00:00+00:00",
                "created_at": "2026-03-23T10:00:00+00:00",
            },
        ])
        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["reflections"] == 1

    def test_migrate_exemplars(self, tmp_path):
        self._write_jsonl(str(tmp_path / "exemplars.jsonl"), [
            {
                "scenario": "test",
                "pete": "push it",
                "atlas": "Done.",
                "calibration": "Execute immediately",
                "tags": ["execution"],
            },
        ])
        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["exemplars"] == 1

    def test_migrate_deduplicates_by_hash(self, tmp_path):
        self._write_jsonl(str(tmp_path / "knowledge.jsonl"), [
            {
                "fact": "First version",
                "content_hash": "same_hash",
                "confidence": 0.7,
                "project": "global",
                "created_at": "2026-03-23T10:00:00+00:00",
            },
            {
                "fact": "Updated version",
                "content_hash": "same_hash",
                "confidence": 0.9,
                "project": "global",
                "created_at": "2026-03-23T11:00:00+00:00",
            },
        ])
        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["facts"] == 1  # Deduped
        db = NullDB(str(tmp_path))
        fact = db.get_fact_by_id("same_hash")
        assert fact["fact"] == "Updated version"  # Last-write-wins
        db.close()

    def test_migrate_backs_up_files(self, tmp_path):
        self._write_jsonl(str(tmp_path / "knowledge.jsonl"), [
            {"fact": "test", "content_hash": "h1", "project": "global",
             "created_at": "2026-03-23T10:00:00+00:00"},
        ])
        migrate_jsonl_to_sqlite(str(tmp_path))
        assert not os.path.isfile(str(tmp_path / "knowledge.jsonl"))
        assert os.path.isfile(str(tmp_path / "knowledge.jsonl.bak"))

    def test_migrate_creates_db(self, tmp_path):
        self._write_jsonl(str(tmp_path / "knowledge.jsonl"), [
            {"fact": "test", "content_hash": "h1", "project": "global",
             "created_at": "2026-03-23T10:00:00+00:00"},
        ])
        migrate_jsonl_to_sqlite(str(tmp_path))
        assert os.path.isfile(str(tmp_path / "memory.db"))

    def test_needs_migration(self, tmp_path):
        db = NullDB(str(tmp_path))
        assert db.needs_migration is False

        # Create JSONL file without DB
        self._write_jsonl(str(tmp_path / "knowledge.jsonl"), [
            {"fact": "test", "content_hash": "h1", "project": "global",
             "created_at": "2026-03-23T10:00:00+00:00"},
        ])
        db2 = NullDB(str(tmp_path))
        assert db2.needs_migration is True

    def test_no_migration_when_db_exists(self, tmp_path):
        # Create DB first
        db = NullDB(str(tmp_path))
        db.initialize()
        db.close()
        # Then create JSONL (shouldn't trigger migration)
        self._write_jsonl(str(tmp_path / "knowledge.jsonl"), [
            {"fact": "test", "content_hash": "h1", "project": "global",
             "created_at": "2026-03-23T10:00:00+00:00"},
        ])
        db2 = NullDB(str(tmp_path))
        assert db2.needs_migration is False

    def test_migrate_empty_dir(self, tmp_path):
        # No JSONL files at all — should still work (empty DB)
        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["facts"] == 0
        assert counts["decisions"] == 0
        assert os.path.isfile(str(tmp_path / "memory.db"))

    def test_migrate_handles_malformed_json(self, tmp_path):
        with open(str(tmp_path / "knowledge.jsonl"), "w") as f:
            f.write('{"fact": "good", "content_hash": "h1", "project": "g", "created_at": "2026-03-23T10:00:00+00:00"}\n')
            f.write('not valid json\n')
            f.write('{"fact": "also good", "content_hash": "h2", "project": "g", "created_at": "2026-03-23T11:00:00+00:00"}\n')
        counts = migrate_jsonl_to_sqlite(str(tmp_path))
        assert counts["facts"] == 2  # Skipped the bad line
