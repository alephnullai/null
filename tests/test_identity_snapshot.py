"""Tests for the resilience-bridge identity snapshot (IDENTITY.md).

Identity must survive the Null MCP server being down: a static IDENTITY.md
that loads with ZERO Null dependency. These tests seed a tmp_path unified
store, write a snapshot (directly and via the `null sync-anchors` CLI), and
assert the file exists, is non-empty, and carries the code-word FINGERPRINT
— never the code word itself, which lives only in the database (the
snapshot lands in a git-pushed directory).
"""

from __future__ import annotations

import hashlib
import os
import stat

import pytest

from null_memory.agent import AgentMemory
from null_memory.identity_payload import (
    build_identity_payload, write_identity_snapshot,
)
from null_memory.migrate_v3 import init_unified_db

from tests.conftest import run_null

# Dummy verification phrase (the real one lives only in the database).
# _fetch_code_word matches the 'code word:' LABEL via LIKE even with no
# 'code_word' anchor, so a labeled fact is enough.
CODE_WORD = "dummy-code-word for tests, not the real phrase"


@pytest.fixture
def seeded_store(tmp_path, monkeypatch):
    """A tmp_path unified store seeded with identity material.

    Returns (mem, agent_dir). NULL_DIR points at tmp_path so the CLI
    resolves the same agent_dir (<tmp>/atlas) the in-process AgentMemory uses.
    """
    init_unified_db(str(tmp_path / "unified.db")).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified, "expected unified mode"

    mem.learn(
        f"Atlas code word: {CODE_WORD}",
        confidence=0.99, project="global",
    )
    origin = mem.learn(
        "Pete and Atlas began working together building the Null memory system",
        confidence=0.95, project="global",
    )
    mem.anchor(origin["id"], "origin", note="where it started")
    mem.decide(
        "Persist identity to a static IDENTITY.md",
        "so identity survives the MCP server being down (resilience bridge)",
        project="null",
    )
    mem.db.conn.commit()
    return mem, str(agent_dir)


def test_write_identity_snapshot_direct(seeded_store):
    """Calling write_identity_snapshot directly creates a usable card."""
    mem, agent_dir = seeded_store

    path = write_identity_snapshot(
        mem.db.conn, personality="atlas", agent_dir=agent_dir,
    )

    assert path is not None
    assert os.path.isfile(path)
    assert path == os.path.join(agent_dir, "IDENTITY.md")

    content = open(path, encoding="utf-8").read()
    assert content.strip(), "snapshot must be non-empty"
    assert "Verification fingerprint" in content
    assert "Identity Snapshot" in content
    # The banner makes the no-Null-needed contract explicit.
    assert "needs no running Null process" in content


def test_write_identity_snapshot_custom_dest(seeded_store):
    """An explicit dest path is honored and written atomically."""
    mem, agent_dir = seeded_store
    dest = os.path.join(agent_dir, "card.md")

    path = write_identity_snapshot(
        mem.db.conn, personality="atlas", agent_dir=agent_dir, dest=dest,
    )

    assert path == dest
    assert os.path.isfile(dest)
    assert "Verification fingerprint" in open(dest, encoding="utf-8").read()


def test_snapshot_never_contains_code_word_plaintext(seeded_store):
    """SECURITY: session-close pushes the agent dir to a remote — the code
    word must never land on disk in plaintext. Only its SHA-256 fingerprint
    prefix appears, labeled as a verification fingerprint."""
    mem, agent_dir = seeded_store

    path = write_identity_snapshot(
        mem.db.conn, personality="atlas", agent_dir=agent_dir,
    )
    assert path is not None
    content = open(path, encoding="utf-8").read()

    # The code-word phrase (and therefore the full fact) is absent.
    assert CODE_WORD not in content
    # The fingerprint of the code-word fact IS present.
    payload = build_identity_payload(mem.db.conn, personality="atlas")
    assert payload.code_word, "fixture must yield a code word"
    fingerprint = hashlib.sha256(
        payload.code_word.strip().encode("utf-8")
    ).hexdigest()[:12]
    assert fingerprint in content
    assert "Verification fingerprint" in content
    # The card explains where the real code word lives.
    assert "only in the database" in content


def test_anchored_code_word_never_renders_in_anchor_list(seeded_store):
    """SECURITY regression (live leak, 2026-06-12): a code-word fact that
    is ALSO anchored as anchor_type='code_word' — which is what a real
    naming ceremony produces — must not have its fact text rendered by
    the Relationship Anchors loop. The original test above never anchored
    its code word, so the anchors-loop path shipped unexercised and
    leaked the full phrase into a pushed IDENTITY.md (with the dedicated
    Code Word section, correctly redacted, sitting right above it)."""
    mem, agent_dir = seeded_store

    row = mem.db.conn.execute(
        "SELECT id FROM facts WHERE fact LIKE '%code word:%'").fetchone()
    assert row is not None
    # Direct SQL, not mem.anchor(): 'code_word' is not in ANCHOR_TYPES, so
    # the API can't create one — but real stores carry them (the athena
    # and atlas ceremonies both anchored via SQL), identity_payload's
    # ANCHOR_PRIORITY ranks them first, and _fetch_anchors returns them.
    # The renderer must survive the data that exists in the wild.
    mem.db.conn.execute(
        "UPDATE facts SET anchor_type='code_word', "
        "anchor_note='naming ceremony', anchor_at=datetime('now') "
        "WHERE id=?", (row[0],))
    mem.db.conn.commit()

    path = write_identity_snapshot(
        mem.db.conn, personality="atlas", agent_dir=agent_dir,
    )
    content = open(path, encoding="utf-8").read()

    # The phrase appears NOWHERE — not in the Code Word section, not in
    # the anchors list, not anywhere else in the rendered card.
    assert CODE_WORD not in content
    # The anchor is acknowledged but redacted.
    assert "**[code_word]** (redacted" in content
    # Other anchors still render their text normally.
    assert "began working together" in content


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_snapshot_is_owner_only_0600(seeded_store):
    """The snapshot carries identity material — it must be chmod 0600."""
    mem, agent_dir = seeded_store

    path = write_identity_snapshot(
        mem.db.conn, personality="atlas", agent_dir=agent_dir,
    )
    assert path is not None
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_snapshot_written_even_when_incomplete(tmp_path, monkeypatch):
    """A bare store (no anchors/decisions/probes => incomplete payload) still
    yields a snapshot — whatever exists is better than nothing."""
    init_unified_db(str(tmp_path / "unified.db")).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    mem.learn(f"Code word: {CODE_WORD}", confidence=0.9, project="global")
    mem.db.conn.commit()

    path = write_identity_snapshot(
        mem.db.conn, personality="atlas", agent_dir=str(agent_dir),
    )

    assert path is not None and os.path.isfile(path)
    content = open(path, encoding="utf-8").read()
    assert content.strip()
    assert CODE_WORD not in content  # never plaintext on disk
    assert "Verification fingerprint" in content


def test_sync_anchors_cli(seeded_store):
    """`null sync-anchors` writes IDENTITY.md under NULL_DIR with no server."""
    mem, agent_dir = seeded_store

    # NULL_DIR is already set by the fixture; run_null inherits the env but
    # we pass tmp explicitly so resolution is unambiguous.
    rc, out, err = run_null("sync-anchors", tmp_path=os.path.dirname(agent_dir))

    assert rc == 0
    assert "wrote identity snapshot" in out
    assert "code word: yes" in out

    snapshot = os.path.join(agent_dir, "IDENTITY.md")
    assert os.path.isfile(snapshot)
    content = open(snapshot, encoding="utf-8").read()
    assert content.strip()
    assert CODE_WORD not in content  # never plaintext on disk
    assert "Verification fingerprint" in content


def test_sync_anchors_legacy_per_personality_store(tmp_path, monkeypatch):
    """Resilience: a fresh per-personality (legacy) store has NO `personalities`
    table — only `facts` etc. `null sync-anchors` must still succeed and write
    IDENTITY.md with the code word, rather than failing the whole payload build
    on `no such table: personalities`.

    Seeds the store via AgentMemory (NOT init_unified_db) so the schema is the
    genuine legacy per-personality shape, then drives the CLI in a subprocess.
    """
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    # Direct AgentMemory on tmp_path => per-personality store at <tmp>/memory.db.
    # Crucially we do NOT create a unified.db, so there is no `personalities`
    # table — the exact shape that used to break the bridge.
    mem = AgentMemory.load(agent_dir=str(tmp_path), personality="atlas")
    assert not mem.db.unified, "expected legacy per-personality mode"

    mem.learn(
        f"Atlas code word: {CODE_WORD}",
        confidence=0.99, project="global",
    )
    mem.db.conn.commit()

    # Sanity: the legacy store really lacks a `personalities` table.
    tables = {
        r[0] for r in mem.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "personalities" not in tables

    rc, out, err = run_null("sync-anchors", tmp_path=str(tmp_path))

    assert rc == 0, f"sync-anchors failed: {out!r} / {err!r}"
    assert "could not render an identity payload" not in (out + err)
    assert "wrote identity snapshot" in out
    assert "code word: yes" in out

    snapshot = os.path.join(str(tmp_path), "IDENTITY.md")
    assert os.path.isfile(snapshot)
    content = open(snapshot, encoding="utf-8").read()
    assert content.strip()
    assert CODE_WORD not in content  # never plaintext on disk
    assert "Verification fingerprint" in content


# ── Boot-hook clobber guard ───────────────────────────────────────────


class _SnapDB:
    def __init__(self, conn):
        self.conn = conn


class _SnapMemory:
    def __init__(self, conn):
        self.db = _SnapDB(conn)


class _SnapHandlers:
    def __init__(self, conn, agent_dir):
        self.memory = _SnapMemory(conn)
        self.agent_dir = agent_dir


@pytest.fixture
def stub_embedder(monkeypatch):
    """Keep the boot hook off fastembed — coherence is not under test."""
    class _StubEngine:
        model_name = "stub"
        available = False

        def __init__(self, conn):
            pass

        def embed(self, text):
            return None

    monkeypatch.setattr(
        "null_memory.embeddings.EmbeddingEngine", _StubEngine,
    )


def test_boot_hook_skips_snapshot_on_incomplete_payload(
        tmp_path, stub_embedder):
    """Booting against a fresh/misconfigured store must NOT clobber the last
    good IDENTITY.md with '(unset)' content."""
    import sqlite3

    from null_memory.mcp.server import SYSTEM_INSTRUCTIONS, _boot_identity

    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(str(db_path))  # empty store => incomplete payload

    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    last_good = "# Atlas — Identity Snapshot\n\nLAST GOOD CONTENT\n"
    snapshot = agent_dir / "IDENTITY.md"
    snapshot.write_text(last_good, encoding="utf-8")

    try:
        _boot_identity(
            _SnapHandlers(conn, str(agent_dir)), SYSTEM_INSTRUCTIONS,
        )
    finally:
        conn.close()

    assert snapshot.read_text(encoding="utf-8") == last_good, (
        "incomplete payload must not overwrite the last good snapshot"
    )


def test_boot_hook_writes_snapshot_on_complete_payload(
        tmp_path, stub_embedder):
    """Guard sanity check: a COMPLETE payload still refreshes IDENTITY.md."""
    import sqlite3

    from null_memory.mcp.server import SYSTEM_INSTRUCTIONS, _boot_identity
    from tests.test_boot_identity import _seed_full_identity

    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(str(db_path))
    _seed_full_identity(conn)

    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    snapshot = agent_dir / "IDENTITY.md"
    snapshot.write_text("stale", encoding="utf-8")

    try:
        _boot_identity(
            _SnapHandlers(conn, str(agent_dir)), SYSTEM_INSTRUCTIONS,
        )
    finally:
        conn.close()

    content = snapshot.read_text(encoding="utf-8")
    assert content != "stale"
    assert "Identity Snapshot" in content
    assert "Verification fingerprint" in content
