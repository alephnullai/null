"""Tests for Hypnos — sleep/dream memory maintenance system."""

import hashlib
import pytest
from datetime import datetime, timedelta, timezone

from null_memory.agent import AgentMemory
from null_memory.hypnos import Hypnos, HypnosResult, hypnos_wakeup_section


def _make_fact(mem, text, age_days=0, confidence=0.8, access_count=0,
               tier="contextual", project="global", session_id=None,
               impact=0.5):
    """Insert a fact with controllable age and metadata."""
    now = datetime.now(timezone.utc)
    created = (now - timedelta(days=age_days)).isoformat()
    fid = hashlib.sha256(f"{text}:{project}".encode()).hexdigest()[:16]
    mem.db.insert_fact({
        "id": fid,
        "fact": text,
        "confidence": confidence,
        "base_confidence": confidence,
        "project": project,
        "source": "test",
        "provenance": "observation",
        "impact": impact,
        "session_id": session_id,
        "created_at": created,
        "access_count": access_count,
        "tier": tier,
    })
    mem.db.conn.commit()
    return fid


def _make_decision(mem, text, session_id=None, project="global"):
    """Insert a decision with optional session_id."""
    now = datetime.now(timezone.utc).isoformat()
    mem.db.insert_decision({
        "decision": text,
        "reasoning": "test reasoning",
        "project": project,
        "session_id": session_id,
        "created_at": now,
    })
    mem.db.conn.commit()


def _make_mistake(mem, text, session_id=None, project="global"):
    """Insert a mistake with optional session_id."""
    now = datetime.now(timezone.utc).isoformat()
    mem.db.insert_mistake({
        "mistake": text,
        "why": "test reason",
        "project": project,
        "session_id": session_id,
        "created_at": now,
    })
    mem.db.conn.commit()


def _make_reflection(mem, went_well="", missed="", do_differently="",
                     session_id=None, project="global"):
    """Insert a reflection."""
    now = datetime.now(timezone.utc).isoformat()
    mem.db.insert_reflection({
        "went_well": went_well,
        "missed": missed,
        "do_differently": do_differently,
        "project": project,
        "session_id": session_id,
        "created_at": now,
    })
    mem.db.conn.commit()


class TestStage1Decay:
    def test_archives_old_low_confidence(self, mem):
        """Old facts with 0 access and low confidence get archived."""
        fid = _make_fact(mem, "ancient forgotten knowledge",
                         age_days=100, confidence=0.01)
        h = Hypnos(mem)
        result = h.run(stages=[1])
        assert result.stage1_archived >= 1
        fact = mem.db.get_fact_by_id(fid)
        assert fact["archived"] == 1

    def test_keeps_accessed_facts(self, mem):
        """Facts with access_count > 0 survive decay even if old and low conf."""
        fid = _make_fact(mem, "frequently recalled ancient fact",
                         age_days=100, confidence=0.01, access_count=3)
        h = Hypnos(mem)
        result = h.run(stages=[1])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["archived"] == 0

    def test_keeps_young_facts(self, mem):
        """Young facts survive even with low confidence."""
        fid = _make_fact(mem, "recent low confidence observation",
                         age_days=5, confidence=0.04)
        h = Hypnos(mem)
        h.run(stages=[1])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["archived"] == 0

    def test_ultra_low_archived_regardless(self, mem):
        """Facts with ultra-low effective confidence get archived even if recent."""
        # 0.001 base confidence => eff_conf will be < 0.025
        fid = _make_fact(mem, "near-zero confidence noise",
                         age_days=5, confidence=0.001)
        h = Hypnos(mem)
        result = h.run(stages=[1])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["archived"] == 1


class TestStage2Tiers:
    def test_promotes_high_access(self, mem):
        """contextual->durable when access_count >= threshold."""
        fid = _make_fact(mem, "heavily used knowledge base entry",
                         access_count=15, tier="contextual")
        h = Hypnos(mem)
        result = h.run(stages=[2])
        assert result.stage2_promoted >= 1
        fact = mem.db.get_fact_by_id(fid)
        assert fact["tier"] == "durable"

    def test_promotes_verified(self, mem):
        """contextual->durable when fact is verified."""
        fid = _make_fact(mem, "verified technical specification")
        mem.db.verify_fact(fid, session_id="test-session")
        mem.db.conn.commit()
        h = Hypnos(mem)
        result = h.run(stages=[2])
        assert result.stage2_promoted >= 1
        fact = mem.db.get_fact_by_id(fid)
        assert fact["tier"] == "durable"

    def test_promotes_decision_referenced(self, mem):
        """contextual->durable when session has 2+ decisions."""
        sid = "decision-heavy-session"
        fid = _make_fact(mem, "fact in decision session",
                         session_id=sid, tier="contextual")
        _make_decision(mem, "first important decision", session_id=sid)
        _make_decision(mem, "second important decision", session_id=sid)
        h = Hypnos(mem)
        result = h.run(stages=[2])
        assert result.stage2_promoted >= 1
        fact = mem.db.get_fact_by_id(fid)
        assert fact["tier"] == "durable"

    def test_demotes_idle_durable(self, mem):
        """durable->contextual when idle 60+ days with no refs."""
        fid = _make_fact(mem, "once important now forgotten durable fact",
                         age_days=90, tier="durable", access_count=0)
        h = Hypnos(mem)
        result = h.run(stages=[2])
        assert result.stage2_demoted >= 1
        fact = mem.db.get_fact_by_id(fid)
        assert fact["tier"] == "contextual"

    def test_no_demote_with_decision_ref(self, mem):
        """durable facts in decision sessions resist demotion."""
        sid = "important-session"
        fid = _make_fact(mem, "durable fact tied to decisions",
                         age_days=90, tier="durable", access_count=0,
                         session_id=sid)
        _make_decision(mem, "critical architectural call", session_id=sid)
        h = Hypnos(mem)
        h.run(stages=[2])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["tier"] == "durable"

    def test_no_demote_accessed(self, mem):
        """durable facts with access survive even if old."""
        fid = _make_fact(mem, "old but still used durable fact",
                         age_days=90, tier="durable", access_count=5)
        h = Hypnos(mem)
        h.run(stages=[2])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["tier"] == "durable"


class TestStage3Salience:
    def test_decision_boost(self, mem):
        """Facts in decision sessions get impact boost."""
        sid = "decision-session"
        fid = _make_fact(mem, "fact near an important decision",
                         session_id=sid, impact=0.5)
        _make_decision(mem, "major technical decision", session_id=sid)
        h = Hypnos(mem)
        result = h.run(stages=[3])
        assert result.stage3_boosted >= 1
        fact = mem.db.get_fact_by_id(fid)
        assert fact["impact"] > 0.5

    def test_mistake_boost_higher(self, mem):
        """Mistake boost (0.3) is applied for facts in mistake sessions."""
        sid = "mistake-session"
        fid = _make_fact(mem, "fact learned from a painful mistake",
                         session_id=sid, impact=0.5)
        _make_mistake(mem, "broke production deploy", session_id=sid)
        h = Hypnos(mem)
        h.run(stages=[3])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["impact"] == pytest.approx(0.8, abs=0.01)

    def test_reflection_boost(self, mem):
        """Facts in positive-reflection sessions get mild boost."""
        sid = "good-session"
        fid = _make_fact(mem, "fact from a productive session",
                         session_id=sid, impact=0.5)
        _make_reflection(mem, went_well="shipped major feature cleanly",
                         session_id=sid)
        h = Hypnos(mem)
        h.run(stages=[3])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["impact"] > 0.5

    def test_impact_capped_at_one(self, mem):
        """Impact never exceeds 1.0."""
        sid = "mega-session"
        fid = _make_fact(mem, "already high impact fact",
                         session_id=sid, impact=0.95)
        _make_mistake(mem, "critical error", session_id=sid)
        h = Hypnos(mem)
        h.run(stages=[3])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["impact"] <= 1.0

    def test_co_session_linking(self, mem):
        """Facts in same session get related_to populated."""
        sid = "shared-session"
        fid1 = _make_fact(mem, "first fact in shared context",
                          session_id=sid)
        fid2 = _make_fact(mem, "second fact in shared context",
                          session_id=sid)
        h = Hypnos(mem)
        result = h.run(stages=[3])
        assert result.stage3_relationships >= 1
        related1 = mem.db.get_related_ids(fid1)
        related2 = mem.db.get_related_ids(fid2)
        assert fid2 in related1
        assert fid1 in related2

    def test_no_linking_large_sessions(self, mem):
        """Sessions with >5 facts don't get linked (too noisy)."""
        sid = "bulk-session"
        fids = []
        for i in range(7):
            fid = _make_fact(mem, f"bulk fact number {i} in a large batch",
                             session_id=sid)
            fids.append(fid)
        h = Hypnos(mem)
        result = h.run(stages=[3])
        assert result.stage3_relationships == 0


class TestStage4ColdStorage:
    def test_archives_dormant(self, mem):
        """0-access, 90+ day old, low-confidence facts get cold stored."""
        fid = _make_fact(mem, "ancient untouched low value observation",
                         age_days=120, confidence=0.1, access_count=0)
        h = Hypnos(mem)
        result = h.run(stages=[4])
        assert result.stage4_cold_stored >= 1
        fact = mem.db.get_fact_by_id(fid)
        assert fact["archived"] == 1

    def test_keeps_accessed(self, mem):
        """Facts with any access survive cold storage."""
        fid = _make_fact(mem, "old but recalled at least once",
                         age_days=120, confidence=0.1, access_count=1)
        h = Hypnos(mem)
        h.run(stages=[4])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["archived"] == 0

    def test_keeps_young(self, mem):
        """Facts under 90 days survive cold storage."""
        fid = _make_fact(mem, "relatively recent zero access fact",
                         age_days=30, confidence=0.1, access_count=0)
        h = Hypnos(mem)
        h.run(stages=[4])
        fact = mem.db.get_fact_by_id(fid)
        assert fact["archived"] == 0


class TestFullRun:
    def test_run_all_stages(self, mem):
        """Full run returns sensible HypnosResult."""
        _make_fact(mem, "a normal healthy fact", confidence=0.9)
        h = Hypnos(mem)
        result = h.run()
        assert isinstance(result, HypnosResult)
        assert result.run_id
        assert result.started_at
        assert result.completed_at
        assert result.total_active >= 0

    def test_selective_stages(self, mem):
        """Can run only specific stages."""
        _make_fact(mem, "test fact for selective run")
        h = Hypnos(mem)
        result = h.run(stages=[1, 3])
        assert isinstance(result, HypnosResult)
        # Stage 2 and 4 should be 0 since not run
        assert result.stage2_promoted == 0
        assert result.stage2_demoted == 0
        assert result.stage4_cold_stored == 0

    def test_journal_populated(self, mem):
        """Run populates hypnos_journal table."""
        _make_fact(mem, "old forgotten junk observation",
                   age_days=100, confidence=0.01)
        h = Hypnos(mem)
        h.run()
        entries = mem.db.get_latest_hypnos_run()
        assert len(entries) > 0
        assert entries[0]["run_id"] == h.run_id

    def test_idempotent(self, mem):
        """Running twice doesn't double-archive."""
        _make_fact(mem, "will be archived once then gone",
                   age_days=100, confidence=0.01)

        h1 = Hypnos(mem)
        r1 = h1.run()
        archived_first = r1.stage1_archived + r1.stage4_cold_stored

        h2 = Hypnos(mem)
        r2 = h2.run()
        archived_second = r2.stage1_archived + r2.stage4_cold_stored

        assert archived_first >= 1
        assert archived_second == 0  # Already archived

    def test_empty_memory(self, mem):
        """Running on empty memory doesn't crash."""
        h = Hypnos(mem)
        result = h.run()
        assert result.total_active == 0
        assert result.errors == []


class TestWakeupSection:
    def test_no_runs(self, mem):
        """Returns empty when no Hypnos runs exist."""
        lines = hypnos_wakeup_section(mem.db)
        assert lines == []

    def test_after_run(self, mem):
        """Returns summary line after a Hypnos run."""
        _make_fact(mem, "ancient dead weight observation",
                   age_days=100, confidence=0.01)
        h = Hypnos(mem)
        h.run()
        lines = hypnos_wakeup_section(mem.db)
        assert len(lines) == 1
        assert "Hypnos" in lines[0]
        assert "archived" in lines[0]

    def test_no_changes_run(self, mem):
        """Shows 'no changes' when run had nothing to do."""
        _make_fact(mem, "perfectly healthy recent fact", confidence=0.9)
        h = Hypnos(mem)
        h.run()
        lines = hypnos_wakeup_section(mem.db)
        assert len(lines) == 1
        assert "no changes" in lines[0]
