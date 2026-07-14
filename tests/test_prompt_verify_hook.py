"""Tests for the UserPromptSubmit verify hook (doc_claims cache +
Phase 2 live verifiers).

The hook lives outside the package as a standalone script. We import it
by path to test its functions directly. Each test isolates one path:

  · _extract_hints       — pattern matcher on prompt text
  · _query_claims        — doc_claims cache lookup
  · _run_live_verifiers  — Phase 2 dispatcher; runs only verifiers
                           whose probe matches the prompt
  · main()               — end-to-end emit shape
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from null_memory.migrate_v3 import init_unified_db


SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "null-prompt-verify-hook.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "null_prompt_verify", SCRIPT_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def repo_root_with_phase_commit(tmp_path):
    """Tiny git repo that contains a 'phase 5' commit so ship_status
    verifier finds something."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "null_memory").mkdir()
    (root / "src" / "null_memory" / "migrate_v3.py").write_text(
        "UNIFIED_SCHEMA_VERSION = 21\n"
    )
    (root / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat(phase 5): ship core feature"],
        cwd=root, check=True,
    )
    return str(root)


@pytest.fixture
def mod():
    return _load_module()


# ── _extract_hints (existing behavior) ──────────────────────────────


def test_extract_hints_phase_question(mod):
    hints = mod._extract_hints("did Phase 7.3 ship yet?")
    assert any("phase 7.3" in h.lower() for h in hints)


def test_extract_hints_schema_version(mod):
    hints = mod._extract_hints("what's the current schema version?")
    assert any("schema" in h.lower() for h in hints)


def test_extract_hints_silent_on_conversational(mod):
    hints = mod._extract_hints("what should we do for lunch")
    assert hints == []


# ── _run_live_verifiers (Phase 2 — the new path) ────────────────────


def test_live_verifiers_skip_when_no_probe_matches(mod, repo_root_with_phase_commit):
    """Conversational prompts must skip ALL verifier work — latency
    on chat turns must be ~0."""
    results = mod._run_live_verifiers(
        "what should we do for lunch", repo_root_with_phase_commit,
    )
    assert results == []


def test_live_verifiers_ship_status_verified(mod, repo_root_with_phase_commit):
    """Prompt asks about a phase that git log mentions → verified."""
    results = mod._run_live_verifiers(
        "did phase 5 ship?", repo_root_with_phase_commit,
    )
    assert any(
        ctype == "ship_status" and verdict == "verified"
        for ctype, verdict, _ in results
    )


def test_live_verifiers_schema_version_refuted(mod, repo_root_with_phase_commit):
    """Doc says schema v99 but live is v21 → refuted with evidence."""
    results = mod._run_live_verifiers(
        "schema version is 99 right now", repo_root_with_phase_commit,
    )
    refuted = [
        (ctype, verdict, ev) for ctype, verdict, ev in results
        if ctype == "schema_version" and verdict == "refuted"
    ]
    assert refuted, f"expected refuted schema, got {results}"
    assert "21" in (refuted[0][2] or "")


def test_live_verifiers_file_ref_refuted(mod, repo_root_with_phase_commit):
    """Reference to a file that doesn't exist → refuted."""
    results = mod._run_live_verifiers(
        "is src/null_memory/imaginary.py the right path?",
        repo_root_with_phase_commit,
    )
    assert any(
        ctype == "file_ref" and verdict == "refuted"
        for ctype, verdict, _ in results
    )


def test_live_verifiers_capped_at_max(mod, repo_root_with_phase_commit):
    """A prompt that triggers all 4 probes still emits at most
    MAX_LIVE_VERIFICATIONS results."""
    prompt = (
        "did phase 5 ship and is schema version 99 and "
        "is src/null_memory/imaginary.py at line 1 and is foo() located"
    )
    results = mod._run_live_verifiers(
        prompt, repo_root_with_phase_commit,
    )
    assert len(results) <= mod.MAX_LIVE_VERIFICATIONS


# ── main() end-to-end ───────────────────────────────────────────────


def _run_main(mod, payload: dict) -> str:
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            mod.main()
    finally:
        sys.stdin = old_stdin
    return buf.getvalue()


def test_main_silent_on_conversational(mod):
    """No verifiable pattern, no doc_claims hit → empty output."""
    out = _run_main(mod, {"prompt": "tell me a story about cats"})
    assert out == ""


def test_main_silent_on_empty_prompt(mod):
    out = _run_main(mod, {"prompt": ""})
    assert out == ""


def test_main_emits_live_block_for_verifiable_prompt(
    mod, repo_root_with_phase_commit, monkeypatch,
):
    """When the prompt asks a live-state question, the verify block
    must appear in stdout — even with no doc_claims cache match."""
    monkeypatch.setattr(mod, "DEFAULT_REPO_ROOT", repo_root_with_phase_commit)
    out = _run_main(mod, {"prompt": "did phase 5 ship yet?"})
    assert "[Null verify]" in out
    assert "ship_status" in out


def test_main_silent_on_garbage_stdin(mod):
    """Hook must never crash or emit when stdin isn't valid JSON."""
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("definitely not json {{{")
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = mod.main()
    finally:
        sys.stdin = old_stdin
    assert rc == 0
    assert buf.getvalue() == ""
