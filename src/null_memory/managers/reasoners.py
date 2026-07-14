"""Reasoner implementations.

v1 — RuleReasoner: handcoded scoring. **Sync** by design — zero LLM
cost, zero latency, deterministic for tests.

v2 (future) — LocalLLMReasoner, AnthropicReasoner, HybridReasoner.
Those will typically be **async** because they do network I/O.

Both shapes satisfy the ``Reasoner`` Protocol — its methods declare a
``T | Awaitable[T]`` return union. Managers that consume reasoners
through ``Manager._resolve`` / ``Manager._resolve_async`` work
transparently with either kind.
"""

from __future__ import annotations


from null_memory.managers.base import ReasonerContext, ScoreResult


class RuleReasoner:
    """Rule-based scorer used as the v1 default for every manager.

    Concrete rubrics live in each manager's subclass method. This class
    only knows how to aggregate (hard constraints, soft weights,
    conflicts) — the domain semantics come from the rubric function
    each manager plugs in.

    Managers call ``score_with_rubric(item, context, rubric)`` where
    rubric is a callable returning the matched/conflicts/hard_failed
    structure described below.
    """

    def score(self, item: dict, context: ReasonerContext) -> ScoreResult:
        # Default implementation returns neutral — Managers typically
        # override by calling score_with_rubric with their own rubric
        # function. This base return exists so Reasoner Protocol is
        # satisfied even for toy/stubbed managers.
        return ScoreResult(score=0.5, rationale="RuleReasoner base — no rubric bound")

    def score_with_rubric(
        self, item: dict, context: ReasonerContext, rubric,
    ) -> ScoreResult:
        """Apply a manager-specific rubric and aggregate the result.

        rubric(item, context) must return a dict with keys:
          matched:          list[str] — soft matches (each +0.1 up to cap)
          conflicts:        list[str] — soft conflicts (each -0.15)
          hard_failed:      list[str] — hard constraint failures
          continuous:       dict[name, 0-1] — weighted continuous scores
          weights:          dict[name, float] — weight per continuous key
          base:             float — starting score (default 0.3)
        """
        out = rubric(item, context)
        if out.get("hard_failed"):
            return ScoreResult(
                score=0.0,
                rationale="hard constraint failed: "
                         + ", ".join(out["hard_failed"]),
                matched=out.get("matched", []),
                conflicts=out.get("hard_failed", []),
                hard_constraint_failed=True,
            )

        score = float(out.get("base", 0.3))
        matched = out.get("matched", [])
        conflicts = out.get("conflicts", [])
        score += min(0.4, 0.1 * len(matched))      # soft-match cap 0.4
        score -= 0.15 * len(conflicts)

        weights = out.get("weights", {})
        for name, raw in out.get("continuous", {}).items():
            w = weights.get(name, 0.1)
            score += w * float(raw)

        score = max(0.0, min(1.0, score))
        rationale_parts = []
        if matched:
            rationale_parts.append("matched: " + ", ".join(matched))
        if conflicts:
            rationale_parts.append("conflicts: " + ", ".join(conflicts))
        rationale = "; ".join(rationale_parts) or "no strong signals"
        return ScoreResult(
            score=score, rationale=rationale,
            matched=matched, conflicts=conflicts,
        )

    def digest(self, items: list[dict], context: ReasonerContext) -> str:
        """Simple count + top-N summary. v2 will do real prose."""
        if not items:
            return f"{context.manager_name}: nothing observed."
        lines = [f"{context.manager_name}: {len(items)} observations this window."]
        # Show top 3 by score if present
        scored = [it for it in items if "score" in it]
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        for item in scored[:3]:
            lines.append(
                f"  [{item.get('score', 0):.2f}] "
                f"{item.get('title', item.get('summary', '(no title)'))[:100]}"
            )
        return "\n".join(lines)

    def compose(self, subject: str, body_context: dict,
                context: ReasonerContext) -> tuple[str, str]:
        """Default composition from template fields. Manager subclasses
        typically provide richer composition."""
        return subject, body_context.get("default_body", subject)
