"""Evaluation suite — periodic health/performance scoring for Null.

Extracted from agent.py (P2 god-object split). Contains EvaluationMixin:
`run_evaluation` plus the four category evaluators (recall quality,
knowledge health, probe trending, session quality) and the weighted
overall score.

Mixed into AgentMemory; methods rely on the host's db / recall /
_session_manager attributes.
"""

from __future__ import annotations

from typing import Any


class EvaluationMixin:
    """Evaluation suite methods for AgentMemory."""

    def run_evaluation(self, notes: str = "") -> dict[str, Any]:
        """Run a comprehensive evaluation of Null's health and performance.

        Computes metrics across four categories, calculates an overall score,
        stores the snapshot, and returns everything including comparison to
        the last evaluation.
        """
        metrics: dict[str, Any] = {}

        # ── 1. Recall Quality ──
        recall_metrics = self._eval_recall_quality()
        metrics["recall"] = recall_metrics

        # ── 2. Knowledge Health ──
        knowledge_metrics = self._eval_knowledge_health()
        metrics["knowledge"] = knowledge_metrics

        # ── 3. Probe Trending ──
        probe_metrics = self._eval_probe_trending()
        metrics["probes"] = probe_metrics

        # ── 4. Session Quality ──
        session_metrics = self._eval_session_quality()
        metrics["sessions"] = session_metrics

        # ── Overall Score (0-100) ──
        score = self._compute_evaluation_score(metrics)
        metrics["overall_score"] = score

        # ── Store snapshot ──
        self.db.insert_evaluation(score, metrics, notes)

        # ── Compare to last evaluation ──
        evals = self.db.get_evaluations(limit=2)
        comparison = None
        if len(evals) >= 2:
            prev = evals[1]  # evals[0] is the one we just inserted
            prev_score = prev.get("score", 0)
            delta = score - prev_score
            prev_metrics = prev.get("metrics", {})
            comparison = {
                "previous_score": prev_score,
                "delta": delta,
                "direction": "improving" if delta > 0 else "degrading" if delta < 0 else "stable",
                "previous_run": prev.get("run_at", "unknown"),
                "category_deltas": {},
            }
            for cat in ("recall", "knowledge", "probes", "sessions"):
                curr_sub = metrics.get(cat, {}).get("subscore", 0)
                prev_sub = prev_metrics.get(cat, {}).get("subscore", 0)
                comparison["category_deltas"][cat] = round(curr_sub - prev_sub, 1)

        return {
            "score": score,
            "metrics": metrics,
            "comparison": comparison,
        }

    def _eval_recall_quality(self) -> dict[str, Any]:
        """Evaluate recall quality using probe results."""
        probes = self.db.get_probes()
        if not probes:
            return {"subscore": 50, "probe_count": 0,
                    "avg_rank": None, "miss_rate": None,
                    "note": "No probes configured — recall quality unknown"}

        total = 0
        ranks = []
        misses = 0

        for probe in probes:
            if probe.get("probe_type") == "system":
                continue  # Skip system probes for recall quality
            total += 1
            recall_results = self.recall(probe["question"], limit=5, _emit_event=False)
            found = False
            for i, entry in enumerate(recall_results):
                text = entry.get("fact", entry.get("mistake", ""))
                if probe["expected"].lower() in text.lower():
                    ranks.append(i + 1)
                    found = True
                    break
            if not found:
                misses += 1

        if total == 0:
            return {"subscore": 50, "probe_count": 0,
                    "avg_rank": None, "miss_rate": None,
                    "note": "No user/auto probes to evaluate"}

        avg_rank = sum(ranks) / len(ranks) if ranks else 5.0
        miss_rate = misses / total
        hit_rate = 1 - miss_rate
        # Score: 100 if all hit at rank 1, degrades with rank and misses
        rank_score = max(0, 100 - (avg_rank - 1) * 15) if ranks else 0
        subscore = round(rank_score * hit_rate)

        return {
            "subscore": subscore,
            "probe_count": total,
            "avg_rank": round(avg_rank, 2) if ranks else None,
            "miss_rate": round(miss_rate, 3),
            "hit_rate": round(hit_rate, 3),
            "hits": total - misses,
            "misses": misses,
        }

    def _eval_knowledge_health(self) -> dict[str, Any]:
        """Evaluate knowledge base health."""
        active = self.db.count_facts(active_only=True)
        total = self.db.count_facts(active_only=False)
        archived = total - active  # Rough: includes forgotten + superseded

        # Confidence distribution
        rows = self.db.conn.execute(
            """SELECT confidence FROM facts
               WHERE forgotten = 0 AND archived = 0 AND superseded_by IS NULL"""
        ).fetchall()
        confidences = [r[0] for r in rows] if rows else []
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0
        high_conf = sum(1 for c in confidences if c >= 0.8)
        low_conf = sum(1 for c in confidences if c < 0.5)

        # Staleness
        stale_row = self.db.conn.execute(
            """SELECT COUNT(*) FROM facts
               WHERE access_count = 0
                 AND forgotten = 0 AND archived = 0 AND superseded_by IS NULL
                 AND created_at < datetime('now', '-60 days')"""
        ).fetchone()
        stale_count = stale_row[0] if stale_row else 0
        stale_pct = stale_count / active if active > 0 else 0

        # Churn: facts created in last 7 days vs archived/forgotten
        recent_row = self.db.conn.execute(
            """SELECT COUNT(*) FROM facts
               WHERE created_at > datetime('now', '-7 days')"""
        ).fetchone()
        recent_created = recent_row[0] if recent_row else 0

        churned_row = self.db.conn.execute(
            """SELECT COUNT(*) FROM facts
               WHERE (forgotten = 1 OR archived = 1 OR superseded_by IS NOT NULL)
                 AND created_at > datetime('now', '-7 days')"""
        ).fetchone()
        recent_churned = churned_row[0] if churned_row else 0

        # Tier distribution
        tier_rows = self.db.conn.execute(
            """SELECT tier, COUNT(*) FROM facts
               WHERE forgotten = 0 AND archived = 0 AND superseded_by IS NULL
               GROUP BY tier"""
        ).fetchall()
        tiers = {r[0] or "contextual": r[1] for r in tier_rows}

        # Score: high confidence is good, low staleness is good, growth is good
        conf_score = min(100, avg_confidence * 100)
        freshness_score = max(0, 100 - stale_pct * 200)  # 50% stale = 0
        growth_score = min(100, recent_created * 5) if recent_created > 0 else 20
        subscore = round((conf_score * 0.4 + freshness_score * 0.3 + growth_score * 0.3))

        return {
            "subscore": subscore,
            "active_facts": active,
            "total_facts": total,
            "archived_or_removed": archived,
            "avg_confidence": round(avg_confidence, 3),
            "high_confidence_facts": high_conf,
            "low_confidence_facts": low_conf,
            "stale_facts": stale_count,
            "stale_pct": round(stale_pct, 3),
            "recent_7d_created": recent_created,
            "recent_7d_churned": recent_churned,
            "tiers": tiers,
        }

    def _eval_probe_trending(self) -> dict[str, Any]:
        """Evaluate probe health and trending."""
        probes = self.db.get_probes()
        if not probes:
            return {"subscore": 50, "total_probes": 0,
                    "note": "No probes configured"}

        total = len(probes)
        ever_run = [p for p in probes if p.get("run_count", 0) > 0]
        never_run = total - len(ever_run)

        # Pass rates
        always_passing = 0
        currently_failing = 0
        regressed = 0  # Used to pass, now failing
        never_passed = 0

        for p in ever_run:
            run_count = p.get("run_count", 0)
            pass_count = p.get("pass_count", 0)
            last_result = p.get("last_result", "")
            pass_rate = pass_count / run_count if run_count > 0 else 0

            if pass_rate == 1.0:
                always_passing += 1
            elif pass_count == 0:
                never_passed += 1
            elif last_result == "fail" and pass_rate > 0:
                regressed += 1  # Used to pass sometimes, now failing

            if last_result == "fail":
                currently_failing += 1

        # Auto-probe coverage
        facts_with_entities = self.db.conn.execute(
            """SELECT COUNT(DISTINCT fact_id) FROM probes
               WHERE probe_type = 'auto' AND fact_id IS NOT NULL"""
        ).fetchone()
        auto_coverage = facts_with_entities[0] if facts_with_entities else 0

        # Score: penalize failures and regressions. The regression penalty
        # is proportional to the regressed FRACTION and bounded at 30 —
        # a flat `regressed * 10` saturated to 0 for any deployment with
        # a few hundred auto-probes and a normal failure tail, making the
        # metric useless exactly when there is enough data to be useful.
        if len(ever_run) > 0:
            current_pass_rate = (len(ever_run) - currently_failing) / len(ever_run)
            regression_penalty = min(30.0, 100.0 * regressed / len(ever_run))
        else:
            current_pass_rate = 1.0
            regression_penalty = 0.0
        subscore = round(max(0, current_pass_rate * 100 - regression_penalty))

        return {
            "subscore": subscore,
            "total_probes": total,
            "ever_run": len(ever_run),
            "never_run": never_run,
            "always_passing": always_passing,
            "currently_failing": currently_failing,
            "regressed": regressed,
            "never_passed": never_passed,
            "current_pass_rate": round(current_pass_rate, 3),
            "auto_probe_coverage": auto_coverage,
            "by_type": {
                "system": len([p for p in probes if p["probe_type"] == "system"]),
                "auto": len([p for p in probes if p["probe_type"] == "auto"]),
                "user": len([p for p in probes if p["probe_type"] == "user"]),
            },
        }

    def _eval_session_quality(self) -> dict[str, Any]:
        """Evaluate session patterns and quality."""
        if self._session_manager is None:
            return {"subscore": 50, "total_sessions": 0,
                    "note": "No session manager initialized"}
        sessions = self._session_manager.list_sessions(limit=50)
        if not sessions:
            return {"subscore": 50, "total_sessions": 0,
                    "note": "No session history"}

        total = len(sessions)
        completed = sum(1 for s in sessions if s.status == "completed")
        crashed = sum(1 for s in sessions if s.status == "crashed")
        crash_rate = crashed / total if total > 0 else 0

        # Facts per session
        facts_per = [s.facts_created for s in sessions]
        avg_facts = sum(facts_per) / len(facts_per) if facts_per else 0

        # Mistakes
        mistake_count = self.db.count_mistakes()
        decision_count = self.db.count_decisions()
        reflection_count = self.db.count_reflections()

        # Score: low crash rate is good, consistent facts creation is good
        crash_score = max(0, 100 - crash_rate * 200)
        productivity_score = min(100, avg_facts * 10) if avg_facts > 0 else 20
        subscore = round(crash_score * 0.6 + productivity_score * 0.4)

        return {
            "subscore": subscore,
            "total_sessions": total,
            "completed": completed,
            "crashed": crashed,
            "crash_rate": round(crash_rate, 3),
            "avg_facts_per_session": round(avg_facts, 1),
            "total_mistakes": mistake_count,
            "total_decisions": decision_count,
            "total_reflections": reflection_count,
        }

    def _compute_evaluation_score(self, metrics: dict) -> float:
        """Compute overall score from category subscores.

        Weights: recall 30%, knowledge 25%, probes 25%, sessions 20%
        """
        weights = {
            "recall": 0.30,
            "knowledge": 0.25,
            "probes": 0.25,
            "sessions": 0.20,
        }
        weighted = 0.0
        for cat, weight in weights.items():
            sub = metrics.get(cat, {}).get("subscore", 50)
            weighted += sub * weight
        return round(weighted, 1)
