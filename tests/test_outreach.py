"""Tests for Phase 4 — Atlas-initiated contact.

Covers:
  - Each evaluator (session_gap, anniversary_window, unresolved_mistake)
  - Cooldown enforcement
  - Daily budget enforcement
  - Pause flag
  - Channel delivery (log guaranteed; macos mocked/env-gated)
  - Seed installer idempotency + default-disabled
  - Outreaches + trigger update on fire
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from null_memory.agent import AgentMemory
from null_memory.migrate_v3 import init_unified_db
from null_memory.outreach import (
    OutreachEvaluator,
    LogChannel,
    MacOSChannel,
    seed_default_triggers,
    DEFAULT_TRIGGERS,
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
    return mem


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── Seeder ────────────────────────────────────────────────────────────────


def test_seed_installs_three_disabled_triggers(unified_agent):
    stats = seed_default_triggers(unified_agent, enable_all=False)
    assert stats["installed"] == 3
    assert stats["skipped_existing"] == 0

    rows = unified_agent.db.conn.execute(
        "SELECT name, enabled FROM outreach_triggers ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["enabled"] == 0 for r in rows)


def test_seed_is_idempotent(unified_agent):
    seed_default_triggers(unified_agent)
    stats = seed_default_triggers(unified_agent)
    assert stats["installed"] == 0
    assert stats["skipped_existing"] == 3


def test_seed_enable_all(unified_agent):
    seed_default_triggers(unified_agent, enable_all=True)
    rows = unified_agent.db.conn.execute(
        "SELECT enabled FROM outreach_triggers"
    ).fetchall()
    assert all(r["enabled"] == 1 for r in rows)


# ── Evaluators ────────────────────────────────────────────────────────────


def _enable(mem, name):
    mem.db.conn.execute(
        "UPDATE outreach_triggers SET enabled=1 WHERE name=?", (name,)
    )
    mem.db.conn.commit()


def test_session_gap_fires_when_stale(unified_agent):
    mem = unified_agent
    seed_default_triggers(mem)
    _enable(mem, "session_gap_3d")
    # Insert an old session fingerprint
    old = _iso(_now() - timedelta(days=5))
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           outcome, created_at) VALUES (?, 'atlas', 'test', 'neutral', ?)""",
        ("stale-session", old),
    )
    mem.db.conn.commit()

    evaluator = OutreachEvaluator(mem, daily_budget=5)
    r = evaluator.evaluate()
    assert r.fired == 1
    assert r.outreaches[0]["kind"] == "session_gap"


def test_session_gap_does_not_fire_when_fresh(unified_agent):
    mem = unified_agent
    seed_default_triggers(mem)
    _enable(mem, "session_gap_3d")
    recent = _iso(_now() - timedelta(hours=2))
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           outcome, created_at) VALUES ('fresh', 'atlas', 'test', 'neutral', ?)""",
        (recent,),
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0


def test_anniversary_fires_within_window(unified_agent):
    mem = unified_agent
    # Put a trigger with an anniversary that's tomorrow
    from null_memory.outreach import OutreachEvaluator
    tomorrow = _now() + timedelta(days=1)
    mem.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('ann_soon', 'anniversary_window', ?, 1, 24, 0.7, ?)""",
        (
            json.dumps({
                "window_days": 2,
                "anniversaries": [
                    {"name": "Test", "month": tomorrow.month,
                     "day": tomorrow.day, "kind": "birthday"},
                ],
            }),
            _iso(_now()),
        ),
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem, daily_budget=5).evaluate()
    assert r.fired == 1
    assert "Test" in r.outreaches[0]["subject"]


def test_anniversary_does_not_fire_outside_window(unified_agent):
    mem = unified_agent
    # Put an anniversary 5 days away with a 2-day window
    future = _now() + timedelta(days=5)
    mem.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('ann_far', 'anniversary_window', ?, 1, 24, 0.7, ?)""",
        (
            json.dumps({
                "window_days": 2,
                "anniversaries": [
                    {"name": "Distant", "month": future.month,
                     "day": future.day, "kind": "birthday"},
                ],
            }),
            _iso(_now()),
        ),
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0
    assert r.skipped_no_candidate == 1


def test_unresolved_mistake_fires_when_stale(unified_agent):
    mem = unified_agent
    seed_default_triggers(mem)
    _enable(mem, "unresolved_mistake_24h")
    # Create a mistake 2 days ago with distinct words
    two_days_ago = _iso(_now() - timedelta(days=2))
    mem.db.conn.execute(
        """INSERT INTO mistakes (mistake, why, project, personality,
           confidence, created_at)
           VALUES ('forgot elephant unicorn paradox', 'distraction',
                   'test', 'atlas', 0.95, ?)""",
        (two_days_ago,),
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem, daily_budget=5).evaluate()
    assert r.fired == 1
    assert "paradox" in r.outreaches[0]["subject"].lower() or \
           "mistake" in r.outreaches[0]["subject"].lower()


def test_unresolved_mistake_skips_if_reflected(unified_agent):
    mem = unified_agent
    seed_default_triggers(mem)
    _enable(mem, "unresolved_mistake_24h")
    two_days_ago = _iso(_now() - timedelta(days=2))
    mem.db.conn.execute(
        """INSERT INTO mistakes (mistake, why, project, personality,
           confidence, created_at)
           VALUES ('forgot elephant unicorn paradox', 'distraction',
                   'test', 'atlas', 0.95, ?)""",
        (two_days_ago,),
    )
    # Reflection that mentions the mistake's distinctive words
    yesterday = _iso(_now() - timedelta(days=1))
    mem.db.conn.execute(
        """INSERT INTO reflections (went_well, missed, do_differently,
           project, personality, created_at)
           VALUES ('slept', 'handled the elephant unicorn one badly',
                   'read the paradox next time', 'test', 'atlas', ?)""",
        (yesterday,),
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem).evaluate()
    # Should NOT fire — overlap ≥ 2 words
    assert r.fired == 0


# ── Cooldown + budget ────────────────────────────────────────────────────


def test_cooldown_blocks_second_fire(unified_agent):
    mem = unified_agent
    seed_default_triggers(mem)
    _enable(mem, "session_gap_3d")
    old = _iso(_now() - timedelta(days=5))
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           outcome, created_at) VALUES ('stale','atlas','t','neutral',?)""",
        (old,),
    )
    mem.db.conn.commit()

    evaluator = OutreachEvaluator(mem, daily_budget=5)
    r1 = evaluator.evaluate()
    assert r1.fired == 1
    r2 = evaluator.evaluate()
    assert r2.fired == 0
    assert r2.skipped_cooldown == 1


def test_daily_budget_caps_fires(unified_agent):
    mem = unified_agent
    # Enable anniversary AND session_gap with stale data
    old = _iso(_now() - timedelta(days=5))
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           outcome, created_at) VALUES ('stale','atlas','t','neutral',?)""",
        (old,),
    )
    tomorrow = _now() + timedelta(days=1)
    # Three triggers that'd all fire
    for i in range(3):
        mem.db.conn.execute(
            """INSERT INTO outreach_triggers (name, kind, payload, enabled,
               cooldown_hours, urgency, created_at)
               VALUES (?, 'anniversary_window', ?, 1, 24, 0.7, ?)""",
            (
                f"ann_{i}",
                json.dumps({
                    "window_days": 2,
                    "anniversaries": [
                        {"name": f"Test{i}", "month": tomorrow.month,
                         "day": tomorrow.day, "kind": "birthday"},
                    ],
                }),
                _iso(_now()),
            ),
        )
    mem.db.conn.commit()

    evaluator = OutreachEvaluator(mem, daily_budget=2)
    r = evaluator.evaluate()
    assert r.fired <= 2  # budget caps at 2


# ── Pause ─────────────────────────────────────────────────────────────────


def test_pause_blocks_all_fires(unified_agent):
    mem = unified_agent
    seed_default_triggers(mem)
    _enable(mem, "anniversary_window_2d")
    mem.db.conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('outreach_paused', '1')"
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0


# ── Channels ──────────────────────────────────────────────────────────────


def test_log_channel_writes_file(tmp_path):
    log_path = tmp_path / "outreaches.log"
    ch = LogChannel(log_path=str(log_path))
    assert ch.send("Test subject", "Test body", 0.5) is True
    assert log_path.exists()
    content = log_path.read_text()
    assert "Test subject" in content
    assert "Test body" in content


def test_macos_channel_inactive_without_env(monkeypatch):
    monkeypatch.delenv("NEBULA_OUTREACH_NOTIFY", raising=False)
    ch = MacOSChannel()
    assert ch.active() is False


# ── Phase 5.2: log rotation ──────────────────────────────────────────────


def test_log_rotates_on_new_day(tmp_path):
    """If the existing log was last written on an earlier UTC day, the
    first write of today renames it to outreaches-<yesterday>.log and
    starts a fresh outreaches.log."""
    import os
    import time
    log_path = tmp_path / "outreaches.log"
    log_path.write_text("yesterday's content\n")
    # Set mtime to 2 days ago
    two_days_ago = time.time() - 2 * 86400
    os.utime(log_path, (two_days_ago, two_days_ago))
    ch = LogChannel(log_path=str(log_path))
    ch.send("Fresh subject", "Fresh body", 0.5)
    # A dated file should exist, and the current log should only have today's write
    dated = sorted(tmp_path.glob("outreaches-*.log"))
    assert len(dated) == 1
    assert "yesterday's content" in dated[0].read_text()
    assert "Fresh subject" in log_path.read_text()
    assert "yesterday's content" not in log_path.read_text()


def test_log_does_not_rotate_same_day(tmp_path):
    log_path = tmp_path / "outreaches.log"
    ch = LogChannel(log_path=str(log_path))
    ch.send("First", "body", 0.5)
    ch.send("Second", "body", 0.5)
    # Both in same file, no rotation
    assert not list(tmp_path.glob("outreaches-*.log"))
    content = log_path.read_text()
    assert "First" in content and "Second" in content


def test_log_prunes_files_older_than_30_days(tmp_path):
    import os
    import time
    log_path = tmp_path / "outreaches.log"
    # Create a very old dated file
    old_name = tmp_path / "outreaches-2020-01-01.log"
    old_name.write_text("ancient history")
    recent_name = (
        tmp_path / f"outreaches-{(datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()}.log"
    )
    recent_name.write_text("recent")
    ch = LogChannel(log_path=str(log_path))
    ch.send("Trigger prune", "body", 0.5)
    assert not old_name.exists()      # pruned
    assert recent_name.exists()       # kept


# ── Force-fire bypasses disabled + cooldown ──────────────────────────────


def test_force_name_bypasses_disabled(unified_agent):
    mem = unified_agent
    seed_default_triggers(mem)  # all disabled
    tomorrow = _now() + timedelta(days=1)
    mem.db.conn.execute(
        "UPDATE outreach_triggers SET payload = ? WHERE name = ?",
        (
            json.dumps({
                "window_days": 2,
                "anniversaries": [
                    {"name": "Forced", "month": tomorrow.month,
                     "day": tomorrow.day, "kind": "birthday"},
                ],
            }),
            "anniversary_window_2d",
        ),
    )
    mem.db.conn.commit()
    evaluator = OutreachEvaluator(mem)
    r = evaluator.evaluate(force_name="anniversary_window_2d")
    assert r.fired == 1
    assert "Forced" in r.outreaches[0]["subject"]


# ── Phase 6.2: per-kind daily caps ───────────────────────────────────────


def test_kind_cap_blocks_second_fire_same_kind_same_day(unified_agent):
    """With daily_cap=1 for session_gap, two matching enabled triggers of
    that kind should only produce one fire in a single evaluate() call."""
    mem = unified_agent
    old = _iso(_now() - timedelta(days=5))
    # Two separate session_gap triggers, both eligible
    for name in ("gap_a", "gap_b"):
        mem.db.conn.execute(
            """INSERT INTO outreach_triggers (name, kind, payload, enabled,
               cooldown_hours, urgency, created_at)
               VALUES (?, 'session_gap', '{"days": 3}', 1, 0, 0.4, ?)""",
            (name, _iso(_now())),
        )
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           outcome, created_at) VALUES ('stale','atlas','t','neutral',?)""",
        (old,),
    )
    mem.db.conn.commit()

    r = OutreachEvaluator(mem, daily_budget=10).evaluate()
    assert r.fired == 1
    assert r.skipped_kind_cap == 1


def test_kind_cap_payload_override_raises_limit(unified_agent):
    """payload.daily_cap overrides the default — set 5 for session_gap and
    both triggers fire."""
    mem = unified_agent
    old = _iso(_now() - timedelta(days=5))
    for name in ("gap_a", "gap_b"):
        mem.db.conn.execute(
            """INSERT INTO outreach_triggers (name, kind, payload, enabled,
               cooldown_hours, urgency, created_at)
               VALUES (?, 'session_gap', '{"days": 3, "daily_cap": 5}', 1, 0, 0.4, ?)""",
            (name, _iso(_now())),
        )
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           outcome, created_at) VALUES ('stale','atlas','t','neutral',?)""",
        (old,),
    )
    mem.db.conn.commit()

    r = OutreachEvaluator(mem, daily_budget=10).evaluate()
    assert r.fired == 2
    assert r.skipped_kind_cap == 0


def test_kind_cap_does_not_apply_to_force_fire(unified_agent):
    """force_name bypasses both cooldown AND kind cap — it's a manual
    override path used by `null outreach test`."""
    mem = unified_agent
    seed_default_triggers(mem)
    tomorrow = _now() + timedelta(days=1)
    mem.db.conn.execute(
        "UPDATE outreach_triggers SET payload = ? WHERE name = ?",
        (
            json.dumps({
                "window_days": 2,
                "anniversaries": [
                    {"name": "Forced", "month": tomorrow.month,
                     "day": tomorrow.day, "kind": "birthday"},
                ],
            }),
            "anniversary_window_2d",
        ),
    )
    mem.db.conn.commit()

    # Even with the kind cap that would block in normal flow, force fires
    evaluator = OutreachEvaluator(mem)
    for _ in range(3):
        r = evaluator.evaluate(force_name="anniversary_window_2d")
        assert r.fired == 1


# ── Phase 6.3: anchor_dormant ────────────────────────────────────────────


def _seed_anchor(mem, fact_id, anchor_type, note, last_accessed=None):
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, confidence, created_at,
                              anchor_type, anchor_note, archived)
           VALUES (?, 'fact text', 1.0, '2026-01-01', ?, ?, 0)""",
        (fact_id, anchor_type, note),
    )
    if last_accessed:
        mem.db.conn.execute(
            """INSERT INTO personality_views (fact_id, personality, last_accessed, access_count)
               VALUES (?, 'atlas', ?, 1)""",
            (fact_id, last_accessed),
        )
    mem.db.conn.commit()


def _install_anchor_dormant_trigger(mem, payload_overrides=None):
    import json as _json
    payload = {"dormant_days": 60}
    payload.update(payload_overrides or {})
    mem.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('anchor_dormant_60', 'anchor_dormant', ?, 1, 0, 0.4, '2026-01-01')""",
        (_json.dumps(payload),),
    )
    mem.db.conn.commit()


def test_anchor_dormant_fires_on_old_anchor(unified_agent):
    mem = unified_agent
    old = _iso(_now() - timedelta(days=90))
    _seed_anchor(mem, "a1", "commitment", "Ship Null by EOY", last_accessed=old)
    _install_anchor_dormant_trigger(mem)
    r = OutreachEvaluator(mem, daily_budget=5).evaluate()
    assert r.fired == 1
    # Phase 6.1 — subject is now question-form, not generic "Dormant anchor"
    subject = r.outreaches[0]["subject"]
    assert "Checking in" in subject


def test_anchor_dormant_skips_loss_anchors_by_default(unified_agent):
    mem = unified_agent
    old = _iso(_now() - timedelta(days=90))
    _seed_anchor(mem, "L", "loss", "Sam", last_accessed=old)
    _install_anchor_dormant_trigger(mem)
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0


def test_anchor_dormant_skips_excluded_candidate_and_uses_next(unified_agent):
    mem = unified_agent
    old = _iso(_now() - timedelta(days=90))
    _seed_anchor(mem, "loss_first", "loss", "Sam", last_accessed=old)
    _seed_anchor(mem, "valid_second", "commitment", "Ship Null", last_accessed=old)
    _install_anchor_dormant_trigger(mem)

    r = OutreachEvaluator(mem, daily_budget=5).evaluate()

    assert r.fired == 1
    assert "anchor_id=valid_second" in r.outreaches[0]["body"]


def test_anchor_dormant_includes_loss_when_explicitly_opted_in(unified_agent):
    mem = unified_agent
    old = _iso(_now() - timedelta(days=90))
    _seed_anchor(mem, "L", "loss", "Sam", last_accessed=old)
    _install_anchor_dormant_trigger(mem, {"exclude_types": []})
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 1


def test_anchor_dormant_does_not_fire_on_fresh_anchor(unified_agent):
    mem = unified_agent
    fresh = _iso(_now() - timedelta(days=10))
    _seed_anchor(mem, "a1", "origin", "fresh", last_accessed=fresh)
    _install_anchor_dormant_trigger(mem)
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0
    assert r.skipped_no_candidate == 1


# ── Phase 6.4: probe_failure ─────────────────────────────────────────────


def _install_probe_failure_trigger(mem):
    mem.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('probe_failure_v1', 'probe_failure', '{}', 1, 0, 0.6, '2026-01-01')"""
    )
    mem.db.conn.commit()


def test_probe_failure_fires_on_regressed_probe(unified_agent):
    mem = unified_agent
    now = _now()
    # Probe that passed 5/6 times historically, failed just now
    mem.db.conn.execute(
        """INSERT INTO probes (question, expected, personality, created_at,
           last_run, last_result, run_count, pass_count)
           VALUES ('what is x?', 'forty two', 'atlas', '2026-01-01',
                   ?, 'fail', 6, 5)""",
        (_iso(now - timedelta(hours=1)),),
    )
    mem.db.conn.commit()
    _install_probe_failure_trigger(mem)
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 1
    assert "Probe failing" in r.outreaches[0]["subject"]


def test_probe_failure_skips_probes_never_passed(unified_agent):
    """A probe that has never passed isn't a regression — it's just broken."""
    mem = unified_agent
    now = _now()
    mem.db.conn.execute(
        """INSERT INTO probes (question, expected, personality, created_at,
           last_run, last_result, run_count, pass_count)
           VALUES ('what is y?', 'seventeen', 'atlas', '2026-01-01',
                   ?, 'fail', 5, 0)""",
        (_iso(now - timedelta(hours=1)),),
    )
    mem.db.conn.commit()
    _install_probe_failure_trigger(mem)
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0


def test_probe_failure_skips_stale_failures(unified_agent):
    """Last run older than 48h is too stale to be actionable."""
    mem = unified_agent
    now = _now()
    mem.db.conn.execute(
        """INSERT INTO probes (question, expected, personality, created_at,
           last_run, last_result, run_count, pass_count)
           VALUES ('what is z?', 'three', 'atlas', '2026-01-01',
                   ?, 'fail', 6, 5)""",
        (_iso(now - timedelta(days=5)),),
    )
    mem.db.conn.commit()
    _install_probe_failure_trigger(mem)
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0


# ── Phase 6.5: contradiction_alert ───────────────────────────────────────


def _install_contradiction_trigger(mem):
    mem.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('contradiction_v1', 'contradiction_alert', '{}', 1, 0, 0.7, '2026-01-01')"""
    )
    mem.db.conn.commit()


def test_contradiction_fires_on_negation_asymmetry(unified_agent):
    """Old fact with no negation, new fact with negation → flagged."""
    mem = unified_agent
    now = _now()
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, project, confidence, created_at)
           VALUES ('old', 'migrations run automatically on startup',
                   'null', 0.9, '2025-01-01')"""
    )
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, project, confidence, created_at)
           VALUES ('new', 'migrations do not run automatically on startup',
                   'null', 0.8, ?)""",
        (_iso(now - timedelta(hours=2)),),
    )
    mem.db.conn.commit()
    _install_contradiction_trigger(mem)
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 1
    assert "Contradiction" in r.outreaches[0]["subject"]


def test_contradiction_skips_benign_differences(unified_agent):
    """Two facts with no negation asymmetry should not fire."""
    mem = unified_agent
    now = _now()
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, project, confidence, created_at)
           VALUES ('old', 'Python uses indentation for blocks',
                   'null', 0.9, '2025-01-01')"""
    )
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, project, confidence, created_at)
           VALUES ('new', 'Python supports typed parameters in annotations',
                   'null', 0.8, ?)""",
        (_iso(now - timedelta(hours=2)),),
    )
    mem.db.conn.commit()
    _install_contradiction_trigger(mem)
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0


# ── Phase 6.1: Memory-referenced composition ─────────────────────────────


def test_anniversary_does_not_fire_day_of_by_default(unified_agent, monkeypatch):
    """Pete's rule: outreach fires day-before, never day-of."""
    import json as _json
    from datetime import date
    mem = unified_agent
    today = _now().date()
    mem.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('ann_today', 'anniversary_window', ?, 1, 0, 0.7, '2025-01-01')""",
        (_json.dumps({
            "window_days": 2,
            "anniversaries": [
                {"name": "Sam", "month": today.month, "day": today.day,
                 "kind": "birthday", "birth_year": 2018},
            ],
        }),),
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem).evaluate()
    assert r.fired == 0


def test_anniversary_fires_day_before_with_age_and_context(unified_agent):
    """Fires tomorrow's birthday. Subject includes age + 'tomorrow'.
    Body references a concrete memory about the subject."""
    import json as _json
    mem = unified_agent
    # Seed a fact about Sam for composer to discover
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, project, confidence, created_at)
           VALUES ('f_kiddo', 'Sam plays baseball and wears #4',
                   'global', 0.9, '2026-03-01')"""
    )
    tomorrow = _now() + timedelta(days=1)
    mem.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('ann_tmr', 'anniversary_window', ?, 1, 0, 0.7, '2025-01-01')""",
        (_json.dumps({
            "window_days": 2,
            "anniversaries": [
                {"name": "Sam", "month": tomorrow.month, "day": tomorrow.day,
                 "kind": "birthday", "birth_year": 2018},
            ],
        }),),
    )
    mem.db.conn.commit()
    r = OutreachEvaluator(mem, daily_budget=5).evaluate()
    assert r.fired == 1
    out = r.outreaches[0]
    # Age should be present (8th by 2026 - 2018)
    assert "8th" in out["subject"]
    assert "tomorrow" in out["subject"].lower()
    # Memory context should appear in body
    assert "baseball" in out["body"].lower() or "#4" in out["body"]


def test_anchor_dormant_uses_specific_memory_detail(unified_agent):
    """Dormant Riley outreach should reference a concrete fact about
    her, not generic 'you haven't touched this'."""
    mem = unified_agent
    old = _iso(_now() - timedelta(days=90))
    _seed_anchor(
        mem, "a_riley", "commitment",
        "Riley — Pete's daughter", last_accessed=old,
    )
    # Seed a specific fact for the composer
    mem.db.conn.execute(
        """INSERT INTO facts (id, fact, project, confidence, created_at)
           VALUES ('f_riley', 'Riley finished gymnastics in March and started track',
                   'global', 0.9, '2026-03-15')"""
    )
    mem.db.conn.commit()
    _install_anchor_dormant_trigger(mem)
    r = OutreachEvaluator(mem, daily_budget=5).evaluate()
    assert r.fired == 1
    body = r.outreaches[0]["body"].lower()
    # Must reference the specific detail
    assert "gymnastics" in body or "track" in body
    # Must NOT contain the banned generic phrase
    assert "haven't touched" not in body

def test_extract_specifics_preserves_decimals_and_urls():
    from null_memory.outreach import _extract_specifics
    facts = [{"fact": "He bought example.com for $1,000, which has a 3.8 GPA."}]
    specifics = _extract_specifics(facts, "Pete")
    joined = " | ".join(specifics)
    assert "example.com" in joined
    assert "$1,000" in joined
    assert "3.8 GPA" in joined


def test_extract_specifics_splits_punctuation_without_spaces():
    from null_memory.outreach import _extract_specifics
    facts = [{
        "fact": "Pete finished gymnastics this spring;"
                "started track this spring,won regionals last weekend"
    }]

    specifics = _extract_specifics(facts, "Pete")
    joined = " | ".join(specifics)

    assert "started track" in joined
    assert "won regionals" in joined


def test_anniversary_malformed_name_degrades_without_error(unified_agent):
    tomorrow = _now() + timedelta(days=1)
    unified_agent.db.conn.execute(
        """INSERT INTO outreach_triggers (name, kind, payload, enabled,
           cooldown_hours, urgency, created_at)
           VALUES ('ann_numeric_name', 'anniversary_window', ?, 1, 0, 0.7, ?)""",
        (
            json.dumps({
                "window_days": 2,
                "anniversaries": [
                    {
                        "name": 123,
                        "month": tomorrow.month,
                        "day": tomorrow.day,
                        "kind": "birthday",
                    },
                ],
            }),
            _iso(_now()),
        ),
    )
    unified_agent.db.conn.commit()

    r = OutreachEvaluator(unified_agent, daily_budget=5).evaluate()

    assert r.errors == 0
    assert r.fired == 1


# ── Phase 7.2 v1: briefing surfaces unacknowledged outreaches ─────────


def test_briefing_surfaces_unacknowledged_outreach(unified_agent):
    mem = unified_agent
    # Need a recent session_fingerprint so the briefing has a "since"
    mem.db.conn.execute(
        """INSERT INTO session_fingerprints (session_id, personality, project,
           outcome, created_at) VALUES ('s1','atlas','t','neutral',?)""",
        (_iso(_now() - timedelta(hours=12)),),
    )
    # One unacked + one acked outreach AFTER that close
    after = _iso(_now() - timedelta(hours=2))
    mem.db.conn.execute(
        """INSERT INTO outreaches (trigger_id, personality, channel, subject,
           body, urgency, delivered, sent_at, acknowledged_at)
           VALUES (NULL, 'atlas', 'log', 'Sam birthday tomorrow', 'body', 0.7, 1, ?, NULL)""",
        (after,),
    )
    mem.db.conn.execute(
        """INSERT INTO outreaches (trigger_id, personality, channel, subject,
           body, urgency, delivered, sent_at, acknowledged_at)
           VALUES (NULL, 'atlas', 'log', 'Already acked', 'body', 0.5, 1, ?, ?)""",
        (after, _iso(_now())),
    )
    mem.db.conn.commit()
    text = mem.briefing()
    assert "Unacknowledged outreaches" in text
    assert "Sam birthday" in text
    assert "Already acked" not in text


def test_briefing_quiet_when_no_unacked(unified_agent):
    mem = unified_agent
    text = mem.briefing()
    assert "Unacknowledged outreaches" not in text
