"""Handler functions for Null MCP tools."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from null_memory.agent import AgentMemory

# Auto-close timeout: if no tool call arrives within this many seconds,
# the session is closed cleanly to prevent false "crash" warnings.
_AUTO_CLOSE_TIMEOUT = 30 * 60  # 30 minutes


@dataclass
class NullHandlers:
    """Stateful handler set for Null MCP tools."""

    agent_dir: str
    _memory: AgentMemory | None = field(default=None, repr=False)
    _session_started: bool = field(default=False, repr=False)
    _auto_close_timer: threading.Timer | None = field(default=None, repr=False)

    @property
    def memory(self) -> AgentMemory:
        if self._memory is None:
            personality = self._infer_personality(self.agent_dir)
            self._memory = AgentMemory.load(
                self.agent_dir, personality=personality, transport="mcp")
        return self._memory

    @staticmethod
    def _infer_personality(agent_dir: str) -> str:
        """Extract personality from agent_dir path — shared logic in
        personality.infer_personality (the CLI and daemon use the same
        resolver, so all entry points attribute writes identically)."""
        from null_memory.personality import infer_personality
        return infer_personality(agent_dir)

    def _ensure_session(self, project: str = "global") -> None:
        """Lazily start a session on first tool call."""
        if not self._session_started:
            self.memory.start_session(project=project)
            self._session_started = True
        elif self.memory._current_session is not None:
            self.memory._current_session.touch()
        # Presence heartbeat — piggybacked on the per-tool-call session
        # touch (throttled in-process to ≤1 write/min; no timer thread).
        self.memory.touch_instance(
            project=None if project == "global" else project)
        self._reset_auto_close_timer()

    def _drift_prefix(self) -> str:
        """Return a mid-session drift warning prefix (once, then cleared), or ''.

        Phase 3b: any handler can prepend this to its response to surface
        in-session identity drift without duplicating the warning across
        multiple tool calls.
        """
        try:
            warn = self.memory.consume_mid_session_drift_warning()
        except Exception:
            return ""
        if warn is None:
            return ""
        return (
            f"⚠ in-session drift detected (cos dist {warn['distance']} vs "
            f"{warn['baseline_size']}-turn baseline) — last turn diverged "
            f"from the voice established this session. "
            f"Am I still me?\n\n"
        )

    def _reset_auto_close_timer(self) -> None:
        """Reset the auto-close timer. Called on every tool use."""
        if self._auto_close_timer is not None:
            self._auto_close_timer.cancel()
        self._auto_close_timer = threading.Timer(
            _AUTO_CLOSE_TIMEOUT, self._auto_close,
        )
        self._auto_close_timer.daemon = True
        self._auto_close_timer.start()

    def _auto_close(self) -> None:
        """Auto-close session after timeout. Prevents false crash warnings."""
        if self._session_started:
            try:
                self.memory.close(
                    summary="Session auto-closed after inactivity timeout",
                    project="global",
                )
                self._session_started = False
            except Exception:
                pass  # Best-effort — don't crash the server

    def handle_identity(self) -> str:
        self._ensure_session()
        return self.memory.format_identity()

    def handle_briefing(self, project: str | None = None) -> str:
        self._ensure_session(project or "global")
        result = self.memory.briefing(project=project)
        self.memory._use_budget(result)
        return result

    def handle_observe(self, summary: str, project: str = "global") -> str:
        self._ensure_session(project)
        entry = self.memory.observe(summary, project=project)
        if entry:
            warnings = []

            # Check for contradictions
            contradiction = self.memory.check_contradiction(entry["fact"], project)
            if contradiction:
                warnings.append(
                    f"WARNING: may contradict prior knowledge: \"{contradiction['fact'][:80]}\""
                )

            # Check for similarity to past mistakes
            similar_mistake = self.memory.check_mistake_similarity(entry["fact"], project)
            if similar_mistake:
                sim = similar_mistake.get("_similarity", 0)
                warnings.append(
                    f"CAUTION: similar to past mistake ({sim:.0%}): "
                    f"\"{similar_mistake['mistake'][:80]}\" — {similar_mistake.get('why', '')[:40]}"
                )

            # Auto-detect mood signals and update state
            try:
                from null_memory.mood import detect_mood, should_update_state
                from null_memory.wakeup import load_state, save_state
                signal = detect_mood(summary)
                if should_update_state(signal):
                    state = load_state()
                    if signal.energy:
                        state["energy"] = signal.energy
                    if signal.sentiment:
                        state.setdefault("concerns", [])
                        if signal.sentiment in ("frustrated", "negative"):
                            # Add concern, keep bounded
                            concern = f"[auto] {signal.reason}: {summary[:60]}"
                            state["concerns"] = [concern] + state.get("concerns", [])[:4]
                        elif signal.sentiment in ("positive", "excited"):
                            state.setdefault("optimistic_about", [])
                            note = f"[auto] {signal.reason}: {summary[:60]}"
                            state["optimistic_about"] = [note] + state.get("optimistic_about", [])[:4]
                    save_state(state)
            except Exception:
                pass

            # Cross-instance decision awareness
            try:
                prior = self.memory.check_prior_decisions(entry["fact"], project)
                if prior:
                    sim = prior.get("_similarity")
                    sim_str = f" ({sim:.0%})" if sim else ""
                    status = prior.get("status", "provisional")
                    warnings.append(
                        f"PRIOR DECISION{sim_str} [{status}]: "
                        f"\"{prior['decision'][:80]}\" — {prior.get('reasoning', '')[:40]}"
                    )
            except Exception:
                pass

            # Proactive insight pushing — Atlas contributes unprompted
            # No rate limiting by count; topic dedup prevents noise.
            # Only suppress if we've already surfaced 3+ insights this turn cycle.
            try:
                insights = self.memory.find_relevant_insights(entry["fact"], project)
                for insight in insights:
                    sim = insight.get("_similarity", 0)
                    proj = insight.get("project", "global")
                    warnings.append(
                        f"INSIGHT ({sim:.0%}) [{proj}]: \"{insight['fact'][:100]}\""
                    )
            except Exception:
                pass

            # Heartbeat: periodic calibration check every 10 turns
            try:
                session = self.memory._current_session
                if session and session.facts_created > 0 and session.facts_created % 10 == 0:
                    cal = self.memory.run_probes()
                    if cal["total"] > 0:
                        if cal["failed"] > 0:
                            warnings.append(
                                f"HEARTBEAT: calibration {cal['passed']}/{cal['total']} "
                                f"({cal['score']:.0%}) — {cal['failed']} probe(s) failing"
                            )
                        else:
                            warnings.append(
                                f"HEARTBEAT: calibration {cal['passed']}/{cal['total']} "
                                f"({cal['score']:.0%}) — all probes passing"
                            )
            except Exception:
                pass

            drift_prefix = self._drift_prefix()
            if warnings:
                return drift_prefix + f"Observed: {summary}\n" + "\n".join(warnings)
            return drift_prefix + f"Observed: {summary}"
        return self._drift_prefix() + "Nothing new to record."

    def handle_checkpoint(self) -> str:
        """Deep save — flush all knowledge to disk and git commit."""
        self._ensure_session()
        self.memory.sync()
        committed = self.memory.checkpoint(note="manual checkpoint")
        msg = f"Checkpoint complete. {len(self.memory.knowledge)} total facts, {len(self.memory.decisions)} decisions."
        if committed:
            msg += " (committed to git)"
        return msg

    def handle_recall(self, query: str, project: str | None = None,
                      include_archived: bool = False,
                      since: str | None = None,
                      session: str | None = None,
                      full: bool = False) -> str:
        self._ensure_session()
        results = self.memory.recall(
            query, project=project, include_archived=include_archived,
            since=since, session_id=session,
        )
        if not results:
            return f"No knowledge matching '{query}'."

        # Check probe reliability for all returned facts
        fact_ids = [e.get("id", "") for e in results if e.get("id")]
        reliability_warnings = self.memory.check_fact_reliability(fact_ids)

        lines = [f"Recall ({len(results)} results):"]
        for entry in results:
            conf = self.memory.effective_confidence(entry)
            proj = entry.get("project", "global")
            entry_type = entry.get("_type", "fact")
            if full:
                if entry_type == "mistake":
                    lines.append(f"  !! [{conf:.0%}] [{proj}] MISTAKE: {entry['mistake']} — {entry.get('why', '')}")
                elif entry_type == "archived":
                    lines.append(f"  ~~ [{conf:.0%}] [{proj}] (archived) {entry['fact']}")
                elif entry_type == "context":
                    lines.append(f"    -> [{conf:.0%}] [{proj}] {entry['fact']}")
                else:
                    lines.append(f"  [{conf:.0%}] [{proj}] {entry['fact']}")
            else:
                if entry_type == "mistake":
                    lines.append(f"  !! [{conf:.0%}] [{proj}] MISTAKE: {entry['mistake'][:100]} — {entry.get('why', '')[:40]}")
                elif entry_type == "archived":
                    lines.append(f"  ~~ [{conf:.0%}] [{proj}] (archived) {entry['fact'][:120]}")
                elif entry_type == "context":
                    lines.append(f"    -> [{conf:.0%}] [{proj}] {entry['fact'][:120]}")
                else:
                    lines.append(f"  [{conf:.0%}] [{proj}] {entry['fact'][:120]}")
            # Append reliability warning if this fact has failing probes
            fid = entry.get("id", "")
            if fid in reliability_warnings:
                lines.append(f"    ⚠ {reliability_warnings[fid]}")
        result = "\n".join(lines)
        self.memory._use_budget(result)
        return result

    def handle_learn(self, fact: str, confidence: float = 0.8,
                     project: str = "global") -> str:
        self._ensure_session(project)
        contradiction = self.memory.check_contradiction(fact, project)
        entry = self.memory.learn(fact, confidence, project=project, source="explicit")
        msg = f"Learned: {fact[:80]}... [{confidence:.0%}]"
        if contradiction:
            msg += f"\nWARNING: may contradict: \"{contradiction['fact'][:80]}\""

        # Probe validation: check if this new fact broke any existing probes
        broken = self.memory.validate_after_learn(fact)
        if broken:
            msg += f"\nPROBE ALERT: {len(broken)} probe(s) now failing after this learn:"
            for b in broken[:3]:
                msg += f"\n  ⚠ \"{b['question'][:60]}\" expected \"{b['expected'][:30]}\""
        return msg

    def handle_decide(self, decision: str, reasoning: str,
                      project: str = "global") -> str:
        self._ensure_session(project)
        entry = self.memory.decide(decision, reasoning, project=project)
        out = f"Decision logged: {decision[:80]}"
        warn = entry.get("mistake_warning") if isinstance(entry, dict) else None
        if warn:
            out += (
                f"\n  ⚠ Resembles prior mistake (sim {warn['similarity']}):"
                f"\n    {warn['mistake'][:100]}"
            )
            if warn.get("why"):
                out += f"\n    why: {warn['why'][:100]}"
            out += "\n  (Decision committed — surfaced for your judgment, not blocked.)"
        return self._drift_prefix() + out

    def handle_context(self, project: str) -> str:
        self._ensure_session(project)
        data = self.memory.projects.get(project)
        if not data:
            # Try to find relevant knowledge for this project
            results = self.memory.recall(project, project=project, limit=5)
            if results:
                lines = [f"No dedicated context for '{project}', but found relevant knowledge:"]
                for r in results:
                    lines.append(f"  [{r.get('confidence', 0.5):.0%}] {r['fact'][:100]}")
                return "\n".join(lines)
            return f"No context found for project '{project}'."
        return json.dumps(data, indent=2)

    def handle_contradict(self, fact: str) -> str:
        result = self.memory.check_contradiction(fact)
        if result:
            return f"Contradiction found: \"{result['fact'][:100]}\" conflicts with \"{fact[:100]}\""
        return "No contradiction detected."

    def handle_sync(self) -> str:
        return self.memory.sync()

    def handle_close(self, summary: str = "", went_well: str = "",
                     missed: str = "", do_differently: str = "",
                     decisions_made: list[str] | None = None,
                     lessons: list[str] | None = None,
                     identity_updates: dict[str, str] | None = None,
                     project: str = "global") -> str:
        """Atomic session close: debrief + reflect + sync + git commit."""
        self._ensure_session(project)
        result = self.memory.close(
            summary=summary,
            went_well=went_well,
            missed=missed,
            do_differently=do_differently,
            decisions_made=decisions_made,
            lessons=lessons,
            identity_updates=identity_updates,
            project=project,
        )
        self._session_started = False

        parts = [result.get("synced", "Session closed.")]
        debrief = result.get("debrief", {})
        if debrief:
            parts.append(f"Debrief: {debrief.get('facts', 0)} facts, {debrief.get('decisions', 0)} decisions.")
        if result.get("reflected"):
            parts.append("Reflection saved.")
        if result.get("committed"):
            parts.append("Committed to memory git repo.")
        return " ".join(parts)

    def handle_verify(self, fact_query: str) -> str:
        """Mark the best-matching fact as verified."""
        self._ensure_session()
        entry = self.memory.verify_fact(fact_query)
        if entry is None:
            return f"No fact matching '{fact_query}' found to verify."
        return (
            f"Verified: {entry['fact'][:100]}\n"
            f"  last_verified={entry.get('last_verified', '?')}"
        )

    def handle_consolidate(self) -> str:
        """Trigger memory consolidation."""
        self._ensure_session()
        result = self.memory.consolidate()
        return (
            f"Consolidation complete. "
            f"Strengthened: {result['strengthened']}, "
            f"Faded: {result['faded']}, "
            f"Consolidated: {result['consolidated']}."
        )

    def handle_catchup(self, source: str = "git", project: str = "global",
                       since: str = "", facts: list[str] | None = None) -> str:
        """Reconstruct knowledge from external evidence."""
        self._ensure_session(project)
        if source == "git":
            created = self.memory.catchup_from_git(project=project, since=since)
            if not created:
                return "No commits found to reconstruct from. Try specifying --since or check git repo."
            lines = [f"Catchup from git: {len(created)} facts reconstructed."]
            for entry in created:
                lines.append(f"  [{entry.get('confidence', 0.6):.0%}] {entry['fact'][:100]}")
            return "\n".join(lines)
        elif source == "manual":
            if not facts:
                return "No facts provided for manual catchup."
            created = self.memory.catchup_manual(facts, project=project)
            return f"Manual catchup: {len(created)} facts stored."
        else:
            return f"Unknown catchup source: {source}. Use 'git' or 'manual'."

    def handle_status(self) -> str:
        return self.memory.status()

    def handle_export(self) -> str:
        data = self.memory.export_all()
        return json.dumps(data, indent=2)

    def handle_import(self, data_json: str) -> str:
        try:
            data = json.loads(data_json)
        except json.JSONDecodeError:
            return "Error: invalid JSON."
        self._memory = AgentMemory.import_from(data, self.agent_dir)
        report = AgentMemory.format_import_report(
            self._memory.last_import_counts)
        return f"{report}. Name: {self._memory.name}"

    def handle_mistake(self, what: str, why: str,
                       project: str = "global") -> str:
        self._ensure_session(project)
        entry = self.memory.mistake(what, why, project=project)
        return self._drift_prefix() + f"Mistake recorded: {what[:80]} — Why: {why[:60]}"

    def handle_reflect(self, went_well: str, missed: str,
                       do_differently: str, project: str = "global") -> str:
        self._ensure_session(project)
        self.memory.reflect(went_well, missed, do_differently, project=project)
        return self._drift_prefix() + (
            f"Reflection saved.\n"
            f"  + {went_well[:80]}\n"
            f"  - {missed[:80]}\n"
            f"  > {do_differently[:80]}"
        )

    def handle_debrief(self, summary: str,
                       decisions_made: list[str] | None = None,
                       lessons: list[str] | None = None,
                       identity_updates: dict[str, str] | None = None,
                       project: str = "global") -> str:
        self._ensure_session(project)
        result = self.memory.debrief(
            summary=summary,
            decisions_made=decisions_made,
            lessons=lessons,
            identity_updates=identity_updates,
            project=project,
        )
        parts = [f"Debrief saved: {result['facts']} facts, {result['decisions']} decisions."]
        if result["identity_updated"]:
            parts.append("Identity updated.")
        return " ".join(parts)

    def handle_gc(self) -> str:
        result = self.memory.gc()
        return (
            f"GC complete. {result['original']} → {result['remaining']} facts. "
            f"Archived: {result['archived']}, merged: {result['merged']}."
        )

    def handle_exemplar(self, query: str) -> str:
        results = self.memory.find_exemplars(query)
        if not results:
            return "No matching exemplars."
        lines = [f"Calibration exemplars ({len(results)} matches):"]
        for ex in results:
            lines.append(f"\n  Scenario: {ex.get('scenario', '?')}")
            lines.append(f"  User: \"{ex.get('user_text', ex.get('pete', ''))}\"")
            lines.append(f"  Agent: \"{ex.get('agent_text', ex.get('atlas', ''))[:150]}\"")
            lines.append(f"  Calibration: {ex['calibration']}")
        return "\n".join(lines)

    def handle_wonder(self, question: str, context: str = "",
                      category: str = "calibration") -> str:
        """Store a calibration question for Pete to answer."""
        self._ensure_session()
        from null_memory.wakeup import add_simmering
        entry = add_simmering(
            question=question,
            context=context,
            category=category,
        )
        return f"Question recorded: {question[:80]}\n  Stored in simmering [{entry['id'][:8]}] — will surface in next briefing."

    def handle_forget(self, query: str = "", fact_id: str = "") -> str:
        """Soft-delete a fact. fact_id (exact) takes precedence over
        query (fuzzy); fuzzy near-ties refuse with candidates listed."""
        self._ensure_session()
        if fact_id:
            result = self.memory.forget(fact_id=fact_id)
            if result is None:
                return (
                    f"No fact with id '{fact_id}' (exact match — no fuzzy "
                    f"fallback). Use null_recall to find the right id."
                )
            return (
                f"Forgotten [{fact_id[:12]}]: {result['fact'][:100]}\n"
                f"  (soft-deleted — recoverable via null doctor --fix)"
            )
        if not query:
            return "Provide fact_id (preferred when known) or a query."
        from null_memory.agent import ForgetAmbiguousError
        try:
            result = self.memory.forget(query)
        except ForgetAmbiguousError as e:
            lines = [
                "REFUSED: top matches are a near-tie — fuzzy matching could "
                "delete the wrong near-duplicate. Candidates:",
            ]
            for c in e.candidates:
                lines.append(f"  [{c.get('id', '?')[:12]}] {c.get('fact', '')[:90]}")
            lines.append("Retry with fact_id set to the one you mean.")
            return "\n".join(lines)
        if result is None:
            return f"No fact matching '{query}' found to forget."
        return (
            f"Forgotten: {result['fact'][:100]}\n"
            f"  (soft-deleted — recoverable via null doctor --fix)"
        )

    def handle_outreach(self, subject: str, body: str,
                        urgency: float = 0.5,
                        channel: str = "log") -> str:
        """Manual outreach emission — Atlas reaching out to Pete
        from within a conversation. Writes to outreaches table + log,
        optionally fires macOS notification if opted in.
        """
        self._ensure_session()
        try:
            from null_memory.outreach import send_manual_outreach
        except Exception as e:
            return f"[outreach] module unavailable: {e}"

        try:
            result = send_manual_outreach(
                self.memory, subject, body, urgency=urgency, channel=channel,
            )
        except Exception as e:  # noqa: BLE001 — e.g. missing outreaches table
            return f"[outreach] failed: {e}"

        return (
            f"[outreach] sent id={result['id']} via "
            f"{','.join(result['channels']) or 'none'}\n"
            f"  subject: {subject[:80]}\n"
            f"  body:    {body[:120]}"
        )

    def handle_verify_identity(self) -> str:
        """Run three-proof identity verification and render a terse report."""
        self._ensure_session()
        result = self.memory.verify_identity()
        verdict = result["verdict"].upper()
        proofs = result["proofs"]

        def tag(v):
            if v is True:
                return "✓"
            if v is False:
                return "✗"
            return "?"

        lines = [
            f"[Null] Identity verification: {verdict}",
            f"  {tag(proofs['memory_access'])} memory access (code word)",
            f"  {tag(proofs['shared_experience'])} shared experience (continuity probes)",
            f"  {tag(proofs['behavioral_continuity'])} behavioral continuity (session drift)",
            f"  {tag(proofs['mid_session_continuity'])} mid-session continuity (per-turn drift)",
        ]
        probe_stats = result["details"]["probes"]
        if probe_stats["total"]:
            lines.append(
                f"  Probes: {probe_stats['passed']}/{probe_stats['total']} passed "
                f"({probe_stats['score']:.0%})"
            )
            for d in probe_stats["details"][:5]:
                status = "PASS" if d["passed"] else "FAIL"
                rank = f" @#{d['rank']}" if d.get("rank") else ""
                lines.append(
                    f"    [{status}{rank}] {d['question'][:80]}"
                )
        drift = result["details"]["drift"]
        if drift:
            lines.append(f"  {drift}")
        return "\n".join(lines)

    def handle_anchor(self, query: str, anchor_type: str,
                      note: str = "") -> str:
        """Tag a fact as an emotional anchor — a load-bearing memory that
        never decays, surfaces first in briefing, and gets recall priority.

        anchor_type ∈ {origin, commitment, loss, joy, turning_point}.
        ``query`` may be a 12-char fact id or a text query.
        """
        self._ensure_session()
        try:
            fact = self.memory.anchor(query, anchor_type, note=note)
        except ValueError as e:
            return f"Invalid anchor_type: {e}"
        except RuntimeError as e:
            return f"Anchoring unavailable: {e}"
        if fact is None:
            return f"No fact matching '{query[:80]}' — nothing anchored."
        return (
            f"Anchored [{anchor_type}] {fact['fact'][:110]}\n"
            f"  id={fact['id']}  note={note[:80] or '(none)'}"
        )

    def handle_exemplar_add(self, scenario: str, user_text: str,
                            agent_text: str = "", calibration: str = "",
                            tags: list[str] | None = None) -> str:
        """Add a new calibration exemplar."""
        self._ensure_session()
        entry = self.memory.add_exemplar(
            scenario=scenario, user_text=user_text, agent_text=agent_text,
            calibration=calibration, tags=tags,
        )
        tag_str = ", ".join(entry.get("tags", []))
        return (
            f"Exemplar added: [{scenario}]\n"
            f"  User: \"{user_text[:60]}\"\n"
            f"  Calibration: {calibration[:80]}\n"
            f"  Tags: {tag_str or 'none'}"
        )

    def handle_probe_add(self, question: str, expected: str,
                         fact_id: str | None = None,
                         probe_type: str = "user") -> str:
        """Add a user-defined calibration probe."""
        self._ensure_session()
        probe = self.memory.add_probe(question, expected, fact_id,
                                      probe_type=probe_type or "user")
        return (
            f"Probe added: \"{question}\"\n"
            f"  Expected: {expected}\n"
            f"  Fact: {fact_id or 'any'}\n"
            f"  Type: {probe['probe_type']}\n"
            f"  Run 'null doctor' or 'null calibrate' (CLI) to test."
        )

    def handle_calibrate(self, probe_type: str | None = None) -> str:
        """Run calibration probes and return results."""
        self._ensure_session()
        results = self.memory.run_probes(probe_type=probe_type)
        lines = [f"[Null] Calibration: {results['passed']}/{results['total']} passed "
                 f"({results['score']:.0%})"]
        for d in results["details"]:
            status = "PASS" if d["passed"] else "FAIL"
            rank = f" (rank #{d['matched_rank']})" if d["matched_rank"] else ""
            lines.append(f"  [{status}] [{d['probe_type']}] {d['question'][:80]}{rank}")
            if not d["passed"]:
                lines.append(f"    Expected: {d['expected']}")
        return "\n".join(lines)

    def handle_evaluate(self, notes: str = "") -> str:
        """Run comprehensive evaluation and return formatted report."""
        self._ensure_session()
        result = self.memory.run_evaluation(notes)
        score = result["score"]
        metrics = result["metrics"]
        comparison = result.get("comparison")

        # Grade
        if score >= 80:
            grade = "HEALTHY"
        elif score >= 60:
            grade = "FAIR"
        elif score >= 40:
            grade = "DEGRADED"
        else:
            grade = "CRITICAL"

        lines = [f"[Null] Evaluation: {score}/100 ({grade})"]
        lines.append("")

        # Recall quality
        r = metrics.get("recall", {})
        lines.append(f"  Recall Quality: {r.get('subscore', '?')}/100")
        if r.get("probe_count", 0) > 0:
            lines.append(f"    Probes tested: {r['probe_count']} | "
                         f"Hit rate: {r.get('hit_rate', 0):.0%} | "
                         f"Avg rank: {r.get('avg_rank', '?')}")
            if r.get("misses", 0) > 0:
                lines.append(f"    ⚠ {r['misses']} probe(s) returned no match")
        elif r.get("note"):
            lines.append(f"    {r['note']}")

        # Knowledge health
        k = metrics.get("knowledge", {})
        lines.append(f"  Knowledge Health: {k.get('subscore', '?')}/100")
        lines.append(f"    Active: {k.get('active_facts', 0)} | "
                     f"Avg confidence: {k.get('avg_confidence', 0):.0%} | "
                     f"Stale: {k.get('stale_facts', 0)} ({k.get('stale_pct', 0):.0%})")
        lines.append(f"    7-day: +{k.get('recent_7d_created', 0)} created, "
                     f"-{k.get('recent_7d_churned', 0)} churned")
        tiers = k.get("tiers", {})
        if tiers:
            tier_str = ", ".join(f"{t}={c}" for t, c in sorted(tiers.items()))
            lines.append(f"    Tiers: {tier_str}")

        # Probe trending
        p = metrics.get("probes", {})
        lines.append(f"  Probe Trending: {p.get('subscore', '?')}/100")
        if p.get("total_probes", 0) > 0:
            lines.append(f"    Total: {p['total_probes']} | "
                         f"Pass rate: {p.get('current_pass_rate', 0):.0%} | "
                         f"Regressed: {p.get('regressed', 0)}")
            if p.get("never_passed", 0) > 0:
                lines.append(f"    ⚠ {p['never_passed']} probe(s) have never passed")
            by_type = p.get("by_type", {})
            if by_type:
                lines.append(f"    By type: system={by_type.get('system', 0)} "
                             f"auto={by_type.get('auto', 0)} "
                             f"user={by_type.get('user', 0)}")
        elif p.get("note"):
            lines.append(f"    {p['note']}")

        # Session quality
        s = metrics.get("sessions", {})
        lines.append(f"  Session Quality: {s.get('subscore', '?')}/100")
        if s.get("total_sessions", 0) > 0:
            lines.append(f"    Sessions: {s['total_sessions']} | "
                         f"Crash rate: {s.get('crash_rate', 0):.0%} | "
                         f"Avg facts/session: {s.get('avg_facts_per_session', 0)}")
            lines.append(f"    Decisions: {s.get('total_decisions', 0)} | "
                         f"Mistakes: {s.get('total_mistakes', 0)} | "
                         f"Reflections: {s.get('total_reflections', 0)}")
        elif s.get("note"):
            lines.append(f"    {s['note']}")

        # Comparison to previous
        if comparison:
            lines.append("")
            delta = comparison["delta"]
            direction = comparison["direction"]
            arrow = "↑" if delta > 0 else "↓" if delta < 0 else "→"
            lines.append(f"  vs Previous: {comparison['previous_score']}/100 "
                         f"{arrow} {delta:+.1f} ({direction})")
            cat_deltas = comparison.get("category_deltas", {})
            changes = []
            for cat, d in cat_deltas.items():
                if d != 0:
                    a = "↑" if d > 0 else "↓"
                    changes.append(f"{cat} {a}{abs(d):.0f}")
            if changes:
                lines.append(f"    Changes: {', '.join(changes)}")
            lines.append(f"    Previous run: {comparison['previous_run'][:19]}")

        return "\n".join(lines)

    def handle_doctor(self) -> str:
        """Run memory diagnostics with calibration."""
        self._ensure_session()
        findings = self.memory.diagnose()
        lines = ["[Null] Memory Health Check"]
        lines.append(f"  Active facts: {findings['active_facts']}")
        lines.append(f"  Total facts (incl. archived/forgotten): {findings['total_facts']}")
        lines.append(f"  Decisions: {findings['decisions']}")
        lines.append(f"  Mistakes: {findings['mistakes']}")
        lines.append(f"  Reflections: {findings['reflections']}")
        lines.append(f"  Archived: {findings['archived_facts']}")
        lines.append(f"  Forgotten: {findings['forgotten_facts']}")
        lines.append(f"  Superseded: {findings['superseded_facts']}")
        lines.append(f"  Projects: {', '.join(findings['projects'])}")

        tiers = findings.get("tiers", {})
        if tiers:
            tier_parts = [f"{t}={c}" for t, c in sorted(tiers.items())]
            lines.append(f"  Tiers: {', '.join(tier_parts)}")

        # Calibration probes
        probe_count = self.memory.db.count_probes()
        lines.append(f"\n  Calibration probes: {probe_count}")
        if probe_count > 0 or True:  # Always run system probes
            try:
                cal = self.memory.run_probes()
                lines.append(f"  Calibration score: {cal['passed']}/{cal['total']} "
                             f"({cal['score']:.0%})")
                failures = [d for d in cal["details"] if not d["passed"]]
                if failures:
                    lines.append("  Failed probes:")
                    for f in failures[:5]:
                        lines.append(f"    [{f['probe_type']}] {f['question'][:60]} "
                                     f"(expected: {f['expected'][:30]})")
            except Exception as e:
                lines.append(f"  Calibration error: {e}")

        issues = []
        if findings.get("embed_failures"):
            issues.append(
                f"  {findings['embed_failures']} swallowed embedding failures "
                f"(last: {findings.get('embed_failures_last') or 'unknown'}) — "
                "semantic recall may be degraded"
            )
        if findings["test_mistakes"] > 0:
            issues.append(f"  {findings['test_mistakes']} test/stub mistakes (run null doctor --fix)")
        if findings["test_reflections"] > 0:
            issues.append(f"  {findings['test_reflections']} test/stub reflections")
        if findings["test_facts"] > 0:
            issues.append(f"  {findings['test_facts']} test/placeholder facts")
        if findings["stale_facts"] > 0:
            issues.append(f"  {findings['stale_facts']} stale facts (no access in 60+ days)")

        if issues:
            lines.append("\n  Issues found:")
            lines.extend(issues)
        else:
            lines.append("\n  No issues found.")

        return "\n".join(lines)

    def handle_outcome(self, decision_query: str, outcome: str,
                       success: str = "", project: str = "") -> str:
        """Record the outcome of a prior decision."""
        self._ensure_session()
        success_val: bool | None = None
        if success.lower() in ("true", "yes", "1", "success"):
            success_val = True
        elif success.lower() in ("false", "no", "0", "failure", "fail"):
            success_val = False

        result = self.memory.record_outcome(
            decision_query=decision_query,
            outcome=outcome,
            success=success_val,
            project=project or None,
        )
        if result is None:
            # Surface near-miss candidates instead of a bare "not found"
            # so the caller can retry with a better query or an exact id.
            candidates = self.memory.db.find_decision_candidates(
                decision_query, project=project or None, limit=3,
            )
            if not candidates:
                return f"No decision matching '{decision_query}' found."
            lines = [
                f"No decision matching '{decision_query}' found. "
                f"Closest candidates:"
            ]
            for c in candidates:
                created = (c.get("created_at") or "")[:16]
                snippet = (c.get("decision") or "")[:90]
                lines.append(f"  [{c.get('id')}] {created}  {snippet}")
            lines.append("Retry null_outcome with words from the right one.")
            return "\n".join(lines)
        status = "success" if result.get("success") else ("failure" if result.get("success") is False else "recorded")
        return f"Outcome recorded ({status}): {outcome[:80]}"

    def handle_name(self, name: str) -> str:
        self.memory.set_name(name)
        return f"Name set to: {name}"

    # ── Multiverse ──

    def _get_multiverse(self):
        """Lazy-load MultiverseManager."""
        if not hasattr(self, "_multiverse"):
            from null_memory.multiverse import MultiverseManager
            import os
            base = os.environ.get("NULL_DIR", os.path.join(os.path.expanduser("~"), ".null"))
            self._multiverse = MultiverseManager(base_dir=base)
        return self._multiverse

    def handle_multiverse_list(self) -> str:
        mv = self._get_multiverse()
        personalities = mv.list_personalities()
        if not personalities:
            return "[Multiverse] No personalities registered. Run 'null multiverse migrate' first."
        lines = [f"[Multiverse] {len(personalities)} personalities:"]
        for p in personalities:
            role_marker = "*" if p["role"] == "manager" else " "
            focus = f" — {p['focus']}" if p.get("focus") else ""
            active = "" if p.get("active") else " [archived]"
            lines.append(f"  {role_marker} {p['name']}: {p['role']}{focus}{active}")
        return "\n".join(lines)

    def handle_multiverse_broadcast(self, event: str, targets: str = "") -> str:
        self._ensure_session()
        mv = self._get_multiverse()
        target_list = [t.strip() for t in targets.split(",") if t.strip()] if targets else None
        result = mv.broadcast(event=event, targets=target_list)
        target_names = ", ".join(result.get("targets", []))
        lines = [f"[Multiverse] Broadcast to: {target_names}"]
        for personality, fact_id in result.get("fact_ids", {}).items():
            lines.append(f"  {personality}: {fact_id[:12]}")
        return "\n".join(lines)

    def handle_multiverse_recall(self, query: str, personalities: str = "") -> str:
        mv = self._get_multiverse()
        personality_list = (
            [p.strip() for p in personalities.split(",") if p.strip()]
            if personalities else None
        )
        results = mv.recall(query=query, personalities=personality_list)
        if not results:
            return f"No results matching '{query}' across personalities."
        lines = [f"Multiverse recall ({len(results)} results):"]
        for entry in results:
            conf = entry.get("confidence", 0.5)
            p_name = entry.get("_personality", "?")
            proj = entry.get("project", "global")
            entry_type = entry.get("_type", "fact")
            if entry_type == "mistake":
                lines.append(f"  [{p_name}/{conf:.0%}] [{proj}] MISTAKE: {entry['mistake'][:100]}")
            else:
                lines.append(f"  [{p_name}/{conf:.0%}] [{proj}] {entry['fact'][:120]}")
        return "\n".join(lines)

    def handle_verify_claim(self, claim_text: str,
                            claim_type: str = "auto") -> str:
        """Verify a claim about live system state.

        Use this BEFORE asserting "X has shipped", "file Y exists",
        "function Z is at L", etc. Avoids the bug where a stale doc
        gets trusted as truth.

        Steps:
          1. Look up the claim in doc_claims (substring + status match).
             If a refuted/verified row already exists with recent
             verification, return that.
          2. Otherwise run the relevant ad-hoc verifier directly.
          3. Return human-readable status + evidence.
        """
        from null_memory.doc_audit import (
            _verify_file_ref,
            _verify_function_ref,
            _verify_ship_status,
            _verify_schema_version,
        )
        text = (claim_text or "").strip()
        if not text:
            return "[verify] empty claim — nothing to check"

        # 1. DB lookup — substring on claim_text.
        try:
            rows = self.memory.db.conn.execute(
                """SELECT source_path, claim_type, status,
                          refute_evidence, last_verified_at
                   FROM doc_claims
                   WHERE claim_text LIKE ?
                   ORDER BY last_verified_at DESC NULLS LAST LIMIT 3""",
                (f"%{text[:80]}%",),
            ).fetchall()
        except Exception:
            rows = []
        if rows:
            r = rows[0]
            src = (r[0] or "?").rsplit("/", 1)[-1]
            status = r[2]
            evidence = r[3] or ""
            verified_at = r[4] or "(never)"
            line = (
                f"[verify] cached: status={status} source={src} "
                f"verified_at={verified_at}"
            )
            if evidence:
                line += f"\n  evidence: {evidence[:200]}"
            return line

        # 2. Ad-hoc verify. Pick verifier by claim_type or auto-detect.
        import os
        repo_root = os.path.expanduser("~/Repos/null")
        if claim_type == "auto":
            lower = text.lower()
            if any(k in lower for k in ("shipped", "todo", "pending",
                                         "phase ", "next session")):
                claim_type = "ship_status"
            elif "schema" in lower:
                claim_type = "schema_version"
            elif any(text.endswith(ext) or f".{ext}" in text
                     for ext in ("py", "ts", "tsx", "md", "json")):
                claim_type = "file_ref"
            else:
                claim_type = "other"

        verifier_map = {
            "file_ref": _verify_file_ref,
            "function_ref": _verify_function_ref,
            "ship_status": _verify_ship_status,
            "schema_version": _verify_schema_version,
        }
        if claim_type not in verifier_map:
            return (
                f"[verify] no verifier for type={claim_type!r} — "
                f"manual check needed"
            )
        verdict, evidence = verifier_map[claim_type](text, repo_root)
        line = f"[verify] live-checked: {verdict} type={claim_type}"
        if evidence:
            line += f"\n  evidence: {evidence[:200]}"
        return line

    def handle_multiverse_wakeup(self) -> str:
        mv = self._get_multiverse()
        summaries = mv.wakeup()
        lines = ["[Multiverse] Wakeup synthesis:"]
        for name, data in summaries.items():
            state = data.get("state", {})
            momentum = data.get("momentum", {})
            energy = state.get("energy", "?")
            focus = data.get("focus", "")
            project = momentum.get("active_project", "")
            line = f"  [{name}] {energy} energy"
            if focus:
                line += f" — {focus}"
            if project:
                line += f" | project: {project}"
            if data.get("error"):
                line += f" | ERROR: {data['error']}"
            lines.append(line)
        return "\n".join(lines)

    # ── Merged-surface dispatchers (P1-12 / N9) ──────────────────────────
    # The MCP surface shrank from 39 tools to 15: the write path became
    # null_remember(kind=), the verify triad became null_verify(mode=),
    # exemplar search/add and the four multiverse tools merged, and the
    # operator/maintenance commands moved to the CLI. These dispatchers
    # route the merged tools to the original handlers.

    REMEMBER_KINDS = ("observe", "learn", "decide", "mistake", "wonder",
                      "contradict")
    VERIFY_MODES = ("fact", "claim", "identity")
    MULTIVERSE_ACTIONS = ("list", "broadcast", "recall", "wakeup")

    def handle_remember(self, kind: str, text: str, why: str = "",
                        confidence: float = 0.8, project: str = "global",
                        context: str = "", category: str = "") -> str:
        """Unified write path: one tool, six kinds of memory."""
        kind = (kind or "").strip().lower()
        if kind == "observe":
            return self.handle_observe(text, project)
        if kind == "learn":
            return self.handle_learn(text, confidence, project)
        if kind == "decide":
            if not why:
                return "[null] decide requires 'why' — a decision without reasoning can't be learned from."
            return self.handle_decide(text, why, project)
        if kind == "mistake":
            if not why:
                return "[null] mistake requires 'why' — record what went wrong AND why."
            return self.handle_mistake(text, why, project)
        if kind == "wonder":
            return self.handle_wonder(text, context, category or "calibration")
        if kind == "contradict":
            return self.handle_contradict(text)
        return (f"[null] unknown kind '{kind}' — expected one of: "
                f"{', '.join(self.REMEMBER_KINDS)}")

    def handle_verify_dispatch(self, mode: str = "fact",
                               query: str = "",
                               claim_type: str = "auto") -> str:
        """Unified verification: fact (anti-decay), claim (live-state),
        identity (three-proof)."""
        mode = (mode or "").strip().lower()
        if mode == "fact":
            if not query:
                return "[null] verify mode=fact requires a query matching the fact."
            return self.handle_verify(query)
        if mode == "claim":
            if not query:
                return "[null] verify mode=claim requires the claim text."
            return self.handle_verify_claim(query, claim_type or "auto")
        if mode == "identity":
            return self.handle_verify_identity()
        return (f"[null] unknown mode '{mode}' — expected one of: "
                f"{', '.join(self.VERIFY_MODES)}")

    def handle_exemplar_dispatch(self, action: str = "search",
                                 query: str = "", scenario: str = "",
                                 user_text: str = "", agent_text: str = "",
                                 calibration: str = "",
                                 tags: list[str] | None = None) -> str:
        action = (action or "").strip().lower()
        if action == "search":
            if not query:
                return "[null] exemplar action=search requires a query."
            return self.handle_exemplar(query)
        if action == "add":
            if not scenario or not user_text:
                return "[null] exemplar action=add requires scenario and user_text."
            return self.handle_exemplar_add(
                scenario=scenario, user_text=user_text, agent_text=agent_text,
                calibration=calibration, tags=tags,
            )
        return f"[null] unknown action '{action}' — expected 'search' or 'add'."

    def handle_multiverse(self, action: str = "list", text: str = "",
                          targets: str = "") -> str:
        action = (action or "").strip().lower()
        if action == "list":
            return self.handle_multiverse_list()
        if action == "broadcast":
            if not text:
                return "[null] multiverse action=broadcast requires the event text."
            return self.handle_multiverse_broadcast(text, targets)
        if action == "recall":
            if not text:
                return "[null] multiverse action=recall requires a query."
            return self.handle_multiverse_recall(text, targets)
        if action == "wakeup":
            return self.handle_multiverse_wakeup()
        return (f"[null] unknown action '{action}' — expected one of: "
                f"{', '.join(self.MULTIVERSE_ACTIONS)}")
