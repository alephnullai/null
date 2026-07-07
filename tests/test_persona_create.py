"""`null persona create <name>` — clean worker seat (org topology, #21).

A new hire (Steve on Linux, Athena on Windows) must initialize with ZERO
identity bleed from the default 'atlas' personality: own store dir + db,
own registry rows, no anchors, no code word, no atlas rows anywhere.
Also covers:
  • --store-remote wiring — the seat's store becomes its OWN git repo
    pointed at its own remote (local bare repo in tests), never the
    hub's, and is gitignored from a hub repo it nests inside
  • structural-heal regression — healing a worker store seeds the
    store's OWN personality row, never 'atlas'
  • MCP boot-identity on a worker store attributes its writes to the
    store's personality

All paths go through NULL_DIR (set to tmp_path by the autouse sandbox
guard), so nothing touches the real ~/.null.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from null_memory.db import NullDB
from null_memory.migrate_v3 import (
    UNIFIED_SCHEMA_VERSION,
    heal_unified_structure,
    verify_unified_structure,
)
from null_memory.persona_wizard import create_worker, init_store_repo


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )


def _store_conn(result: dict) -> sqlite3.Connection:
    return sqlite3.connect(os.path.join(result["dir"], "memory.db"))


def _tables_with_personality_column(conn) -> list[str]:
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    out = []
    for t in tables:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
        if "personality" in cols:
            out.append(t)
    return out


def _assert_no_atlas_rows(conn) -> None:
    """No 'atlas' value anywhere a personality can be recorded."""
    row = conn.execute(
        "SELECT COUNT(*) FROM personalities WHERE name = 'atlas'"
    ).fetchone()
    assert row[0] == 0, "atlas row leaked into the worker's registry"
    for t in _tables_with_personality_column(conn):
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE personality = 'atlas'"
        ).fetchone()[0]
        assert n == 0, f"atlas-attributed rows leaked into {t}"


# ── Clean seat end-to-end ──────────────────────────────────────────────────


class TestCreateWorker:
    def test_registers_store_and_identity(self, tmp_path):
        result = create_worker(
            "steve", focus="hiwave-linux", description="Linux seat",
        )
        store = Path(result["dir"])
        assert store.is_dir()
        assert store == tmp_path / "personalities" / "steve"

        # identity.json is the seat's own — empty working identity
        with open(store / "identity.json") as f:
            identity = json.load(f)
        assert identity["name"] == "steve"
        assert identity["role"] == "worker"
        assert identity["focus"] == "hiwave-linux"
        assert identity["bootstrapped_from"] is None

        # multiverse registry row — dir is stored RELATIVE to the hub
        # base (issue #23: multiverse.db syncs across machines, so it
        # must never carry machine-absolute paths)
        mv = sqlite3.connect(tmp_path / "multiverse.db")
        try:
            row = mv.execute(
                "SELECT role, focus, dir FROM personalities "
                "WHERE name='steve' AND active=1"
            ).fetchone()
            assert row is not None
            assert row[0] == "worker"
            assert row[1] == "hiwave-linux"
            assert row[2] == "personalities/steve"
            assert Path(tmp_path / row[2]) == store
        finally:
            mv.close()

    def test_store_db_has_own_row_with_role_and_no_atlas(self, tmp_path):
        result = create_worker("athena", role="worker", focus="hiwave-windows")
        conn = _store_conn(result)
        try:
            # Unified per-store layout, stamped, structurally correct
            assert verify_unified_structure(conn) == []
            stamp = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            assert int(stamp) == UNIFIED_SCHEMA_VERSION

            row = conn.execute(
                "SELECT role, focus, active FROM personalities "
                "WHERE name='athena'"
            ).fetchone()
            assert row is not None
            assert row[0] == "worker"
            assert row[1] == "hiwave-windows"
            assert row[2] == 1

            _assert_no_atlas_rows(conn)
        finally:
            conn.close()

    def test_identity_starts_empty_no_anchors_no_code_word(self, tmp_path):
        result = create_worker("steve")
        conn = _store_conn(result)
        try:
            assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM facts WHERE anchor_type IS NOT NULL"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM facts WHERE anchor_type='code_word'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM exemplars"
            ).fetchone()[0] == 0
        finally:
            conn.close()

    def test_role_argument_is_respected(self, tmp_path):
        result = create_worker("helper", role="executor")
        conn = _store_conn(result)
        try:
            row = conn.execute(
                "SELECT role FROM personalities WHERE name='helper'"
            ).fetchone()
            assert row[0] == "executor"
        finally:
            conn.close()

    def test_reserved_name_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="reserved"):
            create_worker("atlas")

    def test_invalid_name_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="lowercase"):
            create_worker("Bad Name!")

    def test_atlas_role_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="role"):
            create_worker("steve", role="atlas")

    def test_duplicate_rejected(self, tmp_path):
        create_worker("steve")
        with pytest.raises(ValueError, match="already exists"):
            create_worker("steve")

    def test_mcp_config_snippet_serves_the_store(self, tmp_path):
        result = create_worker("steve")
        config = json.loads(result["mcp_config"])
        assert "steve" in config
        assert result["dir"] in config["steve"]["args"]
        assert result["remote"] is None
        assert result["store_repo"] is None

    def test_mcp_config_snippet_carries_type_and_env_hardening(self, tmp_path):
        """Issue #24: the paste-ready snippet must include type=stdio and
        the git env hardening — its omission wedged a Windows seat on its
        first unauthenticated git push (9-minute hang class)."""
        result = create_worker("steve")
        entry = json.loads(result["mcp_config"])["steve"]
        assert entry["type"] == "stdio"
        assert entry["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert entry["env"]["GCM_INTERACTIVE"] == "never"


# ── CLI surface ────────────────────────────────────────────────────────────


def test_cli_persona_create_positional(tmp_path):
    from tests.conftest import run_null

    rc, out, _ = run_null(
        "persona", "create", "steve", "--focus", "hiwave-linux",
        tmp_path=tmp_path,
    )
    assert rc == 0
    assert "Created persona 'steve'" in out
    assert "worker" in out
    assert str(tmp_path / "personalities" / "steve") in out
    assert "mcpServers" in out or "serve" in out
    # store actually exists with its own row
    conn = sqlite3.connect(
        tmp_path / "personalities" / "steve" / "memory.db"
    )
    try:
        row = conn.execute(
            "SELECT role FROM personalities WHERE name='steve'"
        ).fetchone()
        assert row is not None and row[0] == "worker"
        _assert_no_atlas_rows(conn)
    finally:
        conn.close()


def test_cli_positional_plus_template_errors(tmp_path):
    from tests.conftest import run_null

    rc, out, _ = run_null(
        "persona", "create", "steve", "--template", "warm-coach",
        tmp_path=tmp_path,
    )
    assert rc != 0
    assert "not both" in out


# ── --store-remote wiring ──────────────────────────────────────────────────


class TestStoreRemote:
    @pytest.fixture
    def hub_repo(self, tmp_path):
        """Make NULL_DIR itself a git repo with the HUB's remote — the
        layout on a real hub machine (~/.null synced to null-atlas)."""
        hub_remote = tmp_path / "hub-remote.git"
        _git(["init", "--bare", str(hub_remote)], cwd=tmp_path)
        _git(["init"], cwd=tmp_path)
        _git(["remote", "add", "origin", str(hub_remote)], cwd=tmp_path)
        return tmp_path

    def test_own_repo_own_remote_never_hub(self, hub_repo, tmp_path):
        bare = tmp_path / "null-athena.git"
        _git(["init", "--bare", str(bare)], cwd=tmp_path)

        result = create_worker(
            "athena", focus="hiwave-windows", store_remote=str(bare),
        )
        store = Path(result["dir"])
        repo = result["store_repo"]

        # Own .git at the store root — MemoryRepo's upward walk stops here
        assert (store / ".git").is_dir()
        from null_memory.session import MemoryRepo
        assert Path(MemoryRepo(str(store)).repo_dir) == store

        # Remote is the seat's own, never the hub's
        url = _git(["remote", "get-url", "origin"], cwd=store).stdout.strip()
        assert url == str(bare)
        hub_url = _git(
            ["remote", "get-url", "origin"], cwd=tmp_path
        ).stdout.strip()
        assert hub_url != url

        # Initial commit pushed to the local bare remote
        assert repo["pushed"] is True
        ls = _git(["ls-remote", "--heads", str(bare)], cwd=tmp_path)
        assert ls.stdout.strip(), "no branch arrived at the seat's remote"

        # Hub repo gitignores the nested seat store
        assert repo["hub_gitignored"] is True
        gitignore = (tmp_path / ".gitignore").read_text()
        assert "personalities/athena/" in gitignore
        status = _git(["status", "--porcelain"], cwd=tmp_path).stdout
        assert "personalities/athena" not in status

    def test_no_remote_changes_nothing(self, hub_repo, tmp_path):
        result = create_worker("steve")
        store = Path(result["dir"])
        assert not (store / ".git").exists()
        gi = tmp_path / ".gitignore"
        if gi.exists():
            assert "personalities/steve" not in gi.read_text()

    def test_init_store_repo_idempotent(self, tmp_path):
        bare = tmp_path / "remote.git"
        _git(["init", "--bare", str(bare)], cwd=tmp_path)
        store = tmp_path / "personalities" / "w"
        store.mkdir(parents=True)
        first = init_store_repo(str(store), str(bare), str(tmp_path))
        second = init_store_repo(str(store), str(bare), str(tmp_path))
        assert first["initialized"] is True
        assert second["initialized"] is False
        url = _git(["remote", "get-url", "origin"], cwd=store).stdout.strip()
        assert url == str(bare)


# ── Audit regression: structural heal on a worker store ───────────────────


class TestHealRegression:
    def test_heal_on_worker_store_seeds_own_personality(self, tmp_path):
        result = create_worker("testworker", focus="testing")
        conn = _store_conn(result)
        try:
            # Re-running the heal on a correct store is a pure no-op
            assert heal_unified_structure(
                conn, default_personality="testworker") == []
            _assert_no_atlas_rows(conn)

            # Degrade the store (lost registry) and heal: the seeded row
            # must be the store's OWN personality, never 'atlas'.
            conn.execute("DROP TABLE personalities")
            conn.commit()
            actions = heal_unified_structure(
                conn, default_personality="testworker")
            assert any("personalities" in a for a in actions)
            names = [
                r[0] for r in conn.execute(
                    "SELECT name FROM personalities").fetchall()
            ]
            assert names == ["testworker"]
            row = conn.execute(
                "SELECT role FROM personalities WHERE name='testworker'"
            ).fetchone()
            assert row[0] == "worker"
            _assert_no_atlas_rows(conn)
        finally:
            conn.close()

    def test_initialize_heal_path_uses_store_personality(self, tmp_path):
        """NullDB.initialize on the worker's store (the serve path) heals
        with the store's personality — end-to-end, no atlas rows."""
        result = create_worker("testworker")
        # Degrade: drop the registry so initialize must heal
        conn = _store_conn(result)
        conn.execute("DROP TABLE personalities")
        conn.commit()
        conn.close()

        db = NullDB(result["dir"], personality="testworker")
        db.initialize()
        try:
            assert verify_unified_structure(db.conn) == []
            names = [
                r[0] for r in db.conn.execute(
                    "SELECT name FROM personalities").fetchall()
            ]
            assert names == ["testworker"]
            _assert_no_atlas_rows(db.conn)
        finally:
            db.close()

    def test_identity_payload_renders_store_personality(self, tmp_path):
        """The boot-prompt identity template names the store's own
        personality — previously hardcoded 'You are Atlas'."""
        from null_memory.identity_payload import build_identity_payload

        result = create_worker("steve")
        db = NullDB(result["dir"], personality="steve")
        db.initialize()
        try:
            payload = build_identity_payload(db.conn, personality="steve")
            assert "STEVE IDENTITY" in payload.text
            assert "You are Steve." in payload.text
            assert "Atlas" not in payload.text
        finally:
            db.close()

    def test_mcp_instructions_personalized(self, tmp_path):
        from null_memory.mcp.server import (
            SYSTEM_INSTRUCTIONS,
            instructions_for_personality,
        )

        # atlas: byte-identical to the shipped instructions
        assert instructions_for_personality("atlas") == SYSTEM_INSTRUCTIONS
        s = instructions_for_personality("steve")
        assert "You are Steve." in s
        assert "You are Atlas." not in s
        # only the identity line changes — memory guidance is generic
        assert "null_remember" in s

    def test_session_records_carry_store_personality(self, tmp_path):
        """Session records on a worker store are attributed to it —
        previously the Session dataclass default ('atlas') leaked in."""
        from null_memory.session import SessionManager

        result = create_worker("steve")
        mgr = SessionManager(result["dir"], personality="steve")
        session = mgr.start_session(defer_git=True)
        assert session.personality == "steve"
        record = json.loads(
            (Path(result["dir"]) / "sessions"
             / f"{session.session_id}.json").read_text()
        )
        assert record["personality"] == "steve"

    def test_boot_identity_attributes_to_store_personality(self, tmp_path):
        """MCP boot on a worker store writes session_verifications rows
        for the store's personality — previously hardcoded 'atlas'."""
        from null_memory.mcp.server import SYSTEM_INSTRUCTIONS, _boot_identity

        result = create_worker("testworker")
        db = NullDB(result["dir"], personality="testworker")
        db.initialize()

        class _StubMemory:
            def __init__(self, d):
                self.db = d
                self.personality = "testworker"

        class _StubHandlers:
            def __init__(self, d):
                self.memory = _StubMemory(d)
                self.agent_dir = result["dir"]

        try:
            _boot_identity(_StubHandlers(db), SYSTEM_INSTRUCTIONS)
            rows = [
                r[0] for r in db.conn.execute(
                    "SELECT DISTINCT personality FROM session_verifications"
                ).fetchall()
            ]
            assert "atlas" not in rows
            assert rows in ([], ["testworker"])
            _assert_no_atlas_rows(db.conn)
        finally:
            db.close()
