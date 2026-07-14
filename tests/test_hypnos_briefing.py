"""Tests for the Hypnos "Overnight consolidation" briefing section.

Covers render_hypnos_section() and its integration into briefing():
the section renders one aggregate line (journal rows grouped by action
for the latest batch run) plus up to 2 synthesized/crystallized insight
texts, and renders nothing for old runs, runs before the last clean
close, all-bookkeeping runs, or stores without a hypnos_journal table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from null_memory.memory.briefing_render import render_hypnos_section

HEADER = "Overnight consolidation (Hypnos"


# ── Seeding helpers ──────────────────────────────────────────────────────

def _now_iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _insert_journal(db, run_id: str, stage: str, action: str,
                    fact_id: str | None = None, detail: str | None = None,
                    started_at: str | None = None) -> None:
    """INSERT a journal row directly (handles unified vs legacy schema)."""
    ts = started_at or _now_iso()
    cols = [r[1] for r in db.conn.execute(
        "PRAGMA table_info(hypnos_journal)").fetchall()]
    if "personality" in cols:
        db.conn.execute(
            """INSERT INTO hypnos_journal
               (personality, run_id, started_at, stage, action, fact_id, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (getattr(db, "personality", "atlas"), run_id, ts, stage,
             action, fact_id, detail),
        )
    else:
        db.conn.execute(
            """INSERT INTO hypnos_journal
               (run_id, started_at, stage, action, fact_id, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, ts, stage, action, fact_id, detail),
        )
    db.conn.commit()


def _seed_run(mem, run_id: str = "run-fresh",
              started_at: str | None = None) -> dict:
    """Seed a fake overnight run: 2 decayed, 1 promoted, 1 merged,
    1 crystallize split (with a child fact), 1 synthesized insight."""
    db = mem.db
    ts = started_at or _now_iso()

    synth = mem.learn(
        "[synthesized] Always pin the schema version before migrating",
        confidence=0.85, project="global",
    )
    parent = mem.learn("Verbose parent fact " + "blah " * 70, project="global")
    child = mem.learn(
        "Crystallized child: WAL mode is required for concurrent readers",
        project="global",
    )
    # The tmp-store facts table is the legacy schema; add the unified
    # crystallized_from column the same way migrate_v3 does.
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(facts)").fetchall()]
    if "crystallized_from" not in cols:
        db.conn.execute("ALTER TABLE facts ADD COLUMN crystallized_from TEXT")
    db.conn.execute(
        "UPDATE facts SET crystallized_from = ? WHERE id = ?",
        (parent["id"], child["id"]),
    )
    db.conn.commit()

    _insert_journal(db, run_id, "decay", "archived", "f-old-1",
                    "age=90d conf=0.04", started_at=ts)
    _insert_journal(db, run_id, "decay", "archived_ultra_low", "f-old-2",
                    "conf=0.01", started_at=ts)
    _insert_journal(db, run_id, "tier", "promoted", "f-hot",
                    "contextual->durable: access_count=12", started_at=ts)
    _insert_journal(db, run_id, "live", "consolidate", "f-win",
                    "merged near-duplicate", started_at=ts)
    _insert_journal(db, run_id, "crystallize", "split", parent["id"],
                    f"created=1 ids={child['id']}", started_at=ts)
    _insert_journal(db, run_id, "synthesis", "synthesized", synth["id"],
                    "from 4 facts in global", started_at=ts)
    _insert_journal(db, run_id, "run", "completed",
                    detail="active=10, archived=2", started_at=ts)
    return {"synth": synth, "parent": parent, "child": child}


def _section_block(brief: str) -> str:
    """Extract the Hypnos section (header + insight lines) from a full
    briefing. Asserts the header appears exactly once at output level."""
    lines = brief.splitlines()
    idx = [i for i, ln in enumerate(lines) if HEADER in ln]
    assert len(idx) == 1, f"expected exactly one Hypnos header, got {len(idx)}"
    block = [lines[idx[0]]]
    for ln in lines[idx[0] + 1:]:
        if ln.startswith("    insight: "):
            block.append(ln)
        else:
            break
    return "\n".join(block)


# ── Rendering: counts + insights ─────────────────────────────────────────

class TestHypnosSectionRenders:
    def test_aggregate_counts_in_full_briefing(self, mem):
        _seed_run(mem)
        brief = mem.briefing()
        block = _section_block(brief)  # also asserts exactly-once
        # archived + archived_ultra_low fold into one count
        assert "2 archived" in block
        assert "1 promoted" in block
        assert "1 merged" in block
        assert "1 crystallized" in block
        assert "1 insight synthesized" in block

    def test_synthesized_insight_text_shown_prefix_stripped(self, mem):
        _seed_run(mem)
        block = _section_block(mem.briefing())
        assert "insight: Always pin the schema version before migrating" in block
        # the [synthesized] prefix is stripped inside the section
        assert "[synthesized]" not in block

    def test_crystallized_child_text_shown(self, mem):
        seeded = _seed_run(mem)
        block = _section_block(mem.briefing())
        assert seeded["child"]["fact"][:60] in block

    def test_at_most_two_insights(self, mem):
        run_id = "run-many"
        for i in range(3):
            f = mem.learn(f"[synthesized] Principle number {i} about testing",
                          project="global")
            _insert_journal(mem.db, run_id, "synthesis", "synthesized",
                            f["id"], "from 3 facts")
        block = _section_block(mem.briefing())
        assert block.count("insight: ") == 2

    def test_helper_returns_lines_directly(self, mem):
        _seed_run(mem)
        lines = render_hypnos_section(mem.db)
        assert lines and HEADER in lines[0]
        # compact: 1 header + <=2 insights
        assert len(lines) <= 3

    def test_insight_collision_scoped_to_section(self, mem):
        """The synthesized fact is a real recent fact, so the WARM 'Recent
        context' block may legitimately show its raw text too (with the
        [synthesized] prefix). The section's 'insight:' framing must stay
        unique to the Hypnos block — that's what we assert here."""
        _seed_run(mem)
        brief = mem.briefing()
        assert brief.count("insight: Always pin the schema version") == 1


# ── Absence conditions ───────────────────────────────────────────────────

class TestHypnosSectionAbsent:
    def test_absent_when_no_journal_rows(self, mem):
        mem.learn("Some unrelated fact", project="global")
        assert HEADER not in mem.briefing()

    def test_absent_when_only_old_rows(self, mem):
        _seed_run(mem, run_id="run-stale", started_at=_now_iso(hours_ago=72))
        assert HEADER not in mem.briefing()

    def test_absent_when_run_before_last_close(self, mem):
        _seed_run(mem, run_id="run-seen", started_at=_now_iso(hours_ago=6))
        # A clean close AFTER the run marks it as already surfaced
        mem.db.insert_fingerprint({
            "session_id": "sess-after-run",
            "outcome": "clean",
            "created_at": _now_iso(hours_ago=1),
        })
        mem.db.conn.commit()
        assert HEADER not in mem.briefing()

    def test_present_when_run_after_last_close(self, mem):
        mem.db.insert_fingerprint({
            "session_id": "sess-before-run",
            "outcome": "clean",
            "created_at": _now_iso(hours_ago=10),
        })
        mem.db.conn.commit()
        _seed_run(mem, run_id="run-new", started_at=_now_iso(hours_ago=2))
        assert HEADER in mem.briefing()

    def test_absent_when_run_only_bookkeeping(self, mem):
        run_id = "run-noop"
        _insert_journal(mem.db, run_id, "crystallize", "dryrun_summary",
                        detail="candidates=0 skipped=0")
        _insert_journal(mem.db, run_id, "run", "completed",
                        detail="active=5, archived=0")
        assert HEADER not in mem.briefing()

    def test_absent_for_live_worker_runs(self, mem):
        # Continuous worker reuses one run_id forever — not a sleep cycle
        _insert_journal(mem.db, "live:abc123", "live", "consolidate",
                        "f-1", "merged pair")
        assert HEADER not in mem.briefing()

    def test_legacy_facts_without_crystallized_from_column(self, mem):
        """Pre-unified facts schema: the crystallize-child join must fail
        soft — synthesized insights still render, no crash."""
        cols = [r[1] for r in mem.db.conn.execute(
            "PRAGMA table_info(facts)").fetchall()]
        assert "crystallized_from" not in cols  # fixture is legacy schema
        run_id = "run-legacy"
        synth = mem.learn("[synthesized] Small commits beat big rewrites",
                          project="global")
        _insert_journal(mem.db, run_id, "synthesis", "synthesized",
                        synth["id"], "from 3 facts")
        _insert_journal(mem.db, run_id, "crystallize", "split", "f-parent",
                        "created=2 ids=a,b")
        block = _section_block(mem.briefing())
        assert "1 crystallized" in block
        assert "insight: Small commits beat big rewrites" in block

    def test_no_crash_when_table_missing(self, mem):
        mem.db.conn.execute("DROP TABLE IF EXISTS hypnos_journal")
        mem.db.conn.commit()
        brief = mem.briefing()  # must not raise
        assert HEADER not in brief
        assert render_hypnos_section(mem.db) == []
