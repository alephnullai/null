"""Hypnos — sleep/dream memory maintenance system for Null Memory.

Runs during downtime (cron nightly, manual trigger, or auto-idle).
Processes memories through 4 stages inspired by human sleep:

  Stage 1: Decay Sweep    (NREM cleanup — archive low-confidence old facts)
  Stage 2: Tier Changes   (promote/demote facts based on usage patterns)
  Stage 3: Salience       (REM — boost impact for emotionally significant facts)
  Stage 4: Cold Storage   (archive truly dormant facts)

No LLM calls — pure heuristics. Fast, cron-safe, idempotent.

Scheduling: Hypnos is the BATCH engine — it runs when explicitly invoked
(cron nightly, ``null hypnos``, wakeup hook) in the caller's foreground,
so it needs no leader election. Continuous 60s maintenance is
hypnos_live.HypnosLiveWorker; the 15-minute outer loop is
daemon.DaemonRunner. Candidate selection for the archive sweeps lives in
null_memory.memory.maintenance_actions (shared, pure, unit-tested) —
this module is the scheduler that applies the results.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from null_memory.memory.maintenance_actions import (
    cold_storage_candidates,
    decay_archive_candidates,
    jaccard_words as _jaccard_words,
)


@dataclass
class HypnosResult:
    """Summary of a single Hypnos run."""

    run_id: str
    started_at: str
    completed_at: str = ""
    stage1_archived: int = 0
    stage2_promoted: int = 0
    stage2_demoted: int = 0
    stage3_boosted: int = 0
    stage3_relationships: int = 0
    stage4_cold_stored: int = 0
    stage5_synthesized: int = 0
    stage6_identity_patches: int = 0
    # Stage 4.5 (id=45) — crystallization. Runs between cold-storage (4)
    # and synthesis (5) so synthesis benefits from atomized inputs. Kept
    # numerically as 45 to avoid renumbering existing stages.
    stage45_crystallized: int = 0
    stage45_skipped: int = 0
    stage45_archived_parents: int = 0
    # Stage 8 — worktree doc audit. Scans CLAUDE.md / ATLAS_HANDOFF*.md
    # for claims about live system state, verifies, marks refuted/stale.
    stage8_docs_audited: int = 0
    stage8_claims_extracted: int = 0
    stage8_claims_refuted: int = 0
    stage8_claims_stale: int = 0
    total_active: int = 0
    total_archived: int = 0
    errors: list[str] = field(default_factory=list)


class Hypnos:
    """Memory maintenance engine — runs the 4 sleep stages."""

    def __init__(self, mem: Any):
        self.mem = mem
        self.db = mem.db
        self.config = dict(mem.config)
        self.run_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat()

    def run(self, stages: list[int] | None = None) -> HypnosResult:
        """Execute all (or selected) sleep stages. Returns result summary."""
        if stages is None:
            # Default execution order. Stage 5 (synthesis) is gated by
            # hypnos_synthesis_enabled config; left out of default list.
            # Stage 45 (crystallization) sits between 4 and 5 numerically.
            # Stage 8 (doc audit) runs last — after identity patches.
            stages = [1, 2, 3, 4, 45, 6, 8]

        result = HypnosResult(
            run_id=self.run_id,
            started_at=self.started_at,
        )

        try:
            if 1 in stages:
                result.stage1_archived = self._stage1_decay_sweep()
            if 2 in stages:
                result.stage2_promoted, result.stage2_demoted = (
                    self._stage2_tier_changes()
                )
            if 3 in stages:
                result.stage3_boosted, result.stage3_relationships = (
                    self._stage3_salience()
                )
            if 4 in stages:
                result.stage4_cold_stored = self._stage4_cold_storage()
            if 45 in stages:
                (result.stage45_crystallized,
                 result.stage45_skipped,
                 result.stage45_archived_parents) = self._stage45_crystallize()
            if 5 in stages and self.config.get("hypnos_synthesis_enabled", False):
                result.stage5_synthesized = self._stage5_synthesis()
            if 6 in stages:
                result.stage6_identity_patches = self._stage6_identity()
            if 8 in stages:
                (result.stage8_docs_audited,
                 result.stage8_claims_extracted,
                 result.stage8_claims_refuted,
                 result.stage8_claims_stale) = self._stage8_doc_audit()
        except Exception as e:
            result.errors.append(str(e))

        self.db.conn.commit()

        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.total_active = self.db.count_facts(active_only=True)
        result.total_archived = self._count_archived()

        # Always write a completion entry so wakeup can find this run
        self._journal("run", "completed",
                      detail=f"active={result.total_active}, archived={result.total_archived}")
        self.db.conn.commit()

        return result

    def _journal(self, stage: str, action: str,
                 fact_id: str | None = None,
                 detail: str | None = None) -> None:
        """Record a journal entry for this run."""
        self.db.insert_hypnos_entry(
            self.run_id, stage, action, fact_id=fact_id, detail=detail,
        )

    def _count_archived(self) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE archived = 1"
        ).fetchone()
        return row[0] if row else 0

    # ── Stage 1: Decay Sweep ──

    def _stage1_decay_sweep(self) -> int:
        """Archive facts below threshold that are old and untouched.

        Candidate selection is the shared pure action; this stage applies
        the archives + journals them."""
        threshold = self.config.get("hypnos_decay_archive_threshold", 0.05)
        min_age_days = self.config.get("hypnos_decay_min_age_days", 60)
        now = datetime.now(timezone.utc)
        archived = 0

        facts = self.db.get_active_facts()
        eff_conf = {f["id"]: self.mem.effective_confidence(f) for f in facts}
        for fact, detail in decay_archive_candidates(
            facts, eff_conf, now,
            threshold=threshold, min_age_days=min_age_days,
        ):
            self.db.archive_fact(fact["id"])
            action = "archived" if "age=" in detail else "archived_ultra_low"
            self._journal("decay", action, fact["id"], detail)
            archived += 1

        return archived

    # ── Stage 2: Tier Promotion/Demotion ──

    def _stage2_tier_changes(self) -> tuple[int, int]:
        """Promote contextual->durable->core or demote durable->contextual. Never touch core."""
        promoted = 0
        demoted = 0
        now = datetime.now(timezone.utc)

        access_thresh = self.config.get("hypnos_promote_access_threshold", 10)
        verify_thresh = self.config.get("hypnos_promote_verify_threshold", 2)
        decision_ref_thresh = self.config.get(
            "hypnos_promote_decision_ref_threshold", 2
        )
        demote_days = self.config.get("hypnos_demote_idle_days", 60)

        # Core patterns for durable→core promotion — deployment-aware
        # (identity entities come from identity.json, not hardcoded).
        from null_memory.classifier import get_core_patterns
        _CORE_PATTERNS = get_core_patterns(
            identity_terms=self.mem.identity.get("identity_terms"),
            agent_name=self.mem.identity.get("name"),
        )

        facts = self.db.get_active_facts()
        decision_sessions = self._get_decision_session_ids()
        mistake_sessions = self._get_mistake_session_ids()

        for fact in facts:
            tier = fact.get("tier", "contextual")
            fact_id = fact["id"]
            access_count = fact.get("access_count", 0)
            session_id = fact.get("session_id")

            # Core tier — never demote, never touch
            if tier == "core":
                continue

            if tier == "contextual":
                should_promote = False
                reason = ""

                if access_count >= access_thresh:
                    should_promote = True
                    reason = f"access_count={access_count}"

                if not should_promote and fact.get("last_verified"):
                    should_promote = True
                    reason = "verified"

                if (not should_promote and session_id
                        and decision_sessions.get(session_id, 0)
                        >= decision_ref_thresh):
                    should_promote = True
                    reason = f"decision_refs={decision_sessions[session_id]}"

                if should_promote:
                    self.db.conn.execute(
                        "UPDATE facts SET tier = 'durable' WHERE id = ?",
                        (fact_id,),
                    )
                    self._journal("tier", "promoted", fact_id,
                                  f"contextual->durable: {reason}")
                    self.mem._emit_store_event("hypnos.promote", fact_id,
                                               {"tier": "durable"})
                    promoted += 1

            elif tier == "durable":
                # Check for promotion to core: high access + high impact + matches core pattern
                fact_text = fact.get("fact", "")
                impact = fact.get("impact", 0.5)
                if access_count >= 20 and impact >= 0.8:
                    matches_core = any(p.search(fact_text) for p, _ in _CORE_PATTERNS)
                    if matches_core:
                        self.db.conn.execute(
                            "UPDATE facts SET tier = 'core' WHERE id = ?",
                            (fact_id,),
                        )
                        self._journal("tier", "promoted", fact_id,
                                      f"durable->core: access={access_count}, impact={impact:.1f}")
                        self.mem._emit_store_event("hypnos.promote", fact_id,
                                                   {"tier": "core"})
                        promoted += 1
                        continue

                # Check for demotion
                if access_count > 0:
                    continue

                if session_id and (session_id in decision_sessions
                                   or session_id in mistake_sessions):
                    continue

                ts = fact.get("last_accessed") or fact.get("created_at", "")
                idle_days = self._age_days(ts, now)

                if idle_days >= demote_days:
                    self.db.conn.execute(
                        "UPDATE facts SET tier = 'contextual' WHERE id = ?",
                        (fact_id,),
                    )
                    self._journal("tier", "demoted", fact_id,
                                  f"durable->contextual: idle {idle_days:.0f}d")
                    self.mem._emit_store_event("hypnos.demote", fact_id,
                                               {"tier": "contextual"})
                    demoted += 1

        return promoted, demoted

    # ── Stage 3: Salience Recomputation ──

    def _stage3_salience(self) -> tuple[int, int]:
        """Boost impact for decision/mistake-linked facts. Link co-session facts."""
        boosted = 0
        relationships = 0

        decision_boost = self.config.get("hypnos_decision_impact_boost", 0.2)
        mistake_boost = self.config.get("hypnos_mistake_impact_boost", 0.3)

        facts = self.db.get_active_facts()
        decision_sessions = self._get_decision_session_ids()
        mistake_sessions = self._get_mistake_session_ids()

        for fact in facts:
            session_id = fact.get("session_id")
            if not session_id:
                continue

            current_impact = fact.get("impact", 0.5)
            boost = 0.0

            if session_id in mistake_sessions:
                boost = mistake_boost
            elif session_id in decision_sessions:
                boost = decision_boost

            if boost > 0:
                new_impact = min(1.0, current_impact + boost)
                if new_impact > current_impact:
                    self.db.conn.execute(
                        "UPDATE facts SET impact = ? WHERE id = ?",
                        (new_impact, fact["id"]),
                    )
                    self._journal("salience", "boosted", fact["id"],
                                  f"impact {current_impact:.2f}->{new_impact:.2f}")
                    boosted += 1

        # Boost from positive reflections
        boosted += self._boost_from_reflections(facts)

        # Link co-session facts
        relationships = self._link_co_session(facts)

        return boosted, relationships

    def _boost_from_reflections(self, facts: list[dict]) -> int:
        """Mild boost for facts from sessions with positive reflections."""
        boosted = 0
        reflections = self.db.get_reflections()

        good_sessions: set[str] = set()
        for r in reflections:
            if r.get("went_well") and r.get("session_id"):
                good_sessions.add(r["session_id"])

        if not good_sessions:
            return 0

        for fact in facts:
            sid = fact.get("session_id")
            if sid and sid in good_sessions:
                current = fact.get("impact", 0.5)
                new_impact = min(1.0, current + 0.1)
                if new_impact > current:
                    self.db.conn.execute(
                        "UPDATE facts SET impact = ? WHERE id = ?",
                        (new_impact, fact["id"]),
                    )
                    self._journal("salience", "reflection_boost", fact["id"],
                                  "positive reflection session")
                    boosted += 1

        return boosted

    def _link_co_session(self, facts: list[dict]) -> int:
        """Link facts that share a session_id (co-recall proxy)."""
        linked = 0

        session_facts: dict[str, list[str]] = {}
        for fact in facts:
            sid = fact.get("session_id")
            if sid:
                session_facts.setdefault(sid, []).append(fact["id"])

        # Only link sessions with 2-5 facts (>5 is too noisy)
        for _sid, fact_ids in session_facts.items():
            if not (2 <= len(fact_ids) <= 5):
                continue
            for i, fid in enumerate(fact_ids):
                existing = set(self.db.get_related_ids(fid))
                for other_id in fact_ids[i + 1:]:
                    if other_id not in existing:
                        self.db.add_relationship(fid, other_id)
                        self.db.add_relationship(other_id, fid)
                        linked += 1

        if linked:
            self._journal("salience", "linked",
                          detail=f"{linked} new relationships")

        return linked

    # ── Stage 4.5: Crystallization (id=45) ──

    def _stage45_crystallize(self) -> tuple[int, int, int]:
        """Split verbose facts (>300 chars) into atomic children.

        Returns:
            (children_created, skipped, parents_archived)

        Skipped covers: anchor-immune, already-crystallized, suspicious
        LLM output, LLM call failure. Anything that left the parent
        untouched without writing children.

        Knobs (config or env):
          • hypnos_crystallize_dryrun (or env HYPNOS_CRYSTALLIZE_DRYRUN=1):
            log what would happen but write nothing.
          • hypnos_crystallize_max_per_pass: cap parents touched per pass.
            Hard kill switch: if this many archives would exceed the cap,
            abort early (returns whatever was done before the cap).
        """
        import json as _json
        import os as _os
        from null_memory.crystallize import (
            crystallize_fact,
            default_llm_call,
        )

        dry_run = bool(
            _os.environ.get("HYPNOS_CRYSTALLIZE_DRYRUN") == "1"
            or self.config.get("hypnos_crystallize_dryrun", True)
        )
        max_per_pass = int(
            self.config.get("hypnos_crystallize_max_per_pass", 50)
        )
        llm_call = self.config.get(
            "hypnos_crystallize_llm",  # tests inject a stub here
            default_llm_call,
        )

        children_created = 0
        skipped = 0
        archived = 0

        # Crystallization is unified-substrate-only. Legacy per-
        # personality DBs predate the anchor columns and crystallized_*
        # columns, so the SELECT below would crash on schema mismatch.
        if not getattr(self.db, "unified", False):
            return children_created, skipped, archived

        # Candidate set: active facts >= MIN_LEN_TO_CRYSTALLIZE that
        # haven't already been crystallized. Anchor-immunity is a
        # property check inside crystallize_fact, so we pass everything
        # and let the function filter.
        from null_memory.crystallize import MIN_LEN_TO_CRYSTALLIZE
        rows = self.db.conn.execute(
            """SELECT id, fact, confidence, base_confidence, project,
                      source, provenance, impact, session_id, tier,
                      anchor_type, anchor_note, anchor_at,
                      crystallized_into, forgotten, archived, superseded_by
               FROM facts
               WHERE archived = 0 AND forgotten = 0
                 AND superseded_by IS NULL
                 AND crystallized_into IS NULL
                 AND LENGTH(fact) >= ?
               ORDER BY LENGTH(fact) DESC""",
            (MIN_LEN_TO_CRYSTALLIZE,),
        ).fetchall()

        for row in rows:
            if archived >= max_per_pass:
                self._journal(
                    "crystallize", "kill_switch_hit",
                    detail=f"reached cap {max_per_pass} parents/pass",
                )
                break

            parent = {
                "id": row["id"], "fact": row["fact"],
                "confidence": row["confidence"],
                "base_confidence": row["base_confidence"],
                "project": row["project"], "source": row["source"],
                "provenance": row["provenance"], "impact": row["impact"],
                "session_id": row["session_id"], "tier": row["tier"],
                "anchor_type": row["anchor_type"],
                "anchor_note": row["anchor_note"],
                "anchor_at": row["anchor_at"],
                "crystallized_into": row["crystallized_into"],
                "forgotten": row["forgotten"],
                "archived": row["archived"],
                "superseded_by": row["superseded_by"],
            }

            children = crystallize_fact(parent, llm_call)
            if children is None:
                skipped += 1
                continue

            if dry_run:
                self._journal(
                    "crystallize", "dryrun_would_split", parent["id"],
                    f"would_create={len(children)} parent_len={len(parent['fact'])}",
                )
                continue

            # Real path: insert children, mark parent.
            now = datetime.now(timezone.utc).isoformat()
            child_ids: list[str] = []
            for child in children:
                child_id = hashlib.sha256(
                    f"{child['project']}:{child['fact'].strip().lower()}"
                    .encode("utf-8")
                ).hexdigest()[:12]
                child_ids.append(child_id)
                # Direct INSERT — db.insert_fact doesn't know about the
                # crystallized_from column.
                self.db.conn.execute(
                    """INSERT OR IGNORE INTO facts
                       (id, fact, confidence, base_confidence, project,
                        source, provenance, impact, session_id, created_at,
                        access_count, tier, anchor_type, anchor_note,
                        anchor_at, crystallized_from, archived, forgotten)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, 0)""",
                    (
                        child_id, child["fact"],
                        child["confidence"], child["confidence"],
                        child["project"], child["source"],
                        child["provenance"], child["impact"],
                        child["session_id"], now,
                        child["tier"], child["anchor_type"],
                        child["anchor_note"], child["anchor_at"],
                        child["crystallized_from"],
                    ),
                )
                if getattr(self.db, "unified", False):
                    self.db.conn.execute(
                        """INSERT OR REPLACE INTO personality_views
                           (fact_id, personality, last_accessed,
                            access_count, tags)
                           VALUES (?, ?, ?, 0, '[]')""",
                        (child_id, getattr(self.db, "personality", "atlas"),
                         now),
                    )
                # Crystallize creates knowledge — children are fact.add
                # events (issue #20).
                self.mem._emit_store_event("fact.add", child_id, {
                    k: v for k, v in {
                        "fact": child["fact"],
                        "confidence": child["confidence"],
                        "base_confidence": child["confidence"],
                        "project": child["project"],
                        "source": child["source"],
                        "provenance": child["provenance"],
                        "impact": child["impact"],
                        "session_id": child["session_id"],
                        "created_at": now,
                        "tier": child["tier"],
                        "anchor_type": child["anchor_type"],
                        "anchor_note": child["anchor_note"],
                        "anchor_at": child["anchor_at"],
                        "crystallized_from": child["crystallized_from"],
                    }.items() if v is not None
                })
                children_created += 1

            # Mark parent: archived + crystallized_into = [child_ids]
            self.db.conn.execute(
                """UPDATE facts
                   SET archived = 1,
                       crystallized_into = ?
                   WHERE id = ?""",
                (_json.dumps(child_ids), parent["id"]),
            )
            self.mem._emit_store_event("fact.update", parent["id"], {
                "archived": 1,
                "crystallized_into": child_ids,
            })
            archived += 1
            self._journal(
                "crystallize", "split", parent["id"],
                f"created={len(children)} ids={','.join(child_ids[:3])}",
            )

        if dry_run:
            self._journal("crystallize", "dryrun_summary",
                          detail=f"candidates={len(rows)} skipped={skipped}")

        return children_created, skipped, archived

    # ── Stage 4: Cold Storage Sweep ──

    def _stage4_cold_storage(self) -> int:
        """Archive old, untouched, low-confidence facts (shared action)."""
        age_thresh = self.config.get("hypnos_cold_storage_age_days", 90)
        conf_thresh = self.config.get(
            "hypnos_cold_storage_confidence_threshold", 0.3
        )
        now = datetime.now(timezone.utc)
        archived = 0

        facts = self.db.get_active_facts()
        eff_conf = {f["id"]: self.mem.effective_confidence(f) for f in facts}
        for fact, detail in cold_storage_candidates(
            facts, eff_conf, now,
            min_age_days=age_thresh, conf_threshold=conf_thresh,
        ):
            self.db.archive_fact(fact["id"])
            self._journal("cold_storage", "archived", fact["id"], detail)
            archived += 1

        return archived

    # ── Stage 5: Knowledge Synthesis (LLM-powered) ──

    def _stage5_synthesis(self) -> int:
        """Cluster related facts and synthesize principles via LLM."""
        max_clusters = self.config.get("hypnos_synthesis_max_clusters", 5)
        min_cluster = self.config.get("hypnos_synthesis_min_cluster_size", 3)
        synthesized = 0

        emb = self.mem.embeddings
        if emb is None:
            return 0

        # Load API key
        api_key = self._load_synthesis_api_key()
        if not api_key:
            return 0

        facts = self.db.get_active_facts()
        if len(facts) < 10:
            return 0

        # Group by project
        by_project: dict[str, list[dict]] = {}
        for f in facts:
            proj = f.get("project", "global")
            by_project.setdefault(proj, []).append(f)

        import hashlib

        clusters_processed = 0
        for proj, proj_facts in by_project.items():
            if len(proj_facts) < 10 or clusters_processed >= max_clusters:
                continue

            # Get embeddings for this project's facts
            fact_ids = [f["id"] for f in proj_facts]
            emb_map = emb.get_embeddings_batch(fact_ids)
            if len(emb_map) < 10:
                continue

            # Simple clustering: find groups of similar facts
            clusters = self._cluster_facts(proj_facts, emb_map, emb, min_cluster)

            for cluster in clusters:
                if clusters_processed >= max_clusters:
                    break

                fact_texts = [f["fact"] for f in cluster]
                principle = self._synthesize_cluster(api_key, fact_texts)
                if not principle:
                    continue

                # Store as new fact
                now = datetime.now(timezone.utc).isoformat()
                content = f"[synthesized] {principle}"
                fid = hashlib.sha256(
                    f"{content}:{proj}".encode()
                ).hexdigest()[:16]

                self.db.insert_fact({
                    "id": fid,
                    "fact": content,
                    "confidence": 0.85,
                    "base_confidence": 0.85,
                    "project": proj,
                    "source": "observation",
                    "provenance": "synthesized",
                    "impact": 0.8,
                    "tier": "durable",
                    "created_at": now,
                })
                self.mem._emit_store_event("hypnos.synthesis", fid, {
                    "fact": content,
                    "confidence": 0.85,
                    "base_confidence": 0.85,
                    "project": proj,
                    "source": "observation",
                    "provenance": "synthesized",
                    "impact": 0.8,
                    "tier": "durable",
                    "created_at": now,
                })

                # Link source facts to synthesized fact
                for sf in cluster:
                    self.db.add_relationship(sf["id"], fid)

                self._journal("synthesis", "synthesized", fid,
                              f"from {len(cluster)} facts in {proj}")
                synthesized += 1
                clusters_processed += 1

        return synthesized

    def _cluster_facts(self, facts: list[dict], emb_map: dict,
                       emb: Any, min_size: int) -> list[list[dict]]:
        """Simple greedy clustering by cosine similarity."""
        try:
            import numpy as np
        except ImportError:
            return []

        # Only use facts with embeddings
        facts_with_emb = [f for f in facts if f["id"] in emb_map]
        if len(facts_with_emb) < min_size:
            return []

        used: set[str] = set()
        clusters: list[list[dict]] = []

        for anchor in facts_with_emb:
            if anchor["id"] in used:
                continue
            anchor_vec = emb_map[anchor["id"]]

            cluster = [anchor]
            used.add(anchor["id"])

            for candidate in facts_with_emb:
                if candidate["id"] in used:
                    continue
                cand_vec = emb_map[candidate["id"]]
                sim = float(emb.cosine_similarity(anchor_vec, cand_vec))
                if sim >= 0.5:
                    cluster.append(candidate)
                    used.add(candidate["id"])
                    if len(cluster) >= 8:  # Cap cluster size
                        break

            if len(cluster) >= min_size:
                clusters.append(cluster)

        return clusters

    def _synthesize_cluster(self, api_key: str,
                            fact_texts: list[str]) -> str | None:
        """Call LLM to synthesize a principle from a cluster of facts."""
        facts_block = "\n".join(f"- {t[:200]}" for t in fact_texts)
        prompt = (
            "You are a knowledge synthesizer. Given these related facts, extract "
            "ONE concise principle (1-2 sentences) that captures the underlying "
            "pattern or lesson. Do not summarize — synthesize.\n\n"
            f"Facts:\n{facts_block}\n\nPrinciple:"
        )

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception:
            return None

    def _load_synthesis_api_key(self) -> str | None:
        """Load ANTHROPIC_API_KEY for synthesis calls."""
        import os
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            return key
        env_paths = [
            os.path.expanduser("~/Repos/.env"),
            os.path.expanduser("~/.env"),
        ]
        for path in env_paths:
            if os.path.isfile(path):
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ANTHROPIC_API_KEY="):
                            return line.split("=", 1)[1].strip().strip("'\"")
        return None

    # ── Stage 6: Identity Patches (heuristic, no LLM) ──

    def _stage6_identity(self) -> int:
        """Review recent reflections and decisions, patch identity.json."""

        patches = 0
        now = datetime.now(timezone.utc)
        cutoff = (now - __import__("datetime").timedelta(hours=24)).isoformat()

        identity = dict(self.mem.identity)
        anti_patterns = list(identity.get("anti_patterns", []))
        capabilities = list(identity.get("capabilities", []))
        working_style = dict(identity.get("working_style", {}))

        # Get recent reflections
        reflections = self.db.conn.execute(
            "SELECT went_well, missed, do_differently FROM reflections WHERE created_at > ?",
            (cutoff,),
        ).fetchall()

        if not reflections:
            return 0

        # Collect missed/do_differently entries
        improvement_texts = []
        for r in reflections:
            if r[1]:  # missed
                improvement_texts.append(r[1])
            if r[2]:  # do_differently
                improvement_texts.append(r[2])

        # Check for new anti-patterns (mentioned in 2+ reflections, not already known)
        for text in improvement_texts:
            # Skip if already similar to existing anti-pattern
            already_known = any(
                _jaccard_words(text, existing) > 0.6
                for existing in anti_patterns
            )
            if already_known:
                continue

            # Check if this theme appears in other improvement texts
            similar_count = sum(
                1 for other in improvement_texts
                if other != text and _jaccard_words(text, other) > 0.3
            )
            if similar_count >= 1:  # Mentioned in at least 2 entries
                anti_patterns.append(text[:100])
                self._journal("identity", "anti_pattern_added",
                              detail=text[:80])
                patches += 1

        # Extract capabilities from went_well
        for r in reflections:
            went_well = r[0] or ""
            if not went_well:
                continue
            ww_lower = went_well.lower()
            if any(w in ww_lower for w in ("pushed back", "challenged", "questioned")):
                if "assertiveness" not in working_style:
                    working_style["assertiveness"] = "active"
                    patches += 1
            if any(w in ww_lower for w in ("built", "shipped", "created", "implemented")):
                # Extract what was built
                for keyword in ("built", "shipped", "created", "implemented"):
                    if keyword in ww_lower:
                        cap = went_well[:80]
                        if cap not in capabilities:
                            capabilities.append(cap)
                            patches += 1
                        break

        # Project focus from recent decisions
        decisions = self.db.conn.execute(
            "SELECT project, COUNT(*) as cnt FROM decisions WHERE created_at > ? GROUP BY project ORDER BY cnt DESC",
            (cutoff,),
        ).fetchall()
        if decisions and decisions[0][1] >= 3:
            working_style["current_focus"] = decisions[0][0]
            patches += 1

        # Apply patches
        if patches > 0:
            identity["anti_patterns"] = anti_patterns
            identity["capabilities"] = capabilities
            identity["working_style"] = working_style
            self.mem.identity = identity
            self.mem.save_identity()
            self._journal("identity", "patched",
                          detail=f"{patches} patches applied")

        return patches

    # ── Helpers ──

    def _get_decision_session_ids(self) -> dict[str, int]:
        """Return {session_id: count} for all decisions."""
        rows = self.db.conn.execute(
            "SELECT session_id, COUNT(*) AS cnt FROM decisions "
            "WHERE session_id IS NOT NULL GROUP BY session_id"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _get_mistake_session_ids(self) -> dict[str, int]:
        """Return {session_id: count} for all mistakes."""
        rows = self.db.conn.execute(
            "SELECT session_id, COUNT(*) AS cnt FROM mistakes "
            "WHERE session_id IS NOT NULL GROUP BY session_id"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    @staticmethod
    def _age_days(ts_str: str, now: datetime) -> float:
        """Parse a timestamp and return age in days."""
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return max(0, (now - ts).total_seconds() / 86400)
        except (ValueError, TypeError):
            return 999.0


    # ── Stage 8: Worktree Doc Audit ──

    def _stage8_doc_audit(self) -> tuple[int, int, int, int]:
        """Scan worktree docs for live-state claims, verify, mark stale.

        Returns:
            (docs_audited, claims_extracted, claims_refuted, claims_stale)

        Knobs (config):
          • hypnos_doc_audit_roots: list of root dirs (default ["~/Repos"])
          • hypnos_doc_audit_globs: list of patterns (default sensible set)
          • hypnos_doc_audit_llm: stub-injectable LLM callable
          • hypnos_doc_audit_dryrun: skip writes (default False — tests
            prove out, real usage benefits from persisting refutes)
        """
        import fnmatch as _fnmatch
        import os as _os
        from null_memory.doc_audit import audit_doc, default_llm_call

        if not getattr(self.db, "unified", False):
            return 0, 0, 0, 0  # legacy DB: no doc_claims table

        roots = self.config.get(
            "hypnos_doc_audit_roots",
            [_os.path.expanduser("~/Repos")],
        )
        patterns = self.config.get(
            "hypnos_doc_audit_globs",
            [
                "**/CLAUDE.md",
                "**/ATLAS_HANDOFF*.md",
                "**/HANDOFF*.md",
            ],
        )
        llm_call = self.config.get("hypnos_doc_audit_llm", default_llm_call)
        dry_run = bool(self.config.get("hypnos_doc_audit_dryrun", False))

        # Walk roots ourselves so we can prune node_modules / .git BEFORE
        # descending into them. glob.glob(recursive=True) walks every dir
        # first and filters after — pathological on trees with thousands
        # of node_modules subdirs.
        _PRUNE_DIRS = {"node_modules", ".git", ".venv", "venv",
                       "__pycache__", "dist", "build", ".next", ".cache"}
        # Match only the basename — patterns like "**/CLAUDE.md" reduce
        # to "CLAUDE.md" against the file name once we walk ourselves.
        name_patterns = [_os.path.basename(p) for p in patterns]
        docs_set: set[str] = set()
        for root in roots:
            for dirpath, dirnames, filenames in _os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _PRUNE_DIRS]
                for fn in filenames:
                    if any(_fnmatch.fnmatch(fn, pat) for pat in name_patterns):
                        docs_set.add(_os.path.join(dirpath, fn))
        docs = sorted(docs_set)

        if dry_run:
            self._journal(
                "doc_audit", "dryrun_summary",
                detail=f"would_audit={len(docs)}",
            )
            return 0, 0, 0, 0

        audited = 0
        extracted = 0
        refuted = 0
        stale = 0
        for path in docs:
            try:
                counts = audit_doc(path, self.db.conn, llm_call)
            except Exception as exc:  # noqa: BLE001
                self._journal(
                    "doc_audit", "error", detail=f"{path}: {exc}",
                )
                continue
            if counts.get("skipped_reason"):
                self._journal(
                    "doc_audit", "skipped",
                    detail=f"{_os.path.basename(path)}: {counts['skipped_reason']}",
                )
                continue
            audited += 1
            extracted += counts.get("extracted", 0)
            refuted += counts.get("refuted", 0)
            stale += counts.get("stale", 0)
            if counts.get("refuted"):
                self._journal(
                    "doc_audit", "refuted",
                    detail=f"{_os.path.basename(path)}: "
                           f"{counts['refuted']} refuted",
                )

        return audited, extracted, refuted, stale


# ── Wakeup Integration ──

def hypnos_wakeup_section(db: Any) -> list[str]:
    """Return lines for the wakeup display showing last Hypnos run results."""
    try:
        latest = db.get_latest_hypnos_run()
    except Exception:
        return []

    if not latest:
        return []

    actions: dict[str, int] = {}
    started = latest[0].get("started_at", "") if latest else ""
    for entry in latest:
        action = entry["action"]
        actions[action] = actions.get(action, 0) + 1

    parts = []
    arch = actions.get("archived", 0) + actions.get("archived_ultra_low", 0)
    if arch:
        parts.append(f"archived {arch}")
    if actions.get("promoted", 0):
        parts.append(f"promoted {actions['promoted']}")
    if actions.get("demoted", 0):
        parts.append(f"demoted {actions['demoted']}")
    boosted = actions.get("boosted", 0) + actions.get("reflection_boost", 0)
    if boosted:
        parts.append(f"boosted {boosted}")
    if actions.get("linked", 0):
        parts.append(f"linked {actions['linked']}")
    cold = actions.get("archived", 0)  # from cold_storage stage overlaps
    # cold_storage archived entries are counted in the arch total above

    summary = ", ".join(parts) if parts else "no changes"

    age = _age_str(started)
    return [f"Hypnos ({age}): {summary}"]


def _age_str(ts_str: str) -> str:
    """Human-readable age string from an ISO timestamp."""
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except (ValueError, TypeError):
        return "?"
