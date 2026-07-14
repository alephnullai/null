"""Maintenance passes — garbage collection, dedup, consolidation.

Extracted from agent.py (P2 god-object split). Contains MaintenanceMixin:
  * gc()           — ephemeral expiry, dedup, low-confidence archival
  * _deduplicate_knowledge_sql() — near-duplicate merge pass
  * consolidate()  — strengthen / fade / band-merge similar facts

Shared pure primitives (similarity, merge-pair selection, fade/archive
candidate selection) live in null_memory.memory.maintenance_actions —
these methods are the *scheduler* side: they pick the config, call the
actions, and apply the results to the DB.

Mixed into AgentMemory; relies on the host's db / config /
effective_confidence / _get_ts attributes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from null_memory.memory.maintenance_actions import (
    fade_candidates,
    find_band_merge_groups,
    find_merge_pairs,
)


class MaintenanceMixin:
    """GC + consolidation scheduling for AgentMemory."""

    # ── Garbage Collection ──

    def gc(self, max_facts: int | None = None) -> dict:
        """Garbage collect old, low-confidence knowledge.

        Mistakes and reflections are NEVER pruned.
        Returns stats about what was done.
        """
        if max_facts is None:
            max_str = os.environ.get("NULL_MAX_FACTS", "")
            if max_str:
                try:
                    max_facts = int(max_str)
                except ValueError:
                    max_facts = self.config.get("max_facts", 5000)
            else:
                max_facts = self.config.get("max_facts", 5000)

        facts = self.db.get_active_facts()
        original_count = len(facts)

        # Auto-expire ephemeral facts older than 24 hours
        expired_count = 0
        now = datetime.now(timezone.utc)
        for entry in facts:
            if entry.get("tier") == "ephemeral":
                ts_str = self._get_ts(entry)
                try:
                    entry_time = datetime.fromisoformat(ts_str)
                    if entry_time.tzinfo is None:
                        entry_time = entry_time.replace(tzinfo=timezone.utc)
                    age_hours = (now - entry_time).total_seconds() / 3600
                    if age_hours > 24:
                        self.db.archive_fact(entry["id"])
                        expired_count += 1
                except (ValueError, TypeError):
                    pass
        if expired_count:
            self.db.conn.commit()

        # Re-fetch after ephemeral expiry
        facts = self.db.get_active_facts()

        # Dedup (65%+ Jaccard)
        deduped = self._deduplicate_knowledge_sql(facts)

        # Re-fetch after dedup
        facts = self.db.get_active_facts()

        if len(facts) <= max_facts:
            return {
                "original": original_count,
                "archived": 0,
                "expired": expired_count,
                "merged": deduped,
                "remaining": len(facts),
            }

        # Archive low-confidence facts
        archive_threshold = self.config.get("gc_archive_threshold", 0.1)
        archived_count = 0
        for entry in facts:
            eff_conf = self.effective_confidence(entry)
            if eff_conf < archive_threshold:
                self.db.archive_fact(entry["id"])
                archived_count += 1

        # If still over max, archive the weakest
        facts = self.db.get_active_facts()
        if len(facts) > max_facts:
            scored = [(self.effective_confidence(e), e) for e in facts]
            scored.sort(key=lambda x: x[0])
            to_archive = scored[:len(facts) - max_facts]
            for _, entry in to_archive:
                self.db.archive_fact(entry["id"])
                archived_count += 1

        self.db.conn.commit()

        return {
            "original": original_count,
            "archived": archived_count,
            "expired": expired_count,
            "merged": deduped,
            "remaining": self.db.count_facts(),
        }

    def _deduplicate_knowledge_sql(self, facts: list[dict]) -> int:
        """Remove near-duplicate facts. Returns count merged.

        Preferred path (P2-15): vectorized cosine over the embeddings we
        already store — exact neighbor search at numpy speed instead of
        the O(n²) Python-loop pairwise Jaccard sweep (12.5M comparisons
        at 5k facts). Jaccard find_merge_pairs remains the fallback when
        embeddings are unavailable.
        """
        pairs = self._cosine_merge_pairs(facts)
        if pairs is None:
            pairs = find_merge_pairs(
                facts,
                threshold=self.config.get("dedup_jaccard_threshold", 0.65),
            )
        for winner, loser, _score in pairs:
            self.db.supersede_fact(loser["id"], winner["id"])

        if pairs:
            self.db.conn.commit()
            # Dedup merges are knowledge restructure — evented (issue #20).
            for winner, loser, _score in pairs:
                self._emit_store_event("fact.update", loser["id"],
                                       {"superseded_by": winner["id"]})

        return len(pairs)

    def _cosine_merge_pairs(self, facts: list[dict],
                            min_words: int = 3):
        """Vectorized duplicate pairs via stored embeddings.

        Returns [(winner, loser, score), ...] with the same greedy
        semantics as find_merge_pairs (a loser can't merge again), or
        None when embeddings are unavailable so the caller falls back
        to Jaccard.

        Facts without a stored vector are invisible to the cosine sweep
        (e.g. embed failures, facts inserted outside learn()), so the
        uncovered remainder is deduped with the Jaccard fallback — the
        quadratic cost stays bounded to that small subset.
        """
        from null_memory.memory.maintenance_actions import (
            MERGE_COSINE_THRESHOLD,
            merge_decision,
        )

        emb = self.embeddings
        if emb is None:
            return None

        eligible = {
            f["id"]: f for f in facts
            if not f.get("anchor_type")
            and len((f.get("fact") or "").split()) >= min_words
        }
        if len(eligible) < 2:
            return []

        try:
            covered = set(emb.get_embeddings_batch(list(eligible.keys())))
            candidates = emb.find_duplicate_pairs(
                list(eligible.keys()), threshold=MERGE_COSINE_THRESHOLD,
            )
        except Exception as e:
            self._note_embed_failure("gc.dedup", e)
            return None

        pairs: list[tuple[dict, dict, float]] = []
        consumed: set[str] = set()
        for id_a, id_b, score in candidates:  # sorted by similarity desc
            if id_a in consumed or id_b in consumed:
                continue
            a, b = eligible[id_a], eligible[id_b]
            if a.get("project", "global") != b.get("project", "global"):
                continue
            decision = merge_decision(a, b, score, method="cosine")
            if decision is None:
                continue
            winner, loser = decision
            pairs.append((winner, loser, score))
            consumed.add(loser["id"])

        uncovered = [
            f for fid, f in eligible.items()
            if fid not in covered and fid not in consumed
        ]
        if len(uncovered) >= 2:
            pairs.extend(find_merge_pairs(
                uncovered,
                threshold=self.config.get("dedup_jaccard_threshold", 0.65),
            ))
        return pairs

    # ── Consolidation ──

    def consolidate(self) -> dict:
        """Memory consolidation — merge similar facts, strengthen used ones, fade unused."""
        import uuid

        now = datetime.now(timezone.utc)
        # Journal run id — confidence fades are destructive (base_confidence
        # is overwritten), so each one is recorded in hypnos_journal to keep
        # the change auditable/reversible.
        run_id = str(uuid.uuid4())

        strengthen_threshold = self.config.get("consolidation_strengthen_threshold", 5)
        fade_days = self.config.get("consolidation_fade_days", 30)
        jaccard_low = self.config.get("consolidation_jaccard_low", 0.40)
        jaccard_high = self.config.get("consolidation_jaccard_high", 0.65)
        min_words = self.config.get("consolidation_min_words", 5)

        # The whole supersede/fade pass is one BEGIN IMMEDIATE transaction:
        # the snapshot it reads stays consistent with the updates it writes,
        # so a concurrent learn()/consolidate() in another process can't
        # interleave and resurrect or double-supersede facts.
        with self.db.write_transaction():
            return self._consolidate_locked(
                run_id, now, strengthen_threshold, fade_days,
                jaccard_low, jaccard_high, min_words,
            )

    def _consolidate_locked(self, run_id, now, strengthen_threshold,
                            fade_days, jaccard_low, jaccard_high,
                            min_words) -> dict:
        """Body of consolidate(); caller holds the write transaction."""
        strengthened = 0
        faded = 0
        consolidated = 0
        facts = self.db.get_active_facts()

        for entry in facts:
            access_count = entry.get("access_count", 0)
            base = entry.get("base_confidence", entry.get("confidence", 0.5))
            fact_id = entry["id"]

            # Strengthen frequently accessed
            if access_count > strengthen_threshold and base < 0.7:
                self.db.conn.execute(
                    "UPDATE facts SET base_confidence = 0.7, confidence = 0.7 WHERE id = ?",
                    (fact_id,),
                )
                strengthened += 1

        # Fade untouched old facts (shared action)
        for entry, new_base in fade_candidates(facts, now, fade_days=fade_days):
            base = entry.get("base_confidence", entry.get("confidence", 0.5))
            fact_id = entry["id"]
            self.db.conn.execute(
                "UPDATE facts SET base_confidence = ?, confidence = ? WHERE id = ?",
                (new_base, new_base, fact_id),
            )
            try:
                entry_time = datetime.fromisoformat(self._get_ts(entry))
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                age_days = (now - entry_time).days
            except (ValueError, TypeError):
                age_days = -1
            self.db.insert_hypnos_entry(
                run_id, "consolidate", "faded", fact_id=fact_id,
                detail=(f"old_conf={base:.3f}, new_conf={new_base:.3f}, "
                        f"reason=untouched {age_days}d > {fade_days}d"),
            )
            faded += 1

        # Re-fetch for merge pass
        facts = self.db.get_active_facts()

        # Group similar facts in the related-restatement band (below the
        # duplicate bar — see maintenance_actions docstring) and merge.
        eff_conf = {f["id"]: self.effective_confidence(f) for f in facts}
        for winner, losers in find_band_merge_groups(
            facts, eff_conf, jaccard_low, jaccard_high, min_words=min_words,
        ):
            for loser in losers:
                self.db.supersede_fact(loser["id"], winner["id"])
                # Merge is knowledge restructure, not decay — evented
                # (issue #20). The seq counter nests inside the caller's
                # write transaction (write_transaction supports nesting).
                self._emit_store_event("fact.update", loser["id"],
                                       {"superseded_by": winner["id"]})
                consolidated += 1

            # Update provenance
            self.db.conn.execute(
                "UPDATE facts SET provenance = 'consolidated' WHERE id = ?",
                (winner["id"],),
            )
            self._emit_store_event("fact.update", winner["id"],
                                   {"provenance": "consolidated"})

        return {
            "strengthened": strengthened,
            "faded": faded,
            "consolidated": consolidated,
        }

