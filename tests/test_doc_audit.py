"""Tests for Hypnos Stage 8 — worktree doc audit.

Pure-function tests. LLM dependency-injected via stub callable.
The verifiers (file_ref / ship_status / schema_version) hit the
real filesystem + git, so tests build small synthetic trees in tmp_path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess

import pytest

from null_memory.doc_audit import (
    _verify_file_ref,
    _verify_schema_version,
    _verify_ship_status,
    audit_doc,
)
from null_memory.migrate_v3 import init_unified_db


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "u.db"
    init_unified_db(str(db_path)).close()
    c = sqlite3.connect(db_path)
    yield c
    c.close()


def _llm_returning(claims: list[dict[str, str]]):
    def _call(prompt: str) -> str:
        return json.dumps({"claims": claims})
    return _call


def _make_doc(tmp_path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    return str(path)


def _git_repo(tmp_path) -> str:
    """Create a tiny git repo with one commit so verifiers have something
    to grep."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat(phase 7): launchd daemon shipped"],
        cwd=root, check=True,
    )
    return str(root)


# ── Extraction + upsert ──────────────────────────────────────────────


def test_audit_extracts_and_upserts_claims(tmp_path, conn):
    doc = _make_doc(tmp_path, "CLAUDE.md", "Phase 7.3 hasn't shipped.")
    counts = audit_doc(
        doc, conn,
        _llm_returning([
            {"text": "Phase 7.3 hasn't shipped", "type": "ship_status"},
        ]),
        repo_root=str(tmp_path),
    )
    assert counts["extracted"] == 1
    rows = conn.execute(
        "SELECT claim_text, claim_type, status FROM doc_claims"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Phase 7.3 hasn't shipped"
    assert rows[0][1] == "ship_status"


def test_audit_idempotent_upsert(tmp_path, conn):
    """Running audit twice on the same doc doesn't duplicate rows."""
    doc = _make_doc(tmp_path, "CLAUDE.md", "Some content.")
    stub = _llm_returning([
        {"text": "phase 7 ships", "type": "ship_status"},
        {"text": "src/null_memory/cli.py", "type": "file_ref"},
    ])
    audit_doc(doc, conn, stub, repo_root=str(tmp_path))
    audit_doc(doc, conn, stub, repo_root=str(tmp_path))
    n = conn.execute("SELECT COUNT(*) FROM doc_claims").fetchone()[0]
    assert n == 2


def test_audit_skips_oversize_file(tmp_path, conn):
    doc = _make_doc(tmp_path, "huge.md", "x" * 60_000)
    counts = audit_doc(doc, conn, _llm_returning([]), repo_root=str(tmp_path))
    assert counts["extracted"] == 0
    assert "too large" in counts["skipped_reason"]


def test_audit_skips_empty_file(tmp_path, conn):
    doc = _make_doc(tmp_path, "empty.md", "")
    counts = audit_doc(doc, conn, _llm_returning([]), repo_root=str(tmp_path))
    assert "empty" in counts["skipped_reason"]


def test_audit_handles_missing_file(tmp_path, conn):
    counts = audit_doc(
        str(tmp_path / "ghost.md"), conn, _llm_returning([]),
        repo_root=str(tmp_path),
    )
    assert "not found" in counts["skipped_reason"]


def test_audit_handles_llm_failure(tmp_path, conn):
    doc = _make_doc(tmp_path, "x.md", "content")
    def _boom(_p):
        raise RuntimeError("API down")
    counts = audit_doc(doc, conn, _boom, repo_root=str(tmp_path))
    assert "llm failed" in counts["skipped_reason"]


# ── file_ref verifier ────────────────────────────────────────────────


def test_file_ref_verifier_passes_when_file_exists(tmp_path):
    (tmp_path / "real.py").write_text("# real")
    verdict, evidence = _verify_file_ref("see real.py", str(tmp_path))
    assert verdict == "verified"
    assert evidence is None


def test_file_ref_verifier_refutes_when_file_missing(tmp_path):
    verdict, evidence = _verify_file_ref(
        "see ghost.py for details", str(tmp_path),
    )
    assert verdict == "refuted"
    assert "ghost.py" in evidence


def test_file_ref_verifier_unverified_when_no_path_in_text(tmp_path):
    verdict, _ = _verify_file_ref("just prose, no paths", str(tmp_path))
    assert verdict == "unverified"


def test_file_ref_verifier_expands_tilde(fake_home, tmp_path):
    """Claims like '~/.foo.json' must resolve to the home dir, not
    get joined onto repo_root. Regression: glob.glob path bug + missing
    ~ in _PATH_RE caused these to refute as missing.

    Uses fake_home (not a bare HOME monkeypatch): on Windows expanduser
    reads USERPROFILE, not HOME — HOME-only redirection hits the real
    profile and the file is 'missing' (issue #2 failure 1)."""
    (tmp_path / ".test_real.json").write_text("{}")
    verdict, _ = _verify_file_ref(
        "config lives at ~/.test_real.json normally",
        str(tmp_path / "some_repo"),
    )
    assert verdict == "verified"


def test_file_ref_verifier_refutes_missing_tilde_path(fake_home, tmp_path):
    verdict, evidence = _verify_file_ref(
        "should be at ~/.never_existed.json here",
        str(tmp_path / "some_repo"),
    )
    assert verdict == "refuted"
    assert "~/.never_existed.json" in evidence or ".never_existed.json" in evidence


# ── ship_status verifier ─────────────────────────────────────────────


def test_ship_status_refuted_when_git_log_shows_phase(tmp_path):
    """Doc claims 'Phase 7 hasn't shipped', git log mentions it → refute."""
    repo = _git_repo(tmp_path)
    verdict, evidence = _verify_ship_status(
        "Phase 7 hasn't shipped yet", repo,
    )
    assert verdict == "refuted"
    assert "phase 7" in evidence.lower()


def test_ship_status_unverified_for_unship_claim_with_no_git_log(tmp_path):
    """Doc claims 'Phase 99 hasn't shipped' and git log has no mention →
    unverified (we can't prove it negative)."""
    repo = _git_repo(tmp_path)
    verdict, _ = _verify_ship_status("Phase 99 hasn't shipped", repo)
    assert verdict == "unverified"


def test_ship_status_no_phase_number(tmp_path):
    repo = _git_repo(tmp_path)
    verdict, _ = _verify_ship_status("Stuff is shipped probably", repo)
    assert verdict == "unverified"


# ── schema_version verifier ──────────────────────────────────────────


def test_schema_version_verifier_refutes_drift(tmp_path):
    """Synthetic repo with a migrate_v3.py declaring v21; doc claims v5."""
    repo = tmp_path / "repo"
    (repo / "src" / "null_memory").mkdir(parents=True)
    (repo / "src" / "null_memory" / "migrate_v3.py").write_text(
        "UNIFIED_SCHEMA_VERSION = 21\n"
    )
    verdict, evidence = _verify_schema_version(
        "Schema version is 5", str(repo),
    )
    assert verdict == "refuted"
    assert "v5" in evidence and "v21" in evidence


def test_schema_version_verifier_passes_match(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "null_memory").mkdir(parents=True)
    (repo / "src" / "null_memory" / "migrate_v3.py").write_text(
        "UNIFIED_SCHEMA_VERSION = 21\n"
    )
    verdict, _ = _verify_schema_version("schema v21", str(repo))
    assert verdict == "verified"


# ── Stale detection ──────────────────────────────────────────────────


def test_claim_marked_stale_when_disappears_from_doc(tmp_path, conn):
    """First audit picks up a claim. Doc rewritten without it. Second
    audit marks the orphaned claim as stale."""
    doc = _make_doc(tmp_path, "x.md", "old content")
    # First pass: claim seen
    audit_doc(
        doc, conn,
        _llm_returning([{"text": "claim A", "type": "other"}]),
        repo_root=str(tmp_path),
    )
    # Second pass: doc no longer mentions claim A
    audit_doc(
        doc, conn,
        _llm_returning([{"text": "claim B", "type": "other"}]),
        repo_root=str(tmp_path),
    )
    rows = dict(conn.execute(
        "SELECT claim_text, status FROM doc_claims"
    ).fetchall())
    assert rows.get("claim A") == "stale"
    assert rows.get("claim B") in ("unverified", "verified", "refuted")


def test_refuted_claims_not_overwritten_by_stale(tmp_path, conn):
    """A claim that's been refuted in the past keeps that status even
    when stale-sweep would otherwise mark it stale."""
    doc = _make_doc(tmp_path, "x.md", "ph")
    audit_doc(
        doc, conn,
        _llm_returning([{"text": "src/missing.py", "type": "file_ref"}]),
        repo_root=str(tmp_path),
    )
    # Confirm refuted
    s = conn.execute("SELECT status FROM doc_claims").fetchone()[0]
    assert s == "refuted"
    # Second pass: doc no longer mentions it. Should NOT be downgraded
    # to stale — refuted is a stronger signal.
    audit_doc(
        doc, conn,
        _llm_returning([{"text": "completely different", "type": "other"}]),
        repo_root=str(tmp_path),
    )
    s2 = conn.execute(
        "SELECT status FROM doc_claims WHERE claim_text='src/missing.py'"
    ).fetchone()[0]
    assert s2 == "refuted"


# ── Verifier integration via audit_doc ───────────────────────────────


def test_full_audit_marks_refuted_via_file_ref(tmp_path, conn):
    """End-to-end: doc claims a file exists that doesn't → refuted."""
    doc = _make_doc(tmp_path, "CLAUDE.md", "see src/imaginary.py for details")
    audit_doc(
        doc, conn,
        _llm_returning([
            {"text": "src/imaginary.py", "type": "file_ref"},
        ]),
        repo_root=str(tmp_path),
    )
    row = conn.execute(
        "SELECT status, refute_evidence FROM doc_claims"
    ).fetchone()
    assert row[0] == "refuted"
    assert "imaginary.py" in row[1]


# ── Hypnos Stage 8 integration ───────────────────────────────────────


@pytest.fixture
def stage8_mem(tmp_path, monkeypatch):
    """AgentMemory + a fake repo dir with a couple of docs to scan."""
    from null_memory.agent import AgentMemory
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    repos = tmp_path / "Repos" / "demo"
    repos.mkdir(parents=True)
    (repos / "CLAUDE.md").write_text(
        "Phase 7.3 hasn't shipped yet.\nSchema version is 99.\n"
    )
    (repos / "ATLAS_HANDOFF_NEXT_SESSION.md").write_text(
        "src/null_memory/doesnt_exist.py is where the magic happens."
    )
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    yield mem, str(repos)


def test_stage8_audits_docs_and_records_refutes(stage8_mem):
    from null_memory.hypnos import Hypnos
    mem, repo_root = stage8_mem
    # Stub LLM returns claim sets matching whatever is asked
    def _stub(prompt: str) -> str:
        if "CLAUDE.md" in prompt:
            return json.dumps({"claims": [
                {"text": "Phase 7.3 hasn't shipped", "type": "ship_status"},
                {"text": "schema v99", "type": "schema_version"},
            ]})
        if "ATLAS_HANDOFF" in prompt:
            return json.dumps({"claims": [
                {"text": "src/null_memory/doesnt_exist.py",
                 "type": "file_ref"},
            ]})
        return json.dumps({"claims": []})
    # Live repo's migrate_v3.py is at the real null repo, not tmp_path.
    # The schema verifier will go unverified (no migrate_v3.py at fake
    # repo root). file_ref will refute. ship_status will be unverified
    # because there's no git log.
    mem.config["hypnos_doc_audit_roots"] = [str(__import__("os").path.dirname(repo_root))]
    mem.config["hypnos_doc_audit_llm"] = _stub
    h = Hypnos(mem)
    result = h.run(stages=[8])
    assert result.stage8_docs_audited == 2
    assert result.stage8_claims_extracted == 3
    assert result.stage8_claims_refuted >= 1  # the file_ref


def test_stage8_prunes_node_modules_and_finds_doc(stage8_mem):
    """Regression: glob.glob(recursive=True) walks node_modules/ trees
    before filtering, which on real laptops means tens of millions of
    files and effectively hangs. Stage 8 must use a pruned walk."""
    from null_memory.hypnos import Hypnos
    import os as _os
    mem, repo_root = stage8_mem
    # Drop a hostile node_modules tree with a CLAUDE.md inside that
    # would match the pattern but should NOT be audited.
    nm = _os.path.join(repo_root, "node_modules", "evil-pkg")
    _os.makedirs(nm)
    with open(_os.path.join(nm, "CLAUDE.md"), "w") as f:
        f.write("don't audit me")

    captured_prompts: list[str] = []
    def _stub(prompt: str) -> str:
        captured_prompts.append(prompt)
        return json.dumps({"claims": []})
    mem.config["hypnos_doc_audit_roots"] = [_os.path.dirname(repo_root)]
    mem.config["hypnos_doc_audit_llm"] = _stub
    h = Hypnos(mem)
    result = h.run(stages=[8])
    # node_modules/evil-pkg/CLAUDE.md must not be audited.
    assert all("evil-pkg" not in p for p in captured_prompts)
    # The two real docs from the fixture should still be audited.
    assert result.stage8_docs_audited == 2


def test_verify_claim_handler_uses_cached_refute(tmp_path, monkeypatch):
    """null_verify_claim should return cached refute evidence rather
    than re-running the live verifier when a row exists."""
    from null_memory.agent import AgentMemory
    from null_memory.mcp.handlers import NullHandlers
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    init_unified_db(str(tmp_path / "unified.db")).close()
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    mem.db.conn.execute(
        """INSERT INTO doc_claims
           (source_path, claim_text, claim_type, extracted_at,
            last_seen_at, last_verified_at, status, refute_evidence)
           VALUES (?, ?, 'ship_status', ?, ?, ?, 'refuted', ?)""",
        ("/x/CLAUDE.md", "Phase 7.3 hasn't shipped",
         "2026-04-29", "2026-04-29", "2026-04-29",
         "git log shows: 7ebb06e shipped"),
    )
    mem.db.conn.commit()
    handlers = NullHandlers(agent_dir=str(agent_dir))
    handlers._memory = mem
    out = handlers.handle_verify_claim("Phase 7.3 hasn't shipped")
    assert "cached" in out
    assert "refuted" in out
    assert "7ebb06e" in out


def test_verify_claim_handler_runs_live_verifier_when_no_cache(
    tmp_path, monkeypatch,
):
    """Falls back to ad-hoc verifier when nothing matches in doc_claims."""
    from null_memory.agent import AgentMemory
    from null_memory.mcp.handlers import NullHandlers
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    init_unified_db(str(tmp_path / "unified.db")).close()
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    handlers = NullHandlers(agent_dir=str(agent_dir))
    handlers._memory = mem
    out = handlers.handle_verify_claim(
        "src/null_memory/totally_imaginary_module.py", claim_type="file_ref",
    )
    assert "live-checked" in out
    assert "refuted" in out
    assert "imaginary" in out.lower()


def test_briefing_surfaces_refuted_claims(tmp_path, monkeypatch):
    """End-to-end: a refuted claim in doc_claims appears in briefing()."""
    from null_memory.agent import AgentMemory
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    init_unified_db(str(tmp_path / "unified.db")).close()
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    # Seed a refuted claim directly.
    mem.db.conn.execute(
        """INSERT INTO doc_claims
           (source_path, claim_text, claim_type, extracted_at,
            last_seen_at, last_verified_at, status, refute_evidence)
           VALUES (?, ?, 'ship_status', ?, ?, ?, 'refuted', ?)""",
        ("/Users/x/CLAUDE.md", "Phase 7.3 hasn't shipped",
         "2026-04-29T00:00:00", "2026-04-29T00:00:00",
         "2026-04-29T00:00:00",
         "git log shows: 7ebb06e feat(phase 7) Nebula trigger panel"),
    )
    # Seed a stale one too.
    mem.db.conn.execute(
        """INSERT INTO doc_claims
           (source_path, claim_text, claim_type, extracted_at,
            last_seen_at, status)
           VALUES ('/x.md', 'orphan claim', 'other',
                   '2026-04-01', '2026-04-01', 'stale')"""
    )
    mem.db.conn.commit()
    out = mem.briefing()
    assert "Doc-claim refutations" in out
    assert "Phase 7.3" in out
    assert "7ebb06e" in out
    assert "1 stale doc claims" in out


def test_stage8_dry_run_writes_nothing(stage8_mem):
    from null_memory.hypnos import Hypnos
    mem, repo_root = stage8_mem
    mem.config["hypnos_doc_audit_dryrun"] = True
    mem.config["hypnos_doc_audit_roots"] = [str(__import__("os").path.dirname(repo_root))]
    mem.config["hypnos_doc_audit_llm"] = lambda _p: '{"claims": []}'
    h = Hypnos(mem)
    result = h.run(stages=[8])
    assert result.stage8_docs_audited == 0
    n = mem.db.conn.execute(
        "SELECT COUNT(*) FROM doc_claims"
    ).fetchone()[0]
    assert n == 0
