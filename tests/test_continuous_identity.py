"""Tests for Phase 3b — continuous identity (per-turn drift detection)."""

from __future__ import annotations

import pytest

from null_memory.agent import AgentMemory
from null_memory.migrate_v3 import init_unified_db


pytestmark = pytest.mark.skipif(
    pytest.importorskip("fastembed", reason="fastembed not installed") is None,
    reason="continuous identity requires embeddings",
)


@pytest.fixture
def unified_agent(tmp_path, monkeypatch):
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    mem = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert mem.db.unified
    mem.start_session(project="null")
    return mem


# ── Signature buffer ──────────────────────────────────────────────────────


def test_observe_records_turn_signature(unified_agent):
    mem = unified_agent
    assert len(mem._turn_signatures) == 0
    mem.observe("first observation about the project")
    assert len(mem._turn_signatures) == 1
    import numpy as np
    assert isinstance(mem._turn_signatures[0], np.ndarray)


def test_decide_and_mistake_and_reflect_all_record(unified_agent):
    mem = unified_agent
    mem.observe("a thing happened")
    mem.decide("use postgres", "already in stack")
    mem.mistake("forgot tests", "rushed")
    mem.reflect("shipped fast", "skipped review", "slow down")
    assert len(mem._turn_signatures) == 4


def test_buffer_caps_at_TURN_SIGNATURE_BUFFER(unified_agent):
    mem = unified_agent
    cap = mem.TURN_SIGNATURE_BUFFER
    for i in range(cap + 5):
        mem.observe(f"observation number {i} about varied topic alpha")
    assert len(mem._turn_signatures) == cap


# ── Drift detection ───────────────────────────────────────────────────────


def test_no_drift_for_consistent_turns(unified_agent):
    mem = unified_agent
    # A stream of related observations should NOT trigger drift
    for i in range(6):
        mem.observe(f"working on Null Memory Phase 2 probes, iteration {i}")
    assert mem.consume_mid_session_drift_warning() is None


def test_drift_fires_on_sharp_topic_change(unified_agent):
    mem = unified_agent
    # Build consistent baseline
    for i in range(5):
        mem.observe(f"Null Memory identity verification continuity probes iteration {i}")
    # Inject a wildly divergent turn
    mem.observe("quantum chromodynamics gluon confinement lattice calculations")
    warn = mem.consume_mid_session_drift_warning()
    assert warn is not None
    assert warn["distance"] >= mem.MID_SESSION_DRIFT_THRESHOLD


def test_drift_warning_consumed_once(unified_agent):
    mem = unified_agent
    for i in range(5):
        mem.observe(f"null memory phase tests {i}")
    mem.observe("quantum chromodynamics lattice gluon")
    first = mem.consume_mid_session_drift_warning()
    second = mem.consume_mid_session_drift_warning()
    assert first is not None
    assert second is None


def test_drift_requires_minimum_baseline(unified_agent):
    mem = unified_agent
    # Only one turn — no baseline → no drift fires even on divergent content
    mem.observe("first thing")
    assert mem.consume_mid_session_drift_warning() is None


# ── verify_identity integration ───────────────────────────────────────────


def test_verify_identity_has_four_proofs(unified_agent):
    mem = unified_agent
    result = mem.verify_identity()
    assert "mid_session_continuity" in result["proofs"]


def test_verify_reports_mid_session_insufficient_when_empty(unified_agent):
    mem = unified_agent
    # No turns yet
    result = mem.verify_identity()
    assert result["proofs"]["mid_session_continuity"] is None


def test_verify_reports_mid_session_pass_when_consistent(unified_agent):
    mem = unified_agent
    for i in range(5):
        mem.observe(f"null work iteration {i} thesis consistent")
    result = mem.verify_identity()
    assert result["proofs"]["mid_session_continuity"] is True


def test_verify_reports_mid_session_fail_when_drift(unified_agent):
    mem = unified_agent
    for i in range(5):
        mem.observe(f"null memory phase tests iteration {i}")
    mem.observe("quantum chromodynamics lattice gluon confinement")
    # Before consuming the warning, state still reflects drift
    result = mem.verify_identity()
    assert result["proofs"]["mid_session_continuity"] is False
