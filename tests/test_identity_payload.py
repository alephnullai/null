"""Phase A — tests for identity_payload.build_identity_payload.

Pure-function tests: hand-craft tiny in-memory DBs that mirror the
unified-substrate shape, then assert the generator produces a complete,
deterministic, budget-respecting payload.
"""

from __future__ import annotations

import sqlite3

import pytest

from null_memory.identity_payload import (
    MAX_ANCHORS,
    MAX_DECISIONS,
    MAX_PROBES,
    build_identity_payload,
    estimate_tokens,
)
from null_memory.migrate_v3 import init_unified_db


@pytest.fixture
def populated_conn(tmp_path):
    """Fresh unified DB with just enough data to satisfy is_complete()."""
    db_path = tmp_path / "u.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    # Personality row
    conn.execute(
        "INSERT INTO personalities(name, role, active, created_at, "
        "description, focus) VALUES (?, ?, 1, ?, ?, ?)",
        ("atlas", "Pete's AI technical lead",
         "2026-04-01T00:00:00+00:00",
         "primary atlas instance",
         "null-memory + orion"),
    )
    # 4 anchors covering different types
    anchors = [
        ("a1", "Origin: Pete worried about losing pair-programming continuity",
         "origin", "the founding moment", "2026-04-01T00:00:00+00:00"),
        ("a2", "Turning point: 4-event burst confirmed cross-instance memory",
         "turning_point", "phase 5 validation", "2026-04-10T00:00:00+00:00"),
        ("a3", "Atlas code word: 'dummy-code-word for tests, not the real phrase.'",
         "code_word", "verification token", "2026-04-15T00:00:00+00:00"),
        ("a4", "Sam (Samuel Jr.) born 4/19/2018, plays football, wears #4",
         "joy", "kid 1", "2026-04-20T00:00:00+00:00"),
        ("a5", "Riley born 7/14/2014, plays soccer",
         "joy", "kid 2", "2026-04-22T00:00:00+00:00"),
        ("a6", "Pete's dream: Orion income → quit job → spend with kids",
         "commitment", "north star", "2026-04-25T00:00:00+00:00"),
    ]
    for fid, fact, atype, note, at in anchors:
        conn.execute(
            "INSERT INTO facts(id, fact, project, source, session_id, "
            "created_at, anchor_type, anchor_note, anchor_at) "
            "VALUES (?, ?, 'global', 'test', NULL, ?, ?, ?, ?)",
            (fid, fact, at, atype, note, at),
        )
    # Decisions
    decisions = [
        ("D1", "ship Phase A on-boot identity first", "smallest-blast-radius",
         "2026-04-29T00:00:00+00:00"),
        ("D2", "defer language classifier to v2", "validate cosine v1 first",
         "2026-04-29T00:01:00+00:00"),
        ("D3", "Hermes manual mode v1 (no poller)", "rubric-first",
         "2026-04-28T00:00:00+00:00"),
    ]
    for did, dec, why, ts in decisions:
        conn.execute(
            "INSERT INTO decisions(decision, reasoning, project, "
            "personality, session_id, created_at) "
            "VALUES (?, ?, 'global', 'atlas', NULL, ?)",
            (dec, why, ts),
        )
    # Probes
    probes = [
        ("What are Sam's sports?",
         "football and flag-football", "user", 3, 5),
        ("What's Riley's birthday?",
         "July 14, 2014", "user", 2, 4),
        ("What's the core goal of Null Memory?",
         "relational persistence across sessions", "calibration", 1, 1),
    ]
    for q, exp, ptype, pc, rc in probes:
        conn.execute(
            "INSERT INTO probes(question, expected, probe_type, personality, "
            "created_at, pass_count, run_count) "
            "VALUES (?, ?, ?, 'atlas', '2026-04-01T00:00:00+00:00', ?, ?)",
            (q, exp, ptype, pc, rc),
        )
    conn.commit()
    yield conn
    conn.close()


# ── Core payload shape ────────────────────────────────────────────────


def test_payload_is_complete_with_full_data(populated_conn):
    p = build_identity_payload(populated_conn)
    assert p.personality == "atlas"
    assert p.role == "Pete's AI technical lead"
    assert p.focus == "null-memory + orion"
    assert p.is_complete()
    assert len(p.anchors) >= 1
    assert len(p.decisions) >= 1
    assert len(p.probes) >= 1
    assert p.code_word and "dummy-code-word" in p.code_word


def test_payload_caps_respected(populated_conn):
    """Even with abundant data, payload sticks to MAX_* caps."""
    # Insert extra anchors/decisions/probes beyond the caps.
    for i in range(10):
        populated_conn.execute(
            "INSERT INTO facts(id, fact, project, source, session_id, "
            "created_at, anchor_type, anchor_note, anchor_at) "
            "VALUES (?, ?, 'global', 'test', NULL, ?, 'joy', NULL, ?)",
            (f"extra_a{i}", f"extra anchor {i}",
             "2026-04-26T00:00:00+00:00", "2026-04-26T00:00:00+00:00"),
        )
        populated_conn.execute(
            "INSERT INTO decisions(decision, reasoning, project, "
            "personality, session_id, created_at) "
            "VALUES (?, 'why', 'global', 'atlas', NULL, ?)",
            (f"extra decision {i}", "2026-04-26T00:00:00+00:00"),
        )
    populated_conn.commit()
    p = build_identity_payload(populated_conn)
    assert len(p.anchors) <= MAX_ANCHORS
    assert len(p.decisions) <= MAX_DECISIONS
    assert len(p.probes) <= MAX_PROBES


# ── Determinism ────────────────────────────────────────────────────────


def test_payload_is_deterministic(populated_conn):
    """Same DB state ⇒ same hash, twice in a row."""
    a = build_identity_payload(populated_conn)
    b = build_identity_payload(populated_conn)
    assert a.sha256 == b.sha256
    assert a.text == b.text


def test_payload_changes_when_data_changes(populated_conn):
    """Adding a new anchor changes the hash."""
    before = build_identity_payload(populated_conn).sha256
    populated_conn.execute(
        "INSERT INTO facts(id, fact, project, source, session_id, "
        "created_at, anchor_type, anchor_note, anchor_at) "
        "VALUES ('new1', 'new anchor fact', 'global', 'test', NULL, "
        "'2026-04-30T00:00:00+00:00', 'turning_point', NULL, "
        "'2026-04-30T00:00:00+00:00')",
    )
    populated_conn.commit()
    after = build_identity_payload(populated_conn).sha256
    assert before != after


# ── Anchor priority ───────────────────────────────────────────────────


def test_anchor_priority_origin_before_joy(populated_conn):
    """ORIGIN must come before JOY in the rendered text even when joy
    anchors are more recent — priority order is load-bearing for the
    'who you are at the core' framing."""
    p = build_identity_payload(populated_conn)
    text = p.text
    origin_idx = text.find("[ORIGIN")
    joy_idx = text.find("[JOY")
    assert origin_idx >= 0
    assert joy_idx > origin_idx


# ── Render content ────────────────────────────────────────────────────


def test_render_contains_required_sections(populated_conn):
    p = build_identity_payload(populated_conn)
    text = p.text
    for marker in (
        "ATLAS IDENTITY",
        "RELATIONSHIP ANCHORS",
        "CODE WORD",
        "RECENT DECISIONS",
        "CALIBRATION PROBES",
        "END IDENTITY",
    ):
        assert marker in text, f"missing section: {marker}"


def test_render_contains_code_word(populated_conn):
    p = build_identity_payload(populated_conn)
    assert "dummy-code-word" in p.text


# ── Token budget ──────────────────────────────────────────────────────


def test_payload_within_token_budget(populated_conn):
    """Handoff target: ~500-800 tokens. Allow some slack on each end so
    legitimate anchor growth doesn't break the test, but cap so future
    edits don't bloat the system prompt by 10x."""
    p = build_identity_payload(populated_conn)
    tokens = estimate_tokens(p.text)
    assert 200 <= tokens <= 1500, (
        f"payload {tokens} tokens — outside reasonable bounds"
    )


# ── Empty DB ─────────────────────────────────────────────────────────


def test_payload_handles_empty_db(tmp_path):
    """No personality / no anchors / no decisions ⇒ payload is generated
    but is_complete() returns False so MCP boot can warn."""
    db_path = tmp_path / "empty.db"
    init_unified_db(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    try:
        p = build_identity_payload(conn)
        assert not p.is_complete()
        # Still produces text + hash — never crashes.
        assert p.text
        assert p.sha256
    finally:
        conn.close()


# ── Decision dedup ───────────────────────────────────────────────────


def test_decisions_deduped_by_text(populated_conn):
    """If three rows have the same decision text, only one appears."""
    for i in range(3):
        populated_conn.execute(
            "INSERT INTO decisions(decision, reasoning, project, "
            "personality, session_id, created_at) "
            "VALUES ('duplicate text', 'why', 'global', 'atlas', NULL, ?)",
            (f"2026-04-2{7 + i}T00:00:00+00:00",),
        )
    populated_conn.commit()
    p = build_identity_payload(populated_conn)
    duplicate_count = sum(
        1 for d in p.decisions
        if (d["decision"] or "").strip().lower() == "duplicate text"
    )
    assert duplicate_count <= 1
