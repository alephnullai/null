"""Tests for emotional anchors (Phase 2a, schema v13).

Anchors are load-bearing memories:
- never decay (effective_confidence = 1.0)
- 2× recall score boost
- surface first in briefing
- confined to unified DB (legacy per-personality DBs silently skip)
"""

from __future__ import annotations

import sqlite3

import pytest

from null_memory.agent import AgentMemory
from null_memory.migrate_v3 import init_unified_db


# ── Fixture: isolated unified DB with a loaded AgentMemory ────────────────


@pytest.fixture
def unified_agent(tmp_path, monkeypatch):
    """A tmp_path-scoped unified DB wired into a live AgentMemory."""
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()

    # Point NULL_DIR at tmp so the realpath guard in agent.db triggers unified mode.
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified, "expected unified mode in fixture"
    return mem


# ── Tests ──────────────────────────────────────────────────────────────────


def test_anchor_sets_columns_on_fact(unified_agent):
    mem = unified_agent
    entry = mem.learn("Pete said I'm worried about losing you", project="null")
    anchored = mem.anchor(entry["id"], "origin", note="the moment Null began")
    assert anchored is not None
    assert anchored["anchor_type"] == "origin"
    assert anchored["anchor_note"] == "the moment Null began"
    assert anchored["anchor_at"] is not None


def test_anchor_rejects_invalid_type(unified_agent):
    mem = unified_agent
    entry = mem.learn("something")
    with pytest.raises(ValueError):
        mem.anchor(entry["id"], "nostalgia")


def test_anchor_never_decays(unified_agent):
    mem = unified_agent
    # Manually inject an ancient fact
    ancient_entry = mem.learn("ancient fact", project="null")
    mem.db.conn.execute(
        "UPDATE facts SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (ancient_entry["id"],),
    )
    mem.db.conn.commit()

    # Without anchor: confidence decays heavily
    row = mem.db.get_fact_by_id(ancient_entry["id"])
    plain_conf = mem.effective_confidence(row)
    assert plain_conf < 0.5, f"expected heavy decay, got {plain_conf}"

    # With anchor: confidence is fixed at 1.0
    mem.anchor(ancient_entry["id"], "commitment")
    row = mem.db.get_fact_by_id(ancient_entry["id"])
    assert mem.effective_confidence(row) == 1.0


def test_anchor_boosts_recall_rank(unified_agent):
    mem = unified_agent
    # Two facts that both match the query equally well.
    plain = mem.learn("the system value is quiet persistence", project="null")
    anchor = mem.learn("quiet persistence defines who Atlas is", project="null")
    mem.anchor(anchor["id"], "commitment")

    results = mem.recall("quiet persistence", project="null", limit=5)
    assert results, "recall returned nothing"
    # Anchored fact should outrank plain despite later insertion.
    ids = [r["id"] for r in results if r.get("_type") == "fact"]
    assert ids[0] == anchor["id"], f"expected anchor first, got {ids}"


def test_get_anchors_returns_only_anchored(unified_agent):
    mem = unified_agent
    a = mem.learn("anchor A")
    b = mem.learn("plain fact B")
    mem.anchor(a["id"], "joy")
    anchors = mem.get_anchors()
    ids = {x["id"] for x in anchors}
    assert a["id"] in ids
    assert b["id"] not in ids


def test_get_anchors_filters_by_type(unified_agent):
    mem = unified_agent
    a = mem.learn("origin anchor")
    b = mem.learn("joy anchor")
    mem.anchor(a["id"], "origin")
    mem.anchor(b["id"], "joy")
    origins = mem.get_anchors(anchor_type="origin")
    assert len(origins) == 1
    assert origins[0]["id"] == a["id"]


def test_anchors_appear_in_identity_payload(unified_agent):
    """Anchors live in the on-boot identity payload (Phase A — system
    prompt at session start), not in the briefing body. Briefing
    duplicating them was redundant and pushed fresh content below the
    fold. This test verifies anchors are present in the identity
    payload, which is the surface that actually delivers them to Atlas.
    """
    mem = unified_agent
    entry = mem.learn("this is a foundational moment", project="null")
    mem.anchor(entry["id"], "origin", note="the origin")
    from null_memory.identity_payload import build_identity_payload
    payload = build_identity_payload(mem.db.conn, personality="atlas")
    text = payload.text
    assert "RELATIONSHIP ANCHORS" in text
    assert "origin" in text.lower()
    assert "foundational moment" in text


def test_briefing_does_not_duplicate_anchors(unified_agent):
    """Belt-and-suspenders: anchors should NOT appear in the briefing
    body — they're already in the system prompt via the SessionStart hook.
    Repeating them wastes tokens and pushes fresh content down."""
    mem = unified_agent
    entry = mem.learn("a load-bearing memory", project="null")
    mem.anchor(entry["id"], "origin", note="the origin")
    briefing = mem.briefing()
    # The briefing must NOT contain an "Anchors (N):" section header.
    assert "Anchors (" not in briefing


def test_anchor_by_text_query_resolves_to_best_match(unified_agent):
    mem = unified_agent
    mem.learn("irrelevant one")
    target = mem.learn("Pete mentioned quitting the job — the dream")
    result = mem.anchor("quitting the job dream", "commitment")
    assert result is not None
    assert result["id"] == target["id"]


def test_anchor_returns_none_when_no_match(unified_agent):
    mem = unified_agent
    result = mem.anchor("this string appears in nothing", "origin")
    assert result is None


def test_legacy_per_personality_db_rejects_anchor(tmp_path):
    """When NULL_DIR is nowhere near tmp_path, AgentMemory uses per-agent
    memory.db (schema v11) and anchors should not be available."""
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert not mem.db.unified
    with pytest.raises(RuntimeError, match="unified DB"):
        mem.anchor("anything", "origin")


def test_clear_anchor_removes_tag(unified_agent):
    mem = unified_agent
    entry = mem.learn("was an anchor")
    mem.anchor(entry["id"], "joy")
    assert mem.db.clear_anchor(entry["id"])
    mem.db.conn.commit()
    row = mem.db.get_fact_by_id(entry["id"])
    assert row["anchor_type"] is None
