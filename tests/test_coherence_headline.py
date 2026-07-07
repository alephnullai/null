"""Tests for the identity-coherence briefing headline.

Each MCP boot persists a coherence score to session_verifications; the
briefing now surfaces it ("am I still me?" as a first-class signal).
Silent on cold-start; warns on drift; defensive on old schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from null_memory.agent import AgentMemory
from null_memory.coherence import WARN_THRESHOLD
from null_memory.memory.briefing_render import render_identity_coherence


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    m = AgentMemory(agent_dir=str(tmp_path))
    m.learn("seed fact", confidence=0.9)
    return m


def _ensure_table(db):
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS session_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            personality TEXT NOT NULL,
            boot_time TEXT NOT NULL,
            coherence_score REAL,
            verified INTEGER DEFAULT 0,
            sample_size INTEGER DEFAULT 0,
            identity_payload_hash TEXT,
            identity_model TEXT,
            created_at TEXT NOT NULL
        )""")
    db.conn.commit()


def _insert(db, score, verified=1, sample=10):
    now = datetime.now(timezone.utc).isoformat()
    db.conn.execute(
        """INSERT INTO session_verifications
           (session_id, personality, boot_time, coherence_score, verified,
            sample_size, created_at)
           VALUES (?, 'atlas', ?, ?, ?, ?, ?)""",
        ("s", now, score, verified, sample, now),
    )
    db.conn.commit()


def test_silent_when_no_rows(mem):
    _ensure_table(mem.db)
    assert render_identity_coherence(mem.db, "atlas") == []


def test_silent_when_table_missing(mem):
    # old/legacy store: no session_verifications at all -> no crash, no line
    mem.db.conn.execute("DROP TABLE IF EXISTS session_verifications")
    mem.db.conn.commit()
    assert render_identity_coherence(mem.db, "atlas") == []


def test_headline_with_score(mem):
    _ensure_table(mem.db)
    _insert(mem.db, 0.92, verified=1, sample=12)
    lines = render_identity_coherence(mem.db, "atlas")
    assert len(lines) == 1
    assert "0.92" in lines[0]
    assert "n=12" in lines[0]


def test_trend_when_multiple_boots(mem):
    _ensure_table(mem.db)
    for s in (0.90, 0.91, 0.93):
        _insert(mem.db, s)
    (line,) = render_identity_coherence(mem.db, "atlas")
    # headline shows the latest score; trend reads oldest -> newest
    assert line.strip().startswith("Identity coherence: 0.93")
    trend = line[line.index("trend"):]
    assert trend.index("0.90") < trend.index("0.91") < trend.index("0.93")


def test_drift_warning_below_threshold(mem):
    _ensure_table(mem.db)
    _insert(mem.db, 0.45, verified=0, sample=8)
    assert 0.45 < WARN_THRESHOLD  # fixture stays meaningful if the bar moves
    (line,) = render_identity_coherence(mem.db, "atlas")
    assert "drift" in line.lower()
    assert "0.45" in line


def test_threshold_boundary_comes_from_coherence_module(mem):
    """The render condition is WARN_THRESHOLD itself, not a hardcoded copy:
    a score AT the threshold is a plain headline, just below it warns."""
    _ensure_table(mem.db)
    _insert(mem.db, WARN_THRESHOLD, verified=0, sample=8)
    (line,) = render_identity_coherence(mem.db, "atlas")
    assert "drift" not in line.lower()
    _insert(mem.db, WARN_THRESHOLD - 0.01, verified=0, sample=8)
    (line,) = render_identity_coherence(mem.db, "atlas")
    assert "drift" in line.lower()


def test_briefing_includes_headline(mem):
    _ensure_table(mem.db)
    _insert(mem.db, 0.88, verified=1, sample=5)
    out = mem.briefing()
    assert "Identity coherence: 0.88" in out


def test_briefing_clean_on_cold_start(mem):
    _ensure_table(mem.db)
    out = mem.briefing()
    assert "Identity coherence" not in out
    assert "drift" not in out.lower()


def test_coherence_drift_disables_adaptive_quiet(mem):
    """The drift warning lands in the briefing HEADER, not the WARM lines
    the original adaptive check scans — it must still disable suppression
    so the full briefing renders when identity looks off."""
    _ensure_table(mem.db)
    mem.config["adaptive_briefing"] = True

    # Healthy coherence, nothing else off -> quiet: warm content suppressed.
    _insert(mem.db, 0.92, verified=1, sample=10)
    quiet_out = mem.briefing()
    assert "Recent context:" not in quiet_out

    # Drift -> NOT quieted: the same warm content now renders.
    _insert(mem.db, WARN_THRESHOLD - 0.15, verified=0, sample=10)
    drift_out = mem.briefing()
    assert "Identity drift" in drift_out
    assert "Recent context:" in drift_out
