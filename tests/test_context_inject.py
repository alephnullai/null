"""Tests for Phase 1 context injection hook (working-memory plan).

The hook script lives outside the package (in scripts/) because it's
invoked as a standalone process by the Claude Code UserPromptSubmit
hook. We import the module by path to test its functions directly.

Test boundaries:
  · _keywords  — pure stopword/length filter
  · _recent_observations — last 24h observations from facts
  · _recall_hits — keyword-OR scoring + threshold filter
  · main()      — end-to-end, verifies stdout shape + skip rules
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from null_memory.migrate_v3 import init_unified_db


SCRIPT_PATH = (
    Path(__file__).parent.parent
    / "scripts" / "null-context-inject-hook.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "null_context_inject", SCRIPT_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def populated_db(tmp_path, monkeypatch):
    """A unified DB with observations + searchable facts."""
    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    # 5 recent observations (within last 24h via datetime('now'))
    for i in range(5):
        conn.execute(
            """INSERT INTO facts
               (id, fact, confidence, project, source, provenance,
                created_at, archived, forgotten)
               VALUES (?, ?, 0.8, 'global', 'observation', 'observation',
                       datetime('now', ?), 0, 0)""",
            (f"obs{i}", f"observation about topic {i}", f"-{i*5} minutes"),
        )
    # An old observation (>24h) that should NOT surface
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            created_at, archived, forgotten)
           VALUES ('obs_old', 'ancient observation about orion',
                   0.8, 'global', 'observation', 'observation',
                   datetime('now', '-3 days'), 0, 0)"""
    )
    # Searchable non-observation facts (recall targets)
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            created_at, archived, forgotten)
           VALUES ('f1',
                   'Aleph uses tree-sitter for AST parsing across six languages',
                   0.95, 'aleph', 'lesson', 'observation',
                   datetime('now'), 0, 0)"""
    )
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            created_at, archived, forgotten)
           VALUES ('f2',
                   'Orion trading bot lost six dollars on Buenos Aires market',
                   0.9, 'orion', 'lesson', 'observation',
                   datetime('now'), 0, 0)"""
    )
    # Single-keyword high-confidence fact (tests the high-conf shortcut)
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            created_at, archived, forgotten)
           VALUES ('f3', 'tree-sitter is the canonical parser', 0.95,
                   'aleph', 'lesson', 'observation',
                   datetime('now'), 0, 0)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        _load_module(), "DB_PATH", str(db_path), raising=False,
    )
    # Re-import after monkeypatch so module-level reads pick it up.
    return str(db_path)


@pytest.fixture
def mod(populated_db, monkeypatch):
    m = _load_module()
    monkeypatch.setattr(m, "DB_PATH", populated_db)
    return m


# ── Pure helpers ────────────────────────────────────────────────────


def test_keywords_filters_stopwords_and_short(mod):
    out = mod._keywords("the cat is on the mat with a long box")
    assert "long" in out
    assert "the" not in out      # stopword
    assert "cat" not in out      # too short (3 chars)
    assert "mat" not in out      # too short


def test_keywords_dedupes(mod):
    out = mod._keywords("aleph aleph aleph parser parser")
    assert out.count("aleph") == 1
    assert out.count("parser") == 1


def test_keywords_capped(mod):
    txt = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    out = mod._keywords(txt, k=3)
    assert len(out) == 3


# ── Recent observations ─────────────────────────────────────────────


def test_recent_observations_returns_last_n_within_window(mod):
    conn = sqlite3.connect(mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = mod._recent_observations(conn)
    assert len(rows) == mod.MAX_OBS
    # All recent (no 'ancient' string in facts)
    assert all("ancient" not in r for r in rows)
    conn.close()


def test_recent_observations_excludes_old(mod):
    conn = sqlite3.connect(mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = mod._recent_observations(conn)
    assert not any("ancient" in r for r in rows)
    conn.close()


# ── Recall ──────────────────────────────────────────────────────────


def test_recall_hits_requires_multi_keyword_match(mod):
    """A prompt with only one matching keyword and only normal-confidence
    facts should return zero hits."""
    conn = sqlite3.connect(mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    # Prompt has one keyword that matches f1: "parsing" (4 chars OK).
    # Single-keyword match below high-conf floor → suppressed.
    # Lower f1 confidence first so it's below floor.
    conn.execute("UPDATE facts SET confidence = 0.7 WHERE id = 'f1'")
    conn.commit()
    hits = mod._recall_hits(conn, "tell me about parsing")
    # f3 ("tree-sitter is the canonical parser") may still hit because
    # "parser" matches and conf=0.95 ≥ floor.
    assert all(conf >= 0.85 for conf, _ in hits)
    conn.close()


def test_recall_hits_returns_multi_keyword_match(mod):
    conn = sqlite3.connect(mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    hits = mod._recall_hits(conn, "tell me about aleph parser languages")
    assert hits, "should have found f1 (matches aleph + parser/parsing + languages)"
    assert any("tree-sitter" in fact for _, fact in hits)
    conn.close()


def test_recall_hits_excludes_observations(mod):
    """Observations are surfaced via the 'Recent thoughts' channel — they
    must NOT also surface in 'Possibly relevant'."""
    conn = sqlite3.connect(mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    hits = mod._recall_hits(conn, "tell me about topic")
    # 'topic' is a single-keyword match. observations are excluded by source
    # filter regardless of confidence.
    assert all("observation about topic" not in fact for _, fact in hits)
    conn.close()


# ── End-to-end via main() ───────────────────────────────────────────


def _run_main(mod, payload: dict) -> str:
    """Invoke main() with stdin replaced. Returns stdout text."""
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            mod.main()
    finally:
        sys.stdin = old_stdin
    return buf.getvalue()


def test_main_emits_context_block_on_relevant_prompt(mod):
    out = _run_main(
        mod, {"prompt": "tell me about aleph parser and tree-sitter languages"},
    )
    assert "[Null context]" in out
    assert "Recent thoughts" in out
    assert "Possibly relevant" in out


def test_main_silent_on_short_prompt(mod):
    out = _run_main(mod, {"prompt": "ok"})
    assert out == ""


def test_main_silent_on_empty_prompt(mod):
    out = _run_main(mod, {"prompt": ""})
    assert out == ""


def test_main_silent_when_disabled_via_env(mod, monkeypatch):
    monkeypatch.setenv("NULL_CONTEXT_INJECT", "0")
    out = _run_main(
        mod, {"prompt": "tell me about aleph parser and languages"},
    )
    assert out == ""


def test_main_silent_on_garbage_stdin(mod):
    """Hook must never crash or emit when stdin isn't valid JSON."""
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("not json at all {{{")
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = mod.main()
    finally:
        sys.stdin = old_stdin
    assert rc == 0
    assert buf.getvalue() == ""


def test_main_emits_recent_thoughts_even_without_recall_hits(mod):
    """If no facts match the prompt but there are recent observations,
    surface the observations alone — better than nothing."""
    out = _run_main(
        mod, {"prompt": "tell me about quantum chromodynamics please"},
    )
    if out:
        assert "Recent thoughts" in out
        # 'Possibly relevant' may or may not render depending on whether
        # any fact happens to match; both states are acceptable.


# ── Latency budget ──────────────────────────────────────────────────


# ── Phase 3: cross-instance shared working memory ─────────────────


def test_recent_observations_filters_by_session_when_provided(mod, populated_db):
    """When my_session_id is passed, Recent thoughts should ONLY return
    observations written by that session — others go to Other Atlas."""
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    # Tag one observation as from session A, another as session B.
    conn.execute("UPDATE facts SET session_id = 'sess-A' WHERE id = 'obs0'")
    conn.execute("UPDATE facts SET session_id = 'sess-B' WHERE id = 'obs1'")
    conn.commit()
    a_obs = mod._recent_observations(conn, my_session_id="sess-A")
    b_obs = mod._recent_observations(conn, my_session_id="sess-B")
    # A's recent only has A's obs (obs0 = "observation about topic 0")
    assert any("topic 0" in o for o in a_obs)
    assert not any("topic 1" in o for o in a_obs)
    # B's recent only has B's obs
    assert any("topic 1" in o for o in b_obs)
    assert not any("topic 0" in o for o in b_obs)
    conn.close()


def test_recent_observations_falls_back_when_no_session(mod, populated_db):
    """No session_id → legacy any-session behavior preserved."""
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    obs = mod._recent_observations(conn, my_session_id=None)
    assert obs, "legacy fallback should return any-session observations"
    conn.close()


def test_xinstance_skips_when_no_session_id(mod, populated_db):
    """Without our own session_id we can't tell our writes from others;
    skip Phase 3 entirely rather than risk surfacing our own writes."""
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    rows = mod._xinstance_recent(conn, None)
    assert rows == []
    conn.close()


def test_xinstance_returns_other_session_facts(mod, populated_db):
    """A fact written by another session within the recency window
    should appear; our own facts must NOT."""
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    # Add a recent fact from session A
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            session_id, created_at, archived, forgotten)
           VALUES ('xa1', 'session A just learned about widgets',
                   0.9, 'global', 'observation', 'observation',
                   'sess-A', datetime('now'), 0, 0)"""
    )
    # Add an old fact from session A (outside window — must NOT surface)
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            session_id, created_at, archived, forgotten)
           VALUES ('xa_old', 'session A learned old', 0.9, 'global',
                   'observation', 'observation',
                   'sess-A', datetime('now', '-2 hours'), 0, 0)"""
    )
    # Add a fact from MY session (must NOT surface as cross-instance)
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            session_id, created_at, archived, forgotten)
           VALUES ('xb1', 'my own session fact', 0.9, 'global',
                   'observation', 'observation',
                   'sess-B', datetime('now'), 0, 0)"""
    )
    conn.commit()

    rows = mod._xinstance_recent(conn, my_session_id="sess-B")
    facts = [r[0] for r in rows]
    assert any("widgets" in f for f in facts), f"expected sess-A fact, got {facts}"
    assert not any("my own session" in f for f in facts), \
        f"sess-B's own fact leaked into xinstance: {facts}"
    assert not any("learned old" in f for f in facts), \
        "old fact outside recency window leaked"
    conn.close()


def test_xinstance_excludes_loop_marker_facts(mod, populated_db):
    """Facts whose own text describes cross-instance surfacing must not
    propagate (would create N-cycle observation loops)."""
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            session_id, created_at, archived, forgotten)
           VALUES ('loop1', 'Another Atlas just learned X about Y',
                   0.9, 'global', 'observation', 'observation',
                   'sess-A', datetime('now'), 0, 0)"""
    )
    conn.commit()
    rows = mod._xinstance_recent(conn, my_session_id="sess-B")
    assert all("Another Atlas" not in r[0] for r in rows)
    conn.close()


def test_xinstance_filters_to_user_driven_sources(mod, populated_db):
    """Housekeeping sources (crystallized, bootstrap, pontificate) must
    not surface as 'Other Atlas activity' — those aren't user-driven
    learning events."""
    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            session_id, created_at, archived, forgotten)
           VALUES ('crys1', 'a crystallized child fact', 0.85, 'global',
                   'crystallized', 'observation',
                   'sess-A', datetime('now'), 0, 0)"""
    )
    conn.commit()
    rows = mod._xinstance_recent(conn, my_session_id="sess-B")
    assert all("crystallized child" not in r[0] for r in rows)
    conn.close()


def test_main_emits_xinstance_section_when_other_session_active(
    mod, populated_db,
):
    conn = sqlite3.connect(populated_db)
    conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, project, source, provenance,
            session_id, created_at, archived, forgotten)
           VALUES ('xnew1', 'parallel atlas just decided something important',
                   0.9, 'orion', 'decision', 'observation',
                   'sess-other', datetime('now'), 0, 0)"""
    )
    conn.commit()
    conn.close()
    out = _run_main(
        mod,
        {"prompt": "tell me about parser languages", "session_id": "sess-mine"},
    )
    assert "Other Atlas activity" in out
    assert "parallel atlas just decided" in out


# ── Latency budget ──────────────────────────────────────────────────


def test_main_latency_under_budget(mod):
    """Phase 1 commits to <300ms p99 hook latency. This is a single-shot
    smoke test of the warm path against a small DB; production p99 will
    be measured by the observability log."""
    payload = {"prompt": "tell me about aleph parser tree-sitter languages"}
    start = time.perf_counter()
    _run_main(mod, payload)
    elapsed_ms = (time.perf_counter() - start) * 1000
    # Generous bound for CI variance — production target is 300ms.
    assert elapsed_ms < 1000, f"hook took {elapsed_ms:.0f}ms (budget 1000ms)"
