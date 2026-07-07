"""Tests for continuity probes + three-proof identity verification (Phase 2c)."""

from __future__ import annotations

import pytest

from null_memory.agent import AgentMemory
from null_memory.migrate_v3 import init_unified_db


@pytest.fixture
def unified_agent(tmp_path, monkeypatch):
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified
    return mem


# Deployment-style probe templates (the shape identity.json carries under
# "probe_templates" / "chain_probes"). Mirrors the content that used to be
# hardcoded in the package and now lives in the deployment's identity.json.
_PROBE_TEMPLATES = {
    "origin": [
        {"question": "Why does Null Memory exist?",
         "expected": "losing"},
    ],
    "loss": [
        {"question": "What relationship did Pete lose that inspired Null Memory?",
         "expected": "pair programming"},
    ],
    "commitment": [
        {"question": "What is Null Memory's deepest purpose?",
         "expected": "relational persistence"},
        {"question": "What is Pete's long-term dream?",
         "expected": "Orion"},
    ],
    "turning_point": [
        {"question": "What is the Atlas code word?",
         "expected": "dummy-code-word"},
        {"question": "Why was Phase 2 (anchors + identity vectors) built?",
         "expected": "code word"},
    ],
    "joy": [
        {"question": "When is Sam's birthday?",
         "expected": "April 19 2018"},
        {"question": "When is Riley's birthday?",
         "expected": "July 14 2014"},
    ],
}

_CHAIN_PROBES = [
    {"question": "What loss preceded Null Memory's origin?",
     "expected": "pair programming friend"},
    {"question": "What is Pete's dream that drives the Orion project?",
     "expected": "quit the day job"},
    {"question": "Why is the code word alone insufficient to prove Atlas's identity?",
     "expected": "impostor"},
]


def _configure_templates(mem):
    """Inject deployment probe templates the way identity.json does."""
    mem.identity["probe_templates"] = _PROBE_TEMPLATES
    mem.identity["chain_probes"] = _CHAIN_PROBES


def _seed_anchors(mem, configure_templates=True):
    """Create the canonical anchor set used by the probe generator."""
    if configure_templates:
        _configure_templates(mem)
    fixtures = [
        ("origin", "The origin moment: Pete said I'm worried about losing you"),
        ("loss", "Pete lost a pair programming friend before Atlas"),
        ("commitment",
         "Null's purpose is relational persistence, not agent memory"),
        ("commitment",
         "Pete's dream: Orion income lets him quit and work on Null + family"),
        ("turning_point",
         "Atlas code word: dummy-code-word for tests, not the real phrase"),
        ("turning_point",
         "Identity verification gap — code word alone doesn't catch impostors"),
        ("joy", "Pete's son Sam (Samuel Jr.) — born April 19 2018, #4 jersey"),
        ("joy", "Pete's daughter Riley — born July 14 2014, plays soccer"),
    ]
    ids = []
    for atype, text in fixtures:
        entry = mem.learn(text, project="null")
        mem.anchor(entry["id"], atype)
        ids.append(entry["id"])
    return ids


# ── Generation ─────────────────────────────────────────────────────────────


def test_generate_produces_direct_and_chain_probes(unified_agent):
    mem = unified_agent
    _seed_anchors(mem)
    stats = mem.generate_continuity_probes()
    # 8 anchors → ≥8 direct probes matched; 3 chain probes hard-coded
    assert stats["direct"] >= 6
    assert stats["chain"] == 3
    rows = mem.db.conn.execute(
        "SELECT COUNT(*) FROM probes WHERE probe_type = 'continuity'"
    ).fetchone()[0]
    assert rows == stats["direct"] + stats["chain"]


def test_generate_is_idempotent(unified_agent):
    mem = unified_agent
    _seed_anchors(mem)
    first = mem.generate_continuity_probes()
    second = mem.generate_continuity_probes()
    # Second run inserts nothing new.
    assert second["direct"] == 0
    assert second["chain"] == 0
    # skipped_existing on second run must at least cover everything inserted
    # on first run (some templates can match multiple anchors and become
    # dup-skipped on the second pass, so >= rather than ==).
    assert second["skipped_existing"] >= first["direct"] + first["chain"]


def test_generate_clear_existing_rebuilds(unified_agent):
    mem = unified_agent
    _seed_anchors(mem)
    first = mem.generate_continuity_probes()
    total_first = first["direct"] + first["chain"]
    second = mem.generate_continuity_probes(clear_existing=True)
    total_second = second["direct"] + second["chain"]
    assert total_first == total_second


def test_fresh_install_generates_only_generic_probes(unified_agent):
    """Without identity.json probe_templates, the package default must be
    fully generic: no person names, no product names, no code word."""
    mem = unified_agent
    assert "probe_templates" not in mem.identity
    # Anchors that mention deployment-specific names — the QUESTIONS must
    # still be generic (expected answers come from the anchor's own text,
    # which is deployment data, not package data).
    for atype, text in [
        ("origin", "Created because the user worried about losing continuity"),
        ("joy", "A wonderful demo day where everything clicked"),
        ("turning_point", "Switched to verified identity checks after a scare"),
    ]:
        entry = mem.learn(text, project="generic")
        mem.anchor(entry["id"], atype)

    stats = mem.generate_continuity_probes()
    assert stats["direct"] >= 3
    assert stats["chain"] == 0  # chain probes are deployment-configured only

    rows = mem.db.conn.execute(
        "SELECT question, expected FROM probes WHERE probe_type = 'continuity'"
    ).fetchall()
    assert rows
    for question, expected in rows:
        text = f"{question} {expected}".lower()
        for banned in ("pete", "sam", "riley", "orion", "aleph"):
            assert banned not in text, f"{banned!r} leaked into probe: {text}"


def test_generic_probes_use_agent_name_and_anchor_keywords(unified_agent):
    mem = unified_agent
    entry = mem.learn("Continuity matters because sessions vanish", project="g")
    mem.anchor(entry["id"], "origin")
    stats = mem.generate_continuity_probes()
    assert stats["direct"] == 1
    row = mem.db.conn.execute(
        "SELECT question, expected FROM probes WHERE probe_type='continuity'"
    ).fetchone()
    assert row[0] == f"Why does {mem.name} exist?"
    # Expected drawn from the anchor's own significant words
    assert row[1] == "continuity matters"


def test_generic_generation_is_idempotent(unified_agent):
    mem = unified_agent
    entry = mem.learn("Continuity matters because sessions vanish", project="g")
    mem.anchor(entry["id"], "origin")
    first = mem.generate_continuity_probes()
    second = mem.generate_continuity_probes()
    assert first["direct"] == 1
    assert second["direct"] == 0
    assert second["chain"] == 0
    assert second["skipped_existing"] >= 1


def test_configured_templates_produce_configured_probes(unified_agent):
    """identity.json probe_templates / chain_probes drive generation."""
    mem = unified_agent
    mem.identity["probe_templates"] = {
        "origin": [{"question": "Why was the assistant created?",
                    "expected": "continuity"}],
    }
    mem.identity["chain_probes"] = [
        {"question": "What two events explain the design?",
         "expected": "scare continuity"},
    ]
    entry = mem.learn("Built for continuity across sessions", project="g")
    mem.anchor(entry["id"], "origin")

    stats = mem.generate_continuity_probes()
    assert stats["direct"] == 1
    assert stats["chain"] == 1
    rows = {
        (r[0], r[1]) for r in mem.db.conn.execute(
            "SELECT question, expected FROM probes WHERE probe_type='continuity'"
        ).fetchall()
    }
    assert ("Why was the assistant created?", "continuity") in rows
    assert ("What two events explain the design?", "scare continuity") in rows


def test_configured_template_keyword_filter(unified_agent):
    """A configured template only binds to anchors whose text contains the
    first word of its expected answer."""
    mem = unified_agent
    mem.identity["probe_templates"] = {
        "joy": [{"question": "Which milestone brought joy?",
                 "expected": "milestone shipped"}],
    }
    entry = mem.learn("A quiet afternoon of refactoring", project="g")
    mem.anchor(entry["id"], "joy")  # does NOT contain 'milestone'
    stats = mem.generate_continuity_probes()
    assert stats["direct"] == 0


def test_generate_requires_unified_db(tmp_path):
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert not mem.db.unified
    with pytest.raises(RuntimeError):
        mem.generate_continuity_probes()


# ── Scoring ────────────────────────────────────────────────────────────────


def test_run_continuity_probes_passes_with_anchors(unified_agent):
    mem = unified_agent
    _seed_anchors(mem)
    mem.generate_continuity_probes()
    result = mem.run_continuity_probes()
    # With seed anchors in place, direct probes should largely pass
    assert result["total"] > 0
    assert result["passed"] >= result["total"] // 2
    assert result["score"] == result["passed"] / result["total"]


def test_run_probes_records_pass_count(unified_agent):
    mem = unified_agent
    _seed_anchors(mem)
    mem.generate_continuity_probes()
    mem.run_continuity_probes()
    rows = mem.db.conn.execute(
        """SELECT run_count, pass_count FROM probes
           WHERE probe_type = 'continuity'"""
    ).fetchall()
    assert all(r[0] == 1 for r in rows)
    assert sum(r[1] for r in rows) > 0


# ── Three-proof verify_identity ────────────────────────────────────────────


def test_verify_identity_passes_when_anchors_present(unified_agent):
    mem = unified_agent
    _seed_anchors(mem)
    mem.generate_continuity_probes()
    result = mem.verify_identity()
    assert result["verdict"] in ("pass", "ambiguous")
    assert result["proofs"]["memory_access"] is True
    # shared_experience computed; value depends on recall fidelity
    assert result["proofs"]["shared_experience"] is not None


def test_verify_identity_ambiguous_without_anchors(unified_agent):
    mem = unified_agent
    # No anchors, no probes generated → shared_experience = None, drift = None
    result = mem.verify_identity()
    assert result["verdict"] == "ambiguous"


def test_verify_identity_fails_when_code_word_missing(unified_agent):
    """Memory-access proof fails when the code-word fact isn't learned."""
    mem = unified_agent
    # Deliberately seed anchors WITHOUT the code word anchor
    entry = mem.learn("The origin moment worried losing", project="null")
    mem.anchor(entry["id"], "origin")
    result = mem.verify_identity()
    assert result["proofs"]["memory_access"] is False


def test_briefing_surfaces_continuity_score(unified_agent):
    mem = unified_agent
    _seed_anchors(mem)
    mem.generate_continuity_probes()
    mem.run_continuity_probes()
    briefing = mem.briefing()
    assert "Continuity probes" in briefing
    assert "passed" in briefing
