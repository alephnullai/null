"""Issue #19 — briefing/read-surface personality scoping on unified stores.

Multiworker-in-a-box: two personalities (atlas + athena) share one
machine and one unified store. Personality-attributed rows (decisions,
mistakes, reflections, hypnos_journal, probes, fingerprints, feed) must
never leak across briefings; facts remain the shared knowledge plane
within the store's single trust domain (see briefing_render module
docstring for the full split).

Also pins the legacy behavior: per-personality stores (no personality
column on old schemas) keep working — the scoping predicate collapses
to nothing there.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from null_memory.agent import AgentMemory
from null_memory.memory.briefing_render import render_hypnos_section
from null_memory.migrate_v3 import init_unified_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Fixture: one unified store, two live personalities ────────────────────


@pytest.fixture
def two_personalities(tmp_path, monkeypatch):
    """Unified DB shared by an atlas and an athena AgentMemory, with
    interleaved decisions/mistakes/hypnos rows plus one shared fact."""
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))

    atlas_dir = tmp_path / "atlas"
    atlas_dir.mkdir()
    athena_dir = tmp_path / "personalities" / "athena"
    athena_dir.mkdir(parents=True)

    atlas = AgentMemory.load(agent_dir=str(atlas_dir), personality="atlas")
    athena = AgentMemory.load(agent_dir=str(athena_dir), personality="athena")
    assert atlas.db.unified and athena.db.unified

    # Interleaved attributed rows (sentinel strings, today's timestamps so
    # they land in the HOT blocks). Each personality's batch commits before
    # the other writes — two live connections share one WAL store.
    atlas.db.insert_decision(
        {"decision": "ATLAS-DEC-ALPHA ship the unified scoper",
         "reasoning": "issue 19", "created_at": _now()})
    atlas.db.insert_mistake(
        {"mistake": "ATLAS-MIST-ALPHA trusted a stale handoff",
         "why": "doc drift", "created_at": _now()})
    # Hypnos journal: one batch run, archived actions.
    for _ in range(2):
        atlas.db.insert_hypnos_entry("run-atlas-1", "decay", "archived")
    # Shared knowledge plane: a fact learned by atlas.
    atlas.learn("SHARED-FACT the box uses one unified store", project="null")
    atlas.db.conn.commit()

    athena.db.insert_decision(
        {"decision": "ATHENA-DEC-OMEGA refit the hiwave seat",
         "reasoning": "org design", "created_at": _now()})
    athena.db.insert_mistake(
        {"mistake": "ATHENA-MIST-OMEGA over-rotated on polish",
         "why": "sprint pressure", "created_at": _now()})
    # Hypnos journal: one batch run, promoted actions.
    for _ in range(3):
        athena.db.insert_hypnos_entry("run-athena-1", "promote", "promoted")
    athena.db.conn.commit()

    return atlas, athena


# ── Briefing isolation ─────────────────────────────────────────────────────


def test_briefing_contains_only_own_decisions(two_personalities):
    atlas, athena = two_personalities
    atlas_brief = atlas.briefing()
    athena_brief = athena.briefing()

    assert "ATLAS-DEC-ALPHA" in atlas_brief
    assert "ATHENA-DEC-OMEGA" not in atlas_brief
    assert "ATHENA-DEC-OMEGA" in athena_brief
    assert "ATLAS-DEC-ALPHA" not in athena_brief


def test_briefing_contains_only_own_mistakes(two_personalities):
    atlas, athena = two_personalities
    atlas_brief = atlas.briefing()
    athena_brief = athena.briefing()

    assert "ATLAS-MIST-ALPHA" in atlas_brief
    assert "ATHENA-MIST-OMEGA" not in atlas_brief
    assert "ATHENA-MIST-OMEGA" in athena_brief
    assert "ATLAS-MIST-ALPHA" not in athena_brief


def test_briefing_header_counts_scoped(two_personalities):
    atlas, athena = two_personalities
    # One decision + one mistake each — not two of either.
    assert atlas.db.count_decisions() == 1
    assert athena.db.count_decisions() == 1
    assert atlas.db.count_mistakes() == 1
    assert athena.db.count_mistakes() == 1
    assert "1 mistakes, 1 decisions" in atlas.briefing()


def test_hypnos_section_scoped(two_personalities):
    atlas, athena = two_personalities
    atlas_lines = "\n".join(render_hypnos_section(atlas.db))
    athena_lines = "\n".join(render_hypnos_section(athena.db))

    assert "2 archived" in atlas_lines
    assert "promoted" not in atlas_lines
    assert "3 promoted" in athena_lines
    assert "archived" not in athena_lines


def test_facts_are_shared_across_personalities(two_personalities):
    """Facts = shared knowledge plane (one trust domain per store)."""
    atlas, athena = two_personalities
    assert atlas.db.count_facts() == athena.db.count_facts() >= 1
    # The fact atlas learned surfaces in BOTH briefings' recent context.
    assert "SHARED-FACT" in atlas.briefing()
    assert "SHARED-FACT" in athena.briefing()


# ── Other read surfaces ────────────────────────────────────────────────────


def test_status_counts_scoped(two_personalities):
    atlas, athena = two_personalities
    assert "Mistakes: 1" in atlas.status()
    assert "Decisions: 1" in athena.status()


def test_decision_feed_scoped(two_personalities):
    atlas, athena = two_personalities
    atlas.db.insert_decision_feed(
        {"decision": "ATLAS-FEED-ALPHA", "session_id": "s-a1",
         "created_at": _now()})
    atlas.db.conn.commit()
    athena.db.insert_decision_feed(
        {"decision": "ATHENA-FEED-OMEGA", "session_id": "s-b1",
         "created_at": _now()})
    athena.db.conn.commit()

    atlas_feed = [d["decision"] for d in atlas.db.get_decision_feed()]
    athena_feed = [d["decision"] for d in athena.db.get_decision_feed()]
    assert atlas_feed == ["ATLAS-FEED-ALPHA"]
    assert athena_feed == ["ATHENA-FEED-OMEGA"]


def test_find_decision_scoped(two_personalities):
    """null_outcome must not close another personality's decision."""
    atlas, athena = two_personalities
    found = athena.db.find_decision("ATLAS-DEC-ALPHA unified scoper")
    assert found is None
    own = athena.db.find_decision("ATHENA-DEC-OMEGA hiwave")
    assert own is not None and "ATHENA-DEC-OMEGA" in own["decision"]


def test_search_mistakes_scoped(two_personalities):
    atlas, athena = two_personalities
    hits = athena.db.search_mistakes("stale handoff")
    assert hits == []
    own = athena.db.search_mistakes("polish")
    assert len(own) == 1 and "ATHENA-MIST-OMEGA" in own[0]["mistake"]


def test_fingerprints_scoped(two_personalities):
    atlas, athena = two_personalities
    atlas.db.insert_fingerprint(
        {"session_id": "fp-atlas", "created_at": _now()})
    atlas.db.conn.commit()
    athena.db.insert_fingerprint(
        {"session_id": "fp-athena", "created_at": _now()})
    athena.db.conn.commit()
    sids = {fp["session_id"] for fp in atlas.db.get_fingerprints()}
    assert sids == {"fp-atlas"}


# ── Legacy single-personality stores stay unchanged ───────────────────────


def test_legacy_store_briefing_unchanged(tmp_path, monkeypatch):
    """Per-personality store (NULL_DIR far away → no unified mode): the
    scoping predicate collapses and the briefing shows everything as
    before, even though the legacy schema has no personality column."""
    monkeypatch.setenv("NULL_DIR", str(tmp_path / "elsewhere"))
    agent_dir = tmp_path / "solo"
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert not mem.db.unified

    mem.db.insert_decision(
        {"decision": "SOLO-DEC legacy stores still brief",
         "created_at": _now()})
    mem.db.insert_mistake(
        {"mistake": "SOLO-MIST legacy stores still brief",
         "why": "", "created_at": _now()})
    mem.db.conn.commit()

    brief = mem.briefing()
    assert "SOLO-DEC" in brief
    assert "SOLO-MIST" in brief
    assert mem.db.count_decisions() == 1
    assert mem.db.count_mistakes() == 1
