"""The Outcomes-pending briefing block inlines a rotating sample of overdue
decision texts (the salvaged kernel of the closed proactive-briefing PR #10):
count alone wasn't actionable; LIMIT 2 keeps the token budget intact.

Assertions are scoped to the Outcomes-pending block itself — decisions also
legitimately render in "Recent decisions"/"Decisions from other sessions"
(different selection criteria), and a tiny test store overlaps everywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from null_memory.agent import AgentMemory


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    m = AgentMemory(agent_dir=str(tmp_path))
    m.learn("seed fact", confidence=0.9)
    return m


def _backdate_all_decisions(db, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db.conn.execute("UPDATE decisions SET created_at = ?", (old,))
    db.conn.commit()


def _pending_block(briefing: str) -> list[str]:
    """Lines of the Outcomes-pending block: header + its indented bullets."""
    lines = briefing.splitlines()
    for i, line in enumerate(lines):
        if "Outcomes pending" in line:
            block = [line]
            for nxt in lines[i + 1:]:
                if nxt.strip().startswith("·"):
                    block.append(nxt)
                else:
                    break
            return block
    return []


def test_inlines_up_to_two_overdue_decisions(mem):
    for i in range(4):
        mem.decide(f"overdue decision number {i}", f"reasoning {i}")
    _backdate_all_decisions(mem.db, 30)
    block = _pending_block(mem.briefing())
    assert block, "Outcomes-pending block missing"
    assert "4 decisions" in block[0]
    bullets = [l for l in block[1:] if "overdue decision number" in l]
    assert len(bullets) == 2  # LIMIT 2 — budget respected


def test_single_overdue_inlines_one(mem):
    mem.decide("lone overdue decision", "reasoning")
    _backdate_all_decisions(mem.db, 30)
    block = _pending_block(mem.briefing())
    assert len([l for l in block if "lone overdue decision" in l]) == 1


def test_no_block_when_nothing_overdue(mem):
    mem.decide("fresh decision", "made just now")
    assert _pending_block(mem.briefing()) == []


def test_decision_with_outcome_not_counted(mem):
    mem.decide("closed decision", "it worked")
    _backdate_all_decisions(mem.db, 30)
    row = mem.db.conn.execute("SELECT id FROM decisions LIMIT 1").fetchone()
    now = datetime.now(timezone.utc).isoformat()
    mem.db.conn.execute(
        "INSERT INTO decision_outcomes (decision_id, outcome, success, recorded_at)"
        " VALUES (?, 'shipped fine', 1, ?)", (row[0], now))
    mem.db.conn.commit()
    assert _pending_block(mem.briefing()) == []
