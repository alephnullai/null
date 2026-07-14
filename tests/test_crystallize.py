"""Tests for Hypnos Stage 4.5 crystallization.

Pure-function tests — LLM is stubbed via dependency injection. No network.
"""

from __future__ import annotations

import json
import pytest

from null_memory.crystallize import (
    ANCHOR_IMMUNE,
    CHILD_CONFIDENCE_CAP,
    MIN_LEN_TO_CRYSTALLIZE,
    crystallize_fact,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _llm_returning(atoms: list[str]):
    """Build a stub LLM that always returns the given atoms as JSON."""
    def _call(prompt: str) -> str:
        return json.dumps({"atoms": atoms})
    return _call


def _llm_returning_raw(text: str):
    def _call(prompt: str) -> str:
        return text
    return _call


def _long_parent(**over):
    base = {
        "id": "p1",
        "fact": "x" * 350,  # > MIN_LEN_TO_CRYSTALLIZE
        "confidence": 0.9,
        "impact": 0.6,
        "project": "orion",
        "session_id": "s1",
        "tier": "contextual",
        "provenance": "observation",
        "anchor_type": None,
        "anchor_note": None,
        "anchor_at": None,
        "crystallized_into": None,
        "forgotten": 0,
        "archived": 0,
        "superseded_by": None,
    }
    base.update(over)
    return base


# ── Length floor ─────────────────────────────────────────────────────


def test_short_fact_returns_none():
    parent = _long_parent(fact="too short")
    assert crystallize_fact(parent, _llm_returning(["a", "b"])) is None


def test_at_threshold_minus_one_skipped():
    parent = _long_parent(fact="x" * (MIN_LEN_TO_CRYSTALLIZE - 1))
    assert crystallize_fact(parent, _llm_returning(["a", "b"])) is None


# ── Anchor immunity ──────────────────────────────────────────────────


@pytest.mark.parametrize("atype", sorted(ANCHOR_IMMUNE))
def test_anchored_facts_are_immune(atype):
    parent = _long_parent(anchor_type=atype)
    result = crystallize_fact(parent, _llm_returning(["a" * 50, "b" * 50]))
    assert result is None, f"anchor_type={atype} should be immune"


# ── Idempotency ──────────────────────────────────────────────────────


def test_already_crystallized_parent_skipped():
    parent = _long_parent(crystallized_into=json.dumps(["c1", "c2"]))
    assert crystallize_fact(parent, _llm_returning(["a", "b"])) is None


def test_archived_or_forgotten_skipped():
    assert crystallize_fact(
        _long_parent(archived=1), _llm_returning(["a" * 50, "b" * 50])
    ) is None
    assert crystallize_fact(
        _long_parent(forgotten=1), _llm_returning(["a" * 50, "b" * 50])
    ) is None
    assert crystallize_fact(
        _long_parent(superseded_by="other"),
        _llm_returning(["a" * 50, "b" * 50]),
    ) is None


# ── Happy path ───────────────────────────────────────────────────────


def test_long_fact_splits_into_atomic_children():
    # Parent must exceed MIN_LEN_TO_CRYSTALLIZE (300) — pad with realistic
    # context to clear the floor while keeping recognizable atoms.
    parent_text = (
        "Pete answered the calibration question with rich detail about "
        "his kids: Sam (Samuel Jr., born April 19 2018, age 7) plays "
        "football and flag-football, is a good QB, starting baseball "
        "soon. Riley (born July 14 2014, age 11) plays soccer, just "
        "had her last gymnastics meet today (grew out of it), starting "
        "track soon. Pete will be assistant coach for baseball."
    )
    assert len(parent_text) > MIN_LEN_TO_CRYSTALLIZE
    parent = _long_parent(fact=parent_text)
    atoms = [
        "Sam is Samuel Jr., born April 19 2018",
        "Sam plays football",
        "Riley born July 14 2014, plays soccer",
        "Pete will assist coach baseball",
    ]
    children = crystallize_fact(parent, _llm_returning(atoms))
    assert children is not None
    assert len(children) == 4
    assert [c["fact"] for c in children] == atoms


def test_children_inherit_parent_metadata():
    parent = _long_parent(
        fact="x" * 400,
        project="orion",
        session_id="s_123",
        tier="long_term",
        provenance="reflection",
    )
    children = crystallize_fact(
        parent, _llm_returning(["atom one " * 5, "atom two " * 5]),
    )
    assert children is not None
    for child in children:
        assert child["project"] == "orion"
        assert child["session_id"] == "s_123"
        assert child["tier"] == "long_term"
        assert child["provenance"] == "reflection"
        assert child["source"] == "crystallized"
        assert child["crystallized_from"] == "p1"


def test_child_confidence_capped():
    """Child confidence cannot exceed CHILD_CONFIDENCE_CAP regardless of parent."""
    parent = _long_parent(fact="x" * 400, confidence=0.99)
    children = crystallize_fact(
        parent, _llm_returning(["a" * 30, "b" * 30]),
    )
    for c in children:
        assert c["confidence"] == CHILD_CONFIDENCE_CAP


def test_child_confidence_inherits_when_below_cap():
    parent = _long_parent(fact="x" * 400, confidence=0.5)
    children = crystallize_fact(
        parent, _llm_returning(["a" * 30, "b" * 30]),
    )
    for c in children:
        assert c["confidence"] == 0.5


def test_impact_is_split_across_children():
    parent = _long_parent(fact="x" * 400, impact=0.8)
    children = crystallize_fact(
        parent, _llm_returning(["a" * 30, "b" * 30, "c" * 30, "d" * 30]),
    )
    for c in children:
        assert c["impact"] == pytest.approx(0.8 / 4)


# ── Suspicious-output guard ──────────────────────────────────────────


def test_zero_atoms_skipped():
    """LLM returns {atoms: []} = "fact is already atomic, leave alone"."""
    parent = _long_parent()
    assert crystallize_fact(parent, _llm_returning([])) is None


def test_single_atom_skipped():
    """1 atom = paraphrase, not split. Bail."""
    parent = _long_parent()
    assert crystallize_fact(parent, _llm_returning(["just one"])) is None


def test_too_many_atoms_truncated_to_max():
    """Real-world haiku occasionally over-atomizes (returns 11+). Rather
    than skip the parent, take the first MAX_ATOMS — they tend to be
    the most load-bearing claims because LLMs emit priority-first."""
    from null_memory.crystallize import MAX_ATOMS
    parent = _long_parent()
    children = crystallize_fact(
        parent, _llm_returning([f"atom {i}" for i in range(20)])
    )
    assert children is not None
    assert len(children) == MAX_ATOMS
    # First MAX_ATOMS should be preserved in order.
    assert [c["fact"] for c in children] == [
        f"atom {i}" for i in range(MAX_ATOMS)
    ]


def test_atom_equal_to_parent_skipped():
    parent_text = "x" * 400
    parent = _long_parent(fact=parent_text)
    # LLM returns 2 atoms but one is the whole parent — suspicious.
    assert crystallize_fact(
        parent, _llm_returning([parent_text, "tiny atom"])
    ) is None


def test_atom_too_close_to_parent_length_skipped():
    """An atom >70% of parent length is the parent paraphrased, not atomized."""
    parent_text = "x" * 400
    parent = _long_parent(fact=parent_text)
    # 280 chars = 70% — boundary. 290 = over.
    assert crystallize_fact(
        parent, _llm_returning(["a" * 290, "b" * 50])
    ) is None


# ── Provenance noise filter ──────────────────────────────────────────


def test_provenance_noise_atoms_stripped():
    """Voice-transcript header lines like 'Atlas replied to BigPeter on
    YYYY-MM-DD at HH:MM' are dutifully extracted by the LLM but carry no
    semantic content. They must be filtered before counting."""
    parent = _long_parent(fact="x" * 400)
    children = crystallize_fact(
        parent, _llm_returning([
            "Atlas replied to BigPeter on 2026-04-29 at 10:45",
            "Real claim about MCP architecture",
            "Real claim about identity injection",
        ]),
    )
    assert children is not None
    assert len(children) == 2
    assert all("Atlas replied to" not in c["fact"] for c in children)


def test_provenance_only_split_skipped():
    """If after filtering, only 0-1 real atoms remain, skip the parent
    entirely — don't produce a half-empty split."""
    parent = _long_parent(fact="x" * 400)
    # All three look like provenance noise — nothing real survives filter.
    children = crystallize_fact(
        parent, _llm_returning([
            "Atlas replied to BigPeter on 2026-04-29 at 10:45",
            "Pete asked about Orion on 2026-04-29.",
            "[voice:2026-04-29 09:58] Atlas replied to BigPeter:",
        ]),
    )
    assert children is None


# ── Parser robustness ────────────────────────────────────────────────


def test_llm_returns_prose_wrapped_json():
    """LLM occasionally adds 'Here you go:' before JSON. We tolerate it."""
    parent = _long_parent(fact="x" * 400)
    raw = 'Sure, here are the atoms:\n{"atoms": ["alpha", "beta", "gamma"]}\nLet me know if needed.'
    children = crystallize_fact(parent, _llm_returning_raw(raw))
    assert children is not None
    assert [c["fact"] for c in children] == ["alpha", "beta", "gamma"]


def test_llm_returns_garbage_handled():
    parent = _long_parent(fact="x" * 400)
    assert crystallize_fact(
        parent, _llm_returning_raw("not json at all 🤷"),
    ) is None


def test_llm_returns_wrong_shape_handled():
    parent = _long_parent(fact="x" * 400)
    assert crystallize_fact(
        parent, _llm_returning_raw('{"items": ["a", "b"]}'),
    ) is None


def test_llm_raises_handled():
    """LLM exception → None, never propagated."""
    def _boom(_p):
        raise RuntimeError("API down")
    parent = _long_parent(fact="x" * 400)
    assert crystallize_fact(parent, _boom) is None


# ── Hypnos Stage 4.5 integration ─────────────────────────────────────


@pytest.fixture
def stage45_mem(tmp_path):
    """AgentMemory pointing at a fresh unified DB with one long fact."""
    from null_memory.agent import AgentMemory
    from null_memory.migrate_v3 import init_unified_db
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    import os
    os.environ["NULL_DIR"] = str(tmp_path)
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    yield mem
    del os.environ["NULL_DIR"]


def _seed_long_fact(mem, fact_text: str, anchor_type: str | None = None):
    import hashlib
    fact_id = hashlib.sha256(
        f"global:{fact_text.strip().lower()}".encode()
    ).hexdigest()[:12]
    mem.db.conn.execute(
        """INSERT INTO facts
           (id, fact, confidence, base_confidence, project, source,
            provenance, impact, session_id, created_at, access_count,
            tier, anchor_type)
           VALUES (?, ?, 0.9, 0.9, 'global', 'observation',
                   'observation', 0.6, NULL, '2026-04-01', 0,
                   'contextual', ?)""",
        (fact_id, fact_text, anchor_type),
    )
    mem.db.conn.commit()
    return fact_id


def test_stage45_dry_run_writes_nothing(stage45_mem, monkeypatch):
    """Dry-run logs but doesn't insert children or archive parent."""
    from null_memory.hypnos import Hypnos
    long_text = "Pete and Sam and Riley and " + ("x " * 200)
    parent_id = _seed_long_fact(stage45_mem, long_text)
    stage45_mem.config["hypnos_crystallize_dryrun"] = True
    stage45_mem.config["hypnos_crystallize_llm"] = lambda _p: (
        '{"atoms": ["atom one " * 5, "atom two " * 5, "atom three " * 5]}'
    )
    hypnos = Hypnos(stage45_mem)
    result = hypnos.run(stages=[45])
    # Nothing created or archived.
    assert result.stage45_crystallized == 0
    assert result.stage45_archived_parents == 0
    # Parent stays active.
    row = stage45_mem.db.conn.execute(
        "SELECT archived, crystallized_into FROM facts WHERE id = ?",
        (parent_id,),
    ).fetchone()
    assert row[0] == 0
    assert row[1] is None


def test_stage45_real_run_splits_and_archives(stage45_mem, monkeypatch):
    """Non-dry-run inserts atomic children, marks parent archived,
    populates crystallized_into / crystallized_from."""
    import json
    from null_memory.hypnos import Hypnos
    long_text = (
        "Pete answered the calibration question with rich detail about "
        "his kids: Sam is Samuel Jr., born April 19 2018, plays football "
        "and flag-football, good QB, starting baseball soon. Riley "
        "born July 14 2014, plays soccer, finished gymnastics, starting "
        "track. Pete will be assistant coach for baseball. The whole "
        "calibration loop is working as designed: Atlas asked, Pete "
        "answered, memory got richer with each turn."
    )
    assert len(long_text) >= 300
    parent_id = _seed_long_fact(stage45_mem, long_text)
    stage45_mem.config["hypnos_crystallize_dryrun"] = False
    stage45_mem.config["hypnos_crystallize_llm"] = lambda _p: json.dumps({
        "atoms": [
            "Sam is Samuel Jr., born April 19 2018",
            "Sam plays football",
            "Riley born July 14 2014, plays soccer",
            "Pete will assistant coach baseball",
        ],
    })
    hypnos = Hypnos(stage45_mem)
    result = hypnos.run(stages=[45])
    assert result.stage45_crystallized == 4
    assert result.stage45_archived_parents == 1

    # Parent: archived + crystallized_into populated
    row = stage45_mem.db.conn.execute(
        "SELECT archived, crystallized_into FROM facts WHERE id = ?",
        (parent_id,),
    ).fetchone()
    assert row[0] == 1
    child_ids = json.loads(row[1])
    assert len(child_ids) == 4

    # Children: exist, point back via crystallized_from
    for cid in child_ids:
        c = stage45_mem.db.conn.execute(
            "SELECT crystallized_from, archived, source FROM facts WHERE id = ?",
            (cid,),
        ).fetchone()
        assert c is not None
        assert c[0] == parent_id
        assert c[1] == 0  # children active
        assert c[2] == "crystallized"


def test_stage45_anchor_immune_in_db(stage45_mem):
    """Anchor-typed facts in the live DB don't get crystallized."""
    from null_memory.hypnos import Hypnos
    long_text = "An anchor fact " + ("y " * 200)
    parent_id = _seed_long_fact(stage45_mem, long_text, anchor_type="origin")
    stage45_mem.config["hypnos_crystallize_dryrun"] = False
    stage45_mem.config["hypnos_crystallize_llm"] = lambda _p: (
        '{"atoms": ["a" * 30, "b" * 30]}'
    )
    hypnos = Hypnos(stage45_mem)
    result = hypnos.run(stages=[45])
    assert result.stage45_crystallized == 0
    # Parent untouched
    row = stage45_mem.db.conn.execute(
        "SELECT archived FROM facts WHERE id = ?", (parent_id,),
    ).fetchone()
    assert row[0] == 0


def test_stage45_kill_switch_caps_archives(stage45_mem):
    """If max_per_pass is set low, the kill switch aborts early."""
    import json
    from null_memory.hypnos import Hypnos
    # Seed 5 long facts.
    for i in range(5):
        text = f"Fact {i}: " + ("z " * 200)
        _seed_long_fact(stage45_mem, text)
    stage45_mem.config["hypnos_crystallize_dryrun"] = False
    stage45_mem.config["hypnos_crystallize_max_per_pass"] = 2
    stage45_mem.config["hypnos_crystallize_llm"] = lambda _p: json.dumps({
        "atoms": ["alpha alpha alpha", "beta beta beta", "gamma gamma gamma"],
    })
    hypnos = Hypnos(stage45_mem)
    result = hypnos.run(stages=[45])
    assert result.stage45_archived_parents == 2  # capped


def test_stage45_idempotent_on_second_run(stage45_mem):
    """Running stage 45 twice on the same DB doesn't re-crystallize
    already-crystallized parents."""
    import json
    from null_memory.hypnos import Hypnos
    long_text = "Pete's kids are " + ("k " * 200)
    _seed_long_fact(stage45_mem, long_text)
    stage45_mem.config["hypnos_crystallize_dryrun"] = False
    stage45_mem.config["hypnos_crystallize_llm"] = lambda _p: json.dumps({
        "atoms": ["alpha alpha alpha", "beta beta beta"],
    })
    hypnos = Hypnos(stage45_mem)
    r1 = hypnos.run(stages=[45])
    r2 = hypnos.run(stages=[45])
    assert r1.stage45_archived_parents == 1
    assert r2.stage45_archived_parents == 0  # idempotent
