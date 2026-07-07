"""Briefing rendering — the morning-briefing string formatter.

Extracted from agent.py (P2 god-object split). Named briefing_render
(not briefing) to avoid colliding with any future package-root briefing
module. Contains BriefingRenderMixin.briefing(): the HOT (since last
close) -> WARM (last 7d) -> EVERGREEN report assembled from the DB.

Mixed into AgentMemory; relies on the host's db / recall / detect_gaps /
config / load_exemplars / _identity_drift_line / _current_session
attributes.

Personality scoping (issue #19 — multiworker-in-a-box)
------------------------------------------------------
A unified store can host multiple personalities (e.g. Atlas + Athena on
one machine, one trust domain). The briefing splits tables two ways:

* **Shared knowledge plane** — ``facts`` (including anchors, core
  identity facts, recent context) and ``doc_claims``. These have no
  personality column BY DESIGN: within one store everything lives in the
  same trust domain, so knowledge is shared across personalities. This
  matches the rest of the codebase (recall, identity_payload anchors,
  fact counts all read facts unscoped).
* **Personality-scoped rows** — ``decisions``, ``decision_feed``,
  ``mistakes``, ``reflections``, ``exemplars``, ``probes``,
  ``evaluations``, ``session_fingerprints``, ``session_verifications``,
  ``hypnos_journal``, ``outreaches``. One personality's briefing must
  never surface another's work product or identity signals.

Every query here against a scoped table carries the predicate from
``_pscope`` (briefing-local) or ``NullDB._personality_predicate``
(db-level helpers). On legacy per-personality stores (single
personality by construction; the column may not exist) the predicate
collapses to nothing, so old stores keep briefing unchanged.
"""

from __future__ import annotations

import random

from null_memory.coherence import WARN_THRESHOLD


def _pscope(db, column: str = "personality") -> tuple[str, tuple]:
    """Personality predicate for briefing queries against scoped tables.

    Returns (" AND <column> = ?", (personality,)) on unified stores —
    where the structural heal guarantees the column exists — and
    ("", ()) on legacy per-personality stores, which are
    single-personality by construction and may lack the column entirely.
    The fragment composes after an existing WHERE condition.
    """
    if getattr(db, "unified", False):
        return (
            f" AND {column} = ?",
            (getattr(db, "personality", "atlas") or "atlas",),
        )
    return "", ()


def render_identity_coherence(db, personality: str) -> list[str]:
    """Compact identity-coherence headline from session_verifications.

    Each MCP-server boot persists a coherence score (current identity
    payload vs the historical centroid). Surfacing it makes "am I still
    me?" a first-class signal instead of a buried stderr line.

    Render rules (signal, not noise):
      - no rows / cold-start (score IS NULL)  -> []  (silent)
      - latest score present                   -> one line, with a small
        trend when prior scored boots exist
      - latest score low (< WARN_THRESHOLD)    -> drift warning line
    Defensive: any missing table/column -> [] (schema-incomplete stores
    must still brief).
    """
    try:
        rows = db.conn.execute(
            """SELECT coherence_score, verified, sample_size
               FROM session_verifications
               WHERE personality = ? AND coherence_score IS NOT NULL
               ORDER BY id DESC LIMIT 3""",
            (personality,),
        ).fetchall()
    except Exception:  # noqa: BLE001 — missing table/column on old stores
        return []
    if not rows:
        return []
    score, verified, sample = rows[0][0], bool(rows[0][1]), rows[0][2] or 0
    trend = ""
    if len(rows) >= 2:
        # oldest -> newest reads naturally
        path = "→".join(f"{r[0]:.2f}" for r in reversed(rows))
        trend = f"; trend {path}"
    if score < WARN_THRESHOLD:
        return [f"  ⚠ Identity drift: coherence {score:.2f} vs "
                f"{sample}-session baseline{trend} — am I still me? "
                f"Review recent voice/behavior before proceeding."]
    mark = "✓" if verified else ""
    return [f"  Identity coherence: {score:.2f} {mark}".rstrip()
            + f" (baseline n={sample}{trend})"]


def render_hypnos_section(db) -> list[str]:
    """Compact "overnight consolidation" lines for the briefing.

    Surfaces the latest batch Hypnos run (sleep-cycle maintenance) that
    happened since the last clean close — capped at 24h so a store that
    never closes cleanly doesn't resurface the same run forever. One
    aggregate line plus up to 2 synthesized/crystallized insight texts.

    Returns [] when: no hypnos_journal table (pre-Hypnos store), no run
    since the boundary, or the run did nothing notable. Never raises.
    """
    from datetime import datetime, timedelta, timezone

    scope_sql, scope_params = _pscope(db)
    try:
        # Boundary: later of (last clean/neutral close, now-24h). Same
        # session_fingerprints shape the outreach block uses, so a run
        # already seen before a close doesn't resurface next morning.
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        try:
            last_close = db.conn.execute(
                f"""SELECT MAX(created_at) FROM session_fingerprints
                   WHERE outcome IN ('clean', 'neutral'){scope_sql}""",
                scope_params,
            ).fetchone()
            if last_close and last_close[0]:
                since = max(since, last_close[0])
        except Exception:
            pass  # no fingerprints table — 24h window alone is fine

        # Latest batch run with activity since the boundary. live:* runs
        # are excluded — the continuous worker reuses one run_id forever,
        # so it's a perpetual "run", not a discrete sleep cycle.
        row = db.conn.execute(
            f"""SELECT run_id, started_at FROM hypnos_journal
               WHERE started_at > ? AND run_id NOT LIKE 'live:%'{scope_sql}
               ORDER BY id DESC LIMIT 1""",
            (since, *scope_params),
        ).fetchone()
        if not row:
            return []
        run_id, started_at = row[0], row[1]

        counts: dict[str, int] = {
            r[0]: r[1]
            for r in db.conn.execute(
                f"""SELECT action, COUNT(*) FROM hypnos_journal
                   WHERE run_id = ?{scope_sql} GROUP BY action LIMIT 50""",
                (run_id, *scope_params),
            ).fetchall()
        }
    except Exception:
        return []  # pre-Hypnos / legacy store: no table — no section

    # Aggregate line. Bookkeeping actions (completed, dryrun_*,
    # kill_switch_hit) are deliberately not counted; doc_audit actions are
    # skipped because the doc-claim refutations block already owns that
    # signal — repeating it here would duplicate content.
    parts: list[str] = []
    archived = counts.get("archived", 0) + counts.get("archived_ultra_low", 0)
    if archived:
        parts.append(f"{archived} archived")
    if counts.get("promoted"):
        parts.append(f"{counts['promoted']} promoted")
    if counts.get("demoted"):
        parts.append(f"{counts['demoted']} demoted")
    boosted = counts.get("boosted", 0) + counts.get("reflection_boost", 0)
    if boosted:
        parts.append(f"{boosted} boosted")
    if counts.get("linked"):
        parts.append(f"{counts['linked']} linked")
    if counts.get("consolidate"):
        parts.append(f"{counts['consolidate']} merged")
    if counts.get("split"):
        parts.append(f"{counts['split']} crystallized")
    if counts.get("synthesized"):
        n = counts["synthesized"]
        parts.append(f"{n} insight{'s' if n != 1 else ''} synthesized")
    if not parts:
        return []  # run happened but nothing notable — stay quiet

    try:
        from null_memory.hypnos import _age_str
        when = f", {_age_str(started_at)}"
    except Exception:
        when = ""
    out = [f"\n  Overnight consolidation (Hypnos{when}): " + ", ".join(parts)]

    # Up to 2 insight texts: synthesized facts first (journal fact_id is
    # the new fact), then crystallized children (journal fact_id is the
    # archived parent; children carry crystallized_from = parent id).
    insights: list[str] = []
    hj_scope_sql, hj_scope_params = _pscope(db, "hj.personality")
    try:
        rows = db.conn.execute(
            f"""SELECT f.fact FROM hypnos_journal hj
               JOIN facts f ON f.id = hj.fact_id
               WHERE hj.run_id = ? AND hj.action = 'synthesized'{hj_scope_sql}
               ORDER BY hj.id LIMIT 2""",
            (run_id, *hj_scope_params),
        ).fetchall()
        insights.extend(r[0] for r in rows if r[0])
    except Exception:
        pass
    if len(insights) < 2:
        try:
            rows = db.conn.execute(
                f"""SELECT f.fact FROM hypnos_journal hj
                   JOIN facts f ON f.crystallized_from = hj.fact_id
                   WHERE hj.run_id = ? AND hj.action = 'split'{hj_scope_sql}
                     AND f.archived = 0 AND f.forgotten = 0
                   ORDER BY hj.id LIMIT 2""",
                (run_id, *hj_scope_params),
            ).fetchall()
            insights.extend(r[0] for r in rows if r[0])
        except Exception:
            pass  # legacy facts schema: no crystallized_from column

    for text in insights[:2]:
        text = text.removeprefix("[synthesized] ")
        out.append(f"    insight: {text[:100]}")
    return out


class BriefingRenderMixin:
    """Morning-briefing formatting for AgentMemory."""

    def briefing(self, project: str | None = None) -> str:
        """Generate a morning briefing.

        Structure: HOT (since last close) → WARM (last 7d) → EVERGREEN.
        Anchors are NOT included here — they're loaded into the system
        prompt by the SessionStart hook (Phase A on-boot identity payload).
        Repeating them in the briefing was redundant and pushed the
        actually-fresh "what was happening" content below the fold.
        """
        lines = [f"[Null] {self.name} ready to work."]

        # Personality scoping for raw queries below (issue #19). DB helper
        # methods (count_*, get_reflections, get_decisions_with_outcomes,
        # get_decision_feed, ...) scope internally.
        scope_sql, scope_params = _pscope(self.db)

        # Header counts (single line — context, not signal). Facts are the
        # shared knowledge plane (unscoped); mistakes/decisions are this
        # personality's own.
        fact_count = self.db.count_facts()
        mistake_count = self.db.count_mistakes()
        decision_count = self.db.count_decisions()
        lines.append(f"  {fact_count} facts, {mistake_count} mistakes, {decision_count} decisions.")

        # Identity-coherence headline — "am I still me?" as a first-class
        # signal. Silent on cold-start stores (no scored boots yet). A drift
        # warning here must also disable adaptive quiet below — it lands in
        # the header, not in warm_lines where the other drift check looks.
        coherence_lines = render_identity_coherence(self.db, getattr(self, "personality", "atlas"))
        lines.extend(coherence_lines)
        coherence_drift = any("Identity drift" in line for line in coherence_lines)

        # Gap/crash warnings — always before HOT (a crash is a signal you
        # need before anything else)
        gaps = self.detect_gaps()
        if gaps:
            crash = gaps.get("prior_crash")
            if crash is not None:
                lines.append(f"  WARNING: Previous session CRASHED (started {crash.started_at[:19]}). "
                             f"{crash.facts_created} facts were saved before crash.")
            age_hours = gaps.get("last_commit_age_hours")
            if age_hours is not None and age_hours > 24:
                days = age_hours / 24
                lines.append(f"  WARNING: Last memory commit was {days:.1f} days ago. "
                             f"Run null_catchup to sync any missed work.")
            if gaps.get("has_uncommitted"):
                lines.append("  NOTE: Uncommitted memory changes found (prior session may not have closed cleanly).")

        # Multi-instance fragmentation warning — one line, only when >1 live
        multi_live = self.multi_instance_warning()
        if multi_live:
            lines.append(f"  {multi_live}")

        # Adaptive briefing — when enabled, optional sections (warm + evergreen)
        # are suppressed as long as nothing looks off. "Off" = prior crash,
        # mistakes logged recently, or drift detected.
        adaptive = self.config.get("adaptive_briefing", False)
        had_crash = bool(gaps and gaps.get("prior_crash"))
        recent_mistakes_24h = self.db.conn.execute(
            f"""SELECT COUNT(*) FROM mistakes
               WHERE created_at >= datetime('now', '-1 day')
                 AND archived = 0{scope_sql}""",
            scope_params,
        ).fetchone()[0]
        adaptive_quiet = False  # set after drift check below

        # ── HOT: Since last close ─────────────────────────────────────────

        hot_lines: list[str] = []

        # Event-sourced sync (issue #20 Phase B) — one line when the poke
        # loop replayed fresh remote events, plus exchange signals (foreign
        # repo pushes → pull recommended, advisory claims, pending queries).
        # Defensive: stores without the meta keys render nothing.
        try:
            from null_memory.poke import render_sync_lines
            hot_lines.extend(render_sync_lines(self.db))
        except Exception:
            pass
        try:
            from null_memory.exchange import (
                exchange_briefing_lines, own_stream_name,
            )
            import os as _os
            own = own_stream_name(
                _os.path.dirname(self.db.db_path),
                getattr(self, "personality", "atlas"), create=False)
            hot_lines.extend(exchange_briefing_lines(self.db, own_stream=own))
        except Exception:
            pass

        # Doc-claim refutations — load-bearing (the "stale handoff trusted
        # as truth" class of bug). Show all refutations every time.
        # doc_claims is part of the shared knowledge plane (docs on disk
        # belong to the whole box) — deliberately NOT personality-scoped.
        try:
            doc_rows = self.db.conn.execute(
                """SELECT source_path, claim_text, refute_evidence
                   FROM doc_claims WHERE status = 'refuted'
                   ORDER BY last_verified_at DESC LIMIT 5"""
            ).fetchall()
            if doc_rows:
                hot_lines.append("")
                hot_lines.append(f"⚠ Doc-claim refutations: {len(doc_rows)} (top 5)")
                for r in doc_rows:
                    src = r[0].rsplit("/", 1)[-1] if r[0] else "?"
                    claim = (r[1] or "")[:80]
                    why = (r[2] or "")[:100]
                    hot_lines.append(f"  [{src}] \"{claim}\" — {why}")
            stale_count = self.db.conn.execute(
                "SELECT COUNT(*) FROM doc_claims WHERE status = 'stale'"
            ).fetchone()[0]
            if stale_count:
                hot_lines.append(f"  ({stale_count} stale doc claims tracked)")
        except Exception:
            pass  # pre-v21 DB — table not present yet

        # Phase 7.2 v1 — unacknowledged outreaches since last clean close
        try:
            last_close = self.db.conn.execute(
                f"""SELECT MAX(created_at) FROM session_fingerprints
                   WHERE outcome IN ('clean', 'neutral'){scope_sql}""",
                scope_params,
            ).fetchone()
            since = (last_close[0] if last_close and last_close[0] else "2000-01-01")
            unack = self.db.conn.execute(
                f"""SELECT id, subject, sent_at, urgency
                   FROM outreaches
                   WHERE acknowledged_at IS NULL AND delivered = 1
                     AND sent_at > ?{scope_sql}
                   ORDER BY urgency DESC, sent_at DESC LIMIT 6""",
                (since, *scope_params),
            ).fetchall()
            if unack:
                hot_lines.append(f"\n  Unacknowledged outreaches since last close ({len(unack)}):")
                for row in unack:
                    when = (row[2] or "")[:16]
                    subj = (row[1] or "(no subject)")[:80]
                    hot_lines.append(f"    [{(row[3] or 0):.2f}] {when}  {subj}")
                hot_lines.append(
                    "  Acknowledge in Nebula → triggers panel, or "
                    "`null outreach digest` for the full picture."
                )
        except Exception:
            pass

        # Hypnos "sleeping on it" — what the latest overnight maintenance
        # run did (since last close, capped at 24h). Renders nothing when
        # there's no new run or the run made no changes.
        hot_lines.extend(render_hypnos_section(self.db))

        # Today's mistakes (last 24h) — separated from older mistakes which
        # go in WARM. A fresh mistake is a top-of-mind concern.
        try:
            today_mistakes = self.db.conn.execute(
                f"""SELECT mistake, why FROM mistakes
                   WHERE created_at >= datetime('now', '-1 day')
                     AND archived = 0{scope_sql}
                   ORDER BY created_at DESC LIMIT 3""",
                scope_params,
            ).fetchall()
            if today_mistakes:
                hot_lines.append(f"\n  Recent mistakes (last 24h, {len(today_mistakes)}):")
                for m in today_mistakes:
                    why = (m[1] or "")[:60]
                    hot_lines.append(f"    !! {m[0][:80]} — {why}")
        except Exception:
            pass

        # Last session's takeaway (one-liner — full reflection lives in DB)
        reflections = self.db.get_reflections()
        if reflections:
            last = reflections[-1]
            went = (last.get("went_well") or "")[:70]
            missed = (last.get("missed") or "")[:70]
            do_diff = (last.get("do_differently") or "")[:70]
            if any([went, missed, do_diff]):
                hot_lines.append("\n  Last session's takeaway:")
                if went: hot_lines.append(f"    + {went}")
                if missed: hot_lines.append(f"    - {missed}")
                if do_diff: hot_lines.append(f"    > {do_diff}")

        # Momentum — planned next + blocked
        try:
            from null_memory.wakeup import load_momentum
            momentum = load_momentum(self.agent_dir)
            if momentum:
                next_action = momentum.get("next_action")
                if next_action:
                    hot_lines.append(f"\n  Planned next: {next_action}")
                    action_facts = self.recall(
                        next_action, project=project, limit=2, include_mistakes=False,
                    )
                    for af in action_facts:
                        if af.get("_type") == "fact":
                            hot_lines.append(f"    [{af.get('confidence', 0.5):.0%}] {af['fact'][:100]}")
                blocked = momentum.get("blocked_on")
                if blocked:
                    hot_lines.append(f"  Blocked: {blocked}")
        except Exception:
            pass

        # Today's decisions (last 24h)
        try:
            today_decisions = self.db.conn.execute(
                f"""SELECT decision FROM decisions
                   WHERE created_at >= datetime('now', '-1 day'){scope_sql}
                   ORDER BY created_at DESC LIMIT 5""",
                scope_params,
            ).fetchall()
            if today_decisions:
                hot_lines.append(f"\n  Decisions today ({len(today_decisions)}):")
                for d in today_decisions:
                    hot_lines.append(f"    · {d[0][:90]}")
        except Exception:
            pass

        # Outcomes pending — visibility on stale decisions, NOT enforcement.
        # If you ignore this line entirely for a month, escalate to
        # enforcement. For now: surface the wishlist length.
        try:
            d_scope_sql, d_scope_params = _pscope(self.db, "d.personality")
            stale_decisions = self.db.conn.execute(
                f"""SELECT COUNT(*) FROM decisions d
                   LEFT JOIN decision_outcomes do ON d.id = do.decision_id
                   WHERE d.created_at < datetime('now', '-7 days')
                     AND do.id IS NULL{d_scope_sql}""",
                d_scope_params,
            ).fetchone()[0]
            if stale_decisions:
                hot_lines.append(
                    f"\n  Outcomes pending: {stale_decisions} decisions >7d "
                    f"without an outcome — null_outcome to close."
                )
                # Inline a rotating sample so the count is actionable —
                # RANDOM() varies the picks per session instead of pinning
                # the same stalest items forever.
                sample = self.db.conn.execute(
                    f"""SELECT d.decision FROM decisions d
                       LEFT JOIN decision_outcomes do ON d.id = do.decision_id
                       WHERE d.created_at < datetime('now', '-7 days')
                         AND do.id IS NULL{d_scope_sql}
                       ORDER BY RANDOM() LIMIT 2""",
                    d_scope_params,
                ).fetchall()
                for (text,) in sample:
                    hot_lines.append(f"    · {text[:90]}")
        except Exception:
            pass

        if hot_lines:
            lines.extend(hot_lines)

        # ── WARM: Last 7 days ─────────────────────────────────────────────

        warm_lines: list[str] = []

        # Identity drift — informational, not a hot signal unless drift
        # actually detected (which the line itself communicates).
        if getattr(self.db, "unified", False):
            try:
                drift_line = self._identity_drift_line()
                if drift_line:
                    warm_lines.append(f"\n  {drift_line}")
            except Exception:
                pass

        # Drift might flag adaptive_quiet off; recompute now that drift_line
        # has been collected into warm.
        if adaptive:
            warm_str = "\n".join(warm_lines)
            drift_warns = "drift detected" in warm_str or coherence_drift
            adaptive_quiet = not (drift_warns or had_crash or recent_mistakes_24h > 0)

        # Project-relevant facts (top 5 — recall handles ranking).
        # Facts are the shared knowledge plane — deliberately unscoped.
        if project:
            relevant = self.recall(project, project=project, limit=5, include_mistakes=False)
            if not relevant:
                proj_lower = project.strip().lower()
                rows = self.db.conn.execute(
                    """SELECT * FROM facts
                       WHERE (project = ? OR project = 'global')
                         AND forgotten = 0 AND archived = 0 AND superseded_by IS NULL
                       ORDER BY created_at DESC LIMIT 5""",
                    (proj_lower,),
                ).fetchall()
                relevant = [dict(r) for r in rows]
        else:
            rows = self.db.conn.execute(
                """SELECT * FROM facts
                   WHERE forgotten = 0 AND archived = 0 AND superseded_by IS NULL
                   ORDER BY created_at DESC LIMIT 5"""
            ).fetchall()
            relevant = [dict(r) for r in rows]
        if relevant:
            warm_lines.append("\n  Recent context:")
            for entry in relevant:
                conf = entry.get("confidence", 0.5)
                warm_lines.append(f"    [{conf:.0%}] {entry['fact'][:100]}")

        # Older mistakes (>24h, last 7d) — context, not hot signal
        try:
            older_mistakes = self.db.conn.execute(
                f"""SELECT mistake, why FROM mistakes
                   WHERE created_at < datetime('now', '-1 day')
                     AND created_at >= datetime('now', '-7 days')
                     AND archived = 0{scope_sql}
                   ORDER BY created_at DESC LIMIT 3""",
                scope_params,
            ).fetchall()
            project_filter = (project.strip().lower() if project else None)
            if older_mistakes:
                warm_lines.append(f"\n  Earlier this week ({len(older_mistakes)} mistakes):")
                for m in older_mistakes:
                    why = (m[1] or "")[:50]
                    warm_lines.append(f"    !! {m[0][:80]} — {why}")
        except Exception:
            pass

        # Recent decisions w/ outcome track record
        decisions_with_outcomes = self.db.get_decisions_with_outcomes(
            project=project, limit=10,
        )
        if decisions_with_outcomes:
            total_with_outcome = 0
            successes = 0
            for d in decisions_with_outcomes:
                outcome_str = d.get("outcome_successes")
                if outcome_str:
                    for val in str(outcome_str).split(","):
                        val = val.strip()
                        if val == "1":
                            successes += 1
                            total_with_outcome += 1
                        elif val == "0":
                            total_with_outcome += 1
            warm_lines.append("\n  Recent decisions:")
            if total_with_outcome >= 3:
                rate = successes / total_with_outcome if total_with_outcome else 0
                warm_lines.append(f"    Track record: {successes}/{total_with_outcome} succeeded ({rate:.0%})")
            for d in decisions_with_outcomes[:3]:
                text = d["decision"][:80]
                outcome_str = d.get("outcome_successes", "")
                if outcome_str and "1" in str(outcome_str):
                    warm_lines.append(f"    + {text}")
                elif outcome_str and "0" in str(outcome_str):
                    warm_lines.append(f"    x {text}")
                else:
                    warm_lines.append(f"    {text}")

        # Cross-instance decisions
        try:
            sid = self._current_session_id()
            feed = self.db.get_decision_feed(
                project=project, exclude_session=sid, limit=5,
            )
            if feed:
                warm_lines.append("\n  Decisions from other sessions:")
                for d in feed[:3]:
                    status = d.get("status", "provisional")
                    sid_short = d.get("session_id", "?")[:8]
                    warm_lines.append(f"    [{status}] {d['decision'][:80]} ({sid_short})")
        except Exception:
            pass

        if warm_lines and not adaptive_quiet:
            lines.extend(warm_lines)

        # ── EVERGREEN: Reference ──────────────────────────────────────────

        evergreen_lines: list[str] = []

        # Continuity probe pass rate — diagnostic, evergreen
        if getattr(self.db, "unified", False):
            try:
                probe_row = self.db.conn.execute(
                    f"""SELECT COUNT(*) AS total,
                              SUM(CASE WHEN last_result LIKE 'pass%' THEN 1 ELSE 0 END) AS passed
                       FROM probes
                       WHERE probe_type = 'continuity' AND last_run IS NOT NULL{scope_sql}""",
                    scope_params,
                ).fetchone()
                if probe_row and probe_row[0]:
                    total = probe_row[0]
                    passed = probe_row[1] or 0
                    pct = passed * 100 // total
                    evergreen_lines.append(
                        f"\n  Continuity probes: {passed}/{total} passed ({pct}%)"
                    )
            except Exception:
                pass

        # Core identity facts (top 5 most-accessed) — evergreen reference.
        # Facts (incl. core tier) ride the shared knowledge plane — unscoped.
        core_facts = self.db.conn.execute(
            """SELECT fact, confidence FROM facts
               WHERE tier = 'core' AND forgotten = 0 AND archived = 0 AND superseded_by IS NULL
               ORDER BY access_count DESC LIMIT 5"""
        ).fetchall()
        if core_facts:
            evergreen_lines.append(f"\n  Core identity ({len(core_facts)}):")
            for cf in core_facts:
                evergreen_lines.append(f"    [{cf[1]:.0%}] {cf[0][:100]}")

        # Similar past sessions (fingerprint match)
        try:
            from null_memory.fingerprint import (
                SessionFingerprint, find_similar_sessions, format_similar_sessions,
            )
            if self._current_session:
                current_fp = SessionFingerprint(
                    session_id=self._current_session.session_id,
                    project=project or "global",
                    facts_count=self._current_session.facts_created,
                    decisions_count=self._current_session.decisions_created,
                    mistakes_count=self._current_session.mistakes_created,
                )
                matches = find_similar_sessions(self, current_fp, limit=2)
                sim_lines = format_similar_sessions(matches)
                if sim_lines:
                    evergreen_lines.append("")
                    evergreen_lines.extend(f"  {line}" for line in sim_lines)
        except Exception:
            pass

        # Calibration examples (3 random)
        exemplars = self.load_exemplars()
        if exemplars:
            samples = random.sample(exemplars, min(3, len(exemplars)))
            evergreen_lines.append(f"\n  Calibration examples ({len(exemplars)} total):")
            for ex in samples:
                user_text = ex.get("user_text", ex.get("pete", ""))
                evergreen_lines.append(f"    User: \"{user_text[:60]}\"")
                evergreen_lines.append(f"    {self.name}: {ex['calibration'][:80]}")

        # Questions for Pete
        try:
            from null_memory.wakeup import load_simmering
            simmering = load_simmering(self.agent_dir)
            calibration_qs = [
                s for s in simmering
                if s.get("category") == "calibration" and not s.get("resolved")
            ]
            if calibration_qs:
                calibration_qs.sort(
                    key=lambda s: (s.get("last_surfaced") or "", s.get("added", "")),
                )
                evergreen_lines.append(f"\n  Questions for Pete ({len(calibration_qs)} pending):")
                for q in calibration_qs[:3]:
                    evergreen_lines.append(f"    ? {q['question'][:90]}")
        except Exception:
            pass

        if evergreen_lines and not adaptive_quiet:
            lines.extend(evergreen_lines)

        return "\n".join(lines)
