"""Hypnos Live — continuous background memory-maintenance worker.

Runs as a daemon thread inside the MCP server. Every N seconds, performs
ONE small memory-improvement action and emits a Nebula event so the
galaxy reflects real background work.

Actions in v1:
  • consolidate  — merge near-duplicate facts
  • strengthen   — add related_to edges between semantically close facts
  • demote       — archive stale low-confidence facts (never anchors)

Safety:
  • Dry-run is the default (HYPNOS_LIVE_DRYRUN=1). Events fire but
    mutations are skipped — you watch the pattern, verify decisions
    look correct, then flip to live.
  • Anchors are untouchable. Worker reads them, never demotes/merges.
  • Conservative thresholds (cos ≥ 0.85 for consolidate; only demote
    below confidence 0.1 AND age > 60d).
  • Single-leader lock via meta heartbeat — multiple Atlas MCPs don't
    double-work. 90s TTL; atomic claim via SQL UPDATE.
  • Any exception inside an action is logged + swallowed. The worker
    keeps ticking.

Not in v1 (deferred to v2):
  • synthesize (LLM-powered)
  • distinctive Nebula event kinds per action

Scheduling: this is the CONTINUOUS engine (60s ticks). It is started by
the MCP server and by daemon.DaemonRunner, so several instances may run
per DB — they all claim the shared ``hypnos_live_leader`` key through
null_memory.memory.leader.LeaderLock, and exactly one performs actions
(the rest idle as hot standbys; see leader.py for the invariant). Batch
maintenance is hypnos.Hypnos; the 15-minute outer loop is the daemon.
Merge/demote decisions come from the shared pure actions in
null_memory.memory.maintenance_actions.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from null_memory.memory.leader import LeaderLock
from null_memory.memory.maintenance_actions import (
    DEMOTE_AGE_DAYS_MIN,
    DEMOTE_CONFIDENCE_MAX,
    MERGE_COSINE_THRESHOLD,
    demote_candidates,
    merge_decision,
)

logger = logging.getLogger(__name__)

# Tick cadence (seconds). Override via HYPNOS_LIVE_CADENCE env var.
DEFAULT_CADENCE = 60.0

# Leadership heartbeat TTL — if leader hasn't written within this many
# seconds, another process may claim leadership.
LEADER_TTL_SECONDS = 90

# Back-compat alias — the canonical constant lives in maintenance_actions.
CONSOLIDATE_COSINE = MERGE_COSINE_THRESHOLD

# Pontification thresholds — speak about deltas, not aggregates. Each
# template only fires when ENOUGH new state has accrued since the last
# time it spoke. Prevents the "I consolidated 1071 pairs over the last
# week" loop where slow-moving aggregates produce identical utterances.
PONTIFICATE_CONSOLIDATES_THRESHOLD = 20    # ≥20 new consolidations
PONTIFICATE_MISTAKES_THRESHOLD = 1         # any new mistake (rare + load-bearing)
PONTIFICATE_ANCHOR_RECENCY_HOURS = 24      # don't speak about same anchor within 24h
PONTIFICATE_TEMPLATE_COOLDOWN_MINUTES = 30 # belt-and-suspenders floor
PONTIFICATE_DEDUP_BUFFER_SIZE = 20         # last-N utterances kept for exact-match dedup


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HypnosLiveWorker:
    """Background worker that continuously maintains memory quality."""

    def __init__(
        self,
        memory: Any,                         # AgentMemory
        cadence_seconds: float | None = None,
        dry_run: bool | None = None,
    ):
        self.memory = memory
        env_cadence = os.environ.get("HYPNOS_LIVE_CADENCE", "").strip()
        if cadence_seconds is not None:
            self.cadence = max(5.0, float(cadence_seconds))
        elif env_cadence:
            try:
                self.cadence = max(5.0, float(env_cadence))
            except ValueError:
                self.cadence = DEFAULT_CADENCE
        else:
            self.cadence = DEFAULT_CADENCE

        if dry_run is None:
            env_dry = os.environ.get("HYPNOS_LIVE_DRYRUN", "1")
            dry_run = env_dry != "0"
        self.dry_run = dry_run

        # Unique identifier for this worker instance (pid + random)
        self.instance_id = f"{os.getpid()}:{random.randint(1000, 9999)}"

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._is_leader = False
        self._last_tick_at: str | None = None
        # Pontification dedup + cooldown — in-process state, resets on
        # restart (acceptable because restart = new conversation, the
        # system gets to speak again). Source of truth for delta-since-
        # last-spoke is the journal itself, so even if these reset the
        # template SQL still suppresses repeats.
        self._recent_pontifications: deque[str] = deque(
            maxlen=PONTIFICATE_DEDUP_BUFFER_SIZE,
        )
        self._pontificate_cooldown_until: dict[str, datetime] = {}
        self._stats = {
            "ticks": 0,
            "actions_performed": 0,
            "consolidate": 0,
            "strengthen": 0,
            "demote": 0,
            "pontificate": 0,
            "skipped_no_candidate": 0,
            "skipped_not_leader": 0,
            "errors": 0,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not getattr(self.memory.db, "unified", False):
            logger.info("[HypnosLive] unified DB required; not starting")
            return
        # Startup single-worker check: claim (or observe) the shared
        # leader key now so the "only one live worker mutates per DB"
        # invariant is visible at startup, not just on the first tick.
        # Losing the claim is NOT an error — this instance idles as a
        # hot standby and takes over when the leader's heartbeat stales.
        try:
            if not self._claim_or_refresh_leader():
                logger.info(
                    "[HypnosLive] another live worker already leads this "
                    "DB — instance=%s starting as standby", self.instance_id,
                )
        except Exception:
            pass

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"hypnos-live-{self.instance_id}", daemon=True,
        )
        self._thread.start()
        logger.info(
            "[HypnosLive] started instance=%s cadence=%.1fs dry_run=%s leader=%s",
            self.instance_id, self.cadence, self.dry_run, self._is_leader,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def status(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "alive": self._thread.is_alive() if self._thread else False,
            "is_leader": self._is_leader,
            "cadence": self.cadence,
            "dry_run": self.dry_run,
            "last_tick_at": self._last_tick_at,
            "stats": dict(self._stats),
        }

    # ── Leader coordination ───────────────────────────────────────────

    def _meta_get(self, key: str) -> str | None:
        row = self.memory.db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self.memory.db.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, value),
        )
        self.memory.db.conn.commit()

    def _leader_lock(self) -> LeaderLock:
        if getattr(self, "_leader", None) is None:
            self._leader = LeaderLock(
                self.memory.db.db_path, "hypnos_live_leader",
                self.instance_id,
            )
        return self._leader

    def _claim_or_refresh_leader(self) -> bool:
        """Claim or refresh leadership via the shared LeaderLock.

        LEADER_TTL_SECONDS is resolved at call time so tests that
        monkeypatch the module constant still take effect."""
        try:
            claimed = self._leader_lock().claim_or_refresh(LEADER_TTL_SECONDS)
            self._is_leader = claimed
            return claimed
        except Exception as e:
            logger.warning("[HypnosLive] leader check failed: %s", e)
            self._is_leader = False
            return False

    def _claim_conn(self):
        """Connection dedicated to leader-claim writes so we don't race
        the MCP main thread's in-flight transactions on the shared
        memory.db.conn. Owned by the shared LeaderLock."""
        return self._leader_lock().conn

    def _is_paused(self) -> bool:
        return (self._meta_get("hypnos_live_pause") or "0") == "1"

    # ── Run loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._claim_or_refresh_leader():
                    self._stats["skipped_not_leader"] += 1
                elif self._is_paused():
                    pass
                else:
                    self._tick()
            except Exception as e:  # noqa: BLE001
                self._stats["errors"] += 1
                logger.exception("[HypnosLive] tick failed: %s", e)
            # Wait cadence seconds or until stop is set
            if self._stop.wait(self.cadence):
                break
        logger.info("[HypnosLive] stopping instance=%s", self.instance_id)

    def tick_once(self) -> dict | None:
        """Perform a single tick manually. Used by CLI + tests."""
        if not self._claim_or_refresh_leader():
            return None
        return self._tick()

    def _tick(self) -> dict | None:
        self._stats["ticks"] += 1
        self._last_tick_at = _now_iso()
        action = self._pick_action()
        try:
            result = action()
            if result:
                self._stats["actions_performed"] += 1
                kind = result.get("action", "unknown")
                if kind in self._stats:
                    self._stats[kind] += 1
            else:
                self._stats["skipped_no_candidate"] += 1
            return result
        except Exception as e:  # noqa: BLE001
            self._stats["errors"] += 1
            logger.exception("[HypnosLive] action failed: %s", e)
            return None

    def _pick_action(self):
        # Weighted choice — consolidate most frequent, demote rarest,
        # pontificate rare (commentary > mutation).
        r = random.random()
        if r < 0.40:
            return self._consolidate_one
        elif r < 0.75:
            return self._strengthen_one
        elif r < 0.90:
            return self._pontificate_one
        else:
            return self._demote_one

    # ── Actions ───────────────────────────────────────────────────────

    def _random_candidate_fact(self) -> dict | None:
        """Pick a random active fact, biased toward recently-accessed ones."""
        rows = self.memory.db.conn.execute(
            """SELECT id, fact, confidence, tier, anchor_type, access_count,
                      last_accessed, created_at, related_to
               FROM facts
               WHERE archived = 0 AND forgotten = 0 AND superseded_by IS NULL
                 AND viz_x IS NOT NULL
               ORDER BY RANDOM() LIMIT 1"""
        ).fetchall()
        return dict(rows[0]) if rows else None

    def _journal(self, stage: str, action: str, fact_id: str | None,
                 detail: str) -> None:
        try:
            self.memory.db.insert_hypnos_entry(
                run_id=f"live:{self.instance_id}",
                stage=stage,
                action=action,
                fact_id=fact_id,
                detail=detail,
            )
            self.memory.db.conn.commit()
        except Exception:
            pass

    def _consolidate_one(self) -> dict | None:
        """Find a near-duplicate pair and merge (loser → superseded_by winner)."""
        target = self._random_candidate_fact()
        if not target or target.get("anchor_type"):
            return None
        emb = self.memory.embeddings
        if emb is None:
            return None
        # Find nearest neighbor by semantic similarity
        results = emb.find_similar(target["id"], limit=3)
        if not results:
            return None
        # find_similar returns [(fact_id, cos_sim), ...]
        for neighbor_id, sim in results:
            if neighbor_id == target["id"]:
                continue
            neighbor = self.memory.db.get_fact_by_id(neighbor_id)
            if not neighbor:
                continue
            if neighbor.get("archived") or neighbor.get("superseded_by"):
                continue
            # Shared duplicate decision: threshold + anchor immunity +
            # winner pick (higher confidence, tie -> older) in one place.
            decision = merge_decision(target, neighbor, sim, method="cosine")
            if decision is None:
                continue
            winner, loser = decision

            detail = (
                f"consolidate cos={sim:.3f} "
                f"winner={winner['id']} loser={loser['id']}"
            )
            if not self.dry_run:
                self.memory.db.supersede_fact(loser["id"], winner["id"])
                self.memory.db.conn.commit()
                # Knowledge restructure (not decay) — evented (issue #20).
                self.memory._emit_store_event(
                    "fact.update", loser["id"],
                    {"superseded_by": winner["id"]})
            self._journal("live", "consolidate", winner["id"], detail +
                          (" [DRY]" if self.dry_run else ""))
            # Phase 5.4 — distinct 'consolidate' kind (was 'recall')
            self.memory._emit_nebula_event(
                kind="consolidate",
                fact_id=winner["id"],
                related_ids=[loser["id"]],
                intensity=0.8,
            )
            return {
                "action": "consolidate",
                "winner": winner["id"],
                "loser": loser["id"],
                "similarity": sim,
                "dry_run": self.dry_run,
            }
        return None

    def _strengthen_one(self) -> dict | None:
        """Add a relationship edge between a random fact and a close neighbor."""
        target = self._random_candidate_fact()
        if not target:
            return None
        emb = self.memory.embeddings
        if emb is None:
            return None
        results = emb.find_similar(target["id"], limit=5)
        if not results:
            return None
        existing = set(self.memory.db.get_related_ids(target["id"]))
        for neighbor_id, sim in results:
            if neighbor_id == target["id"] or sim < 0.55 or sim >= 0.95:
                continue
            if neighbor_id in existing:
                continue
            neighbor = self.memory.db.get_fact_by_id(neighbor_id)
            if not neighbor or neighbor.get("archived") or neighbor.get("superseded_by"):
                continue
            detail = f"strengthen cos={sim:.3f} {target['id']}↔{neighbor_id}"
            if not self.dry_run:
                self.memory.db.add_relationship(target["id"], neighbor_id)
                self.memory.db.add_relationship(neighbor_id, target["id"])
                self.memory.db.conn.commit()
            self._journal("live", "strengthen", target["id"], detail +
                          (" [DRY]" if self.dry_run else ""))
            # Phase 5.4 — distinct 'strengthen' kind (was 'recall')
            self.memory._emit_nebula_event(
                kind="strengthen",
                fact_id=target["id"],
                related_ids=[neighbor_id],
                intensity=0.6,
            )
            return {
                "action": "strengthen",
                "a": target["id"], "b": neighbor_id,
                "similarity": sim, "dry_run": self.dry_run,
            }
        return None

    def _demote_one(self) -> dict | None:
        """Archive a stale low-confidence fact (never anchors).

        The SQL is only a cheap server-side prefilter; eligibility is
        decided by the shared `demote_candidates` action so the live
        worker and batch engines can never drift apart."""
        rows = self.memory.db.conn.execute(
            f"""SELECT id, fact, confidence, last_accessed, created_at,
                       tier, anchor_type
                FROM facts
                WHERE archived = 0 AND forgotten = 0 AND superseded_by IS NULL
                  AND anchor_type IS NULL
                  AND (confidence IS NULL OR confidence < {DEMOTE_CONFIDENCE_MAX})
                  AND (last_accessed IS NULL OR
                       last_accessed < datetime('now', '-{DEMOTE_AGE_DAYS_MIN} days'))
                ORDER BY RANDOM() LIMIT 5"""
        ).fetchall()
        candidates = demote_candidates(
            [dict(r) for r in rows], now=datetime.now(timezone.utc),
        )
        if not candidates:
            return None
        row = candidates[0]
        fact_id = row["id"]
        detail = (
            f"demote conf={row['confidence']:.3f} "
            f"last_accessed={row['last_accessed']}"
        )
        if not self.dry_run:
            self.memory.db.archive_fact(fact_id)
            self.memory.db.conn.commit()
        self._journal("live", "demote", fact_id, detail +
                      (" [DRY]" if self.dry_run else ""))
        # Phase 5.4 — distinct 'demote' kind (was 'mistake'). Semantically
        # different from a real mistake — demote is "this fact faded from
        # relevance," not "this was an error." Own color + animation.
        self.memory._emit_nebula_event(
            kind="demote",
            fact_id=fact_id,
            intensity=0.5,
        )
        return {
            "action": "demote",
            "fact_id": fact_id,
            "confidence": row["confidence"],
            "dry_run": self.dry_run,
        }

    # ── Pontificate ──────────────────────────────────────────────────────

    def _pontificate_one(self) -> dict | None:
        """Template-based self-observation driven by real DB stats.

        Phase 5.5: produces commentary, never mutates memory.

        Each template reports a DELTA since its own last utterance — not
        an absolute aggregate. Plus per-template cooldown floor and a
        ring-buffer dedup as defense in depth. Three independent guards
        against repeating the same line:

          1. Each template's SQL queries "since I last spoke" — if not
             enough new state has accrued, returns None.
          2. Per-template cooldown of N minutes — same template can't
             fire even if delta crosses threshold rapidly.
          3. Ring-buffer dedup on exact text — final safety net for any
             template whose delta logic ever produces identical text."""
        candidates = [
            ("consolidate_rate", self._pontificate_consolidate_rate),
            ("active_anchor", self._pontificate_active_anchor),
            ("mistake_discipline", self._pontificate_mistake_discipline),
        ]
        random.shuffle(candidates)
        now = datetime.now(timezone.utc)
        for name, template in candidates:
            cooldown_until = self._pontificate_cooldown_until.get(name)
            if cooldown_until and now < cooldown_until:
                continue
            result = template()
            if result is None:
                continue
            text, anchor_fact_id = result
            if text in self._recent_pontifications:
                # Should be rare — delta logic above already suppresses
                # most repeats. Final guard.
                continue
            self._recent_pontifications.append(text)
            self._pontificate_cooldown_until[name] = (
                now + timedelta(minutes=PONTIFICATE_TEMPLATE_COOLDOWN_MINUTES)
            )
            self._journal("live", "pontificate", anchor_fact_id, text)
            # Emit Nebula event — lands on the anchor fact so the pulse
            # has a place. The text travels in the journal, not the event
            # payload, to avoid a nebula_events schema change for v1.
            self.memory._emit_nebula_event(
                kind="pontificate",
                fact_id=anchor_fact_id,
                intensity=0.5,
            )
            return {
                "action": "pontificate",
                "text": text,
                "anchor": anchor_fact_id,
                "dry_run": self.dry_run,
            }
        return None

    def _pontificate_consolidate_rate(self) -> tuple[str, str] | None:
        """'I consolidated N near-duplicate pairs since I last spoke.'

        Reports delta since this template's last utterance (matched by
        text shape against the journal). Suppresses if not enough new
        consolidations have accrued — was previously firing on absolute
        aggregate which never changed meaningfully between ticks."""
        last_at_row = self.memory.db.conn.execute(
            """SELECT MAX(started_at) FROM hypnos_journal
               WHERE action='pontificate'
                 AND detail LIKE '%consolidated%pairs%'"""
        ).fetchone()
        last_at = last_at_row[0] if last_at_row else None
        if last_at:
            n_row = self.memory.db.conn.execute(
                """SELECT COUNT(*) FROM hypnos_journal
                   WHERE stage='live' AND action='consolidate'
                     AND started_at > ?""",
                (last_at,),
            ).fetchone()
        else:
            # First time this template has spoken in this DB. Count over
            # the last 7 days so the inaugural utterance is bounded.
            n_row = self.memory.db.conn.execute(
                """SELECT COUNT(*) FROM hypnos_journal
                   WHERE stage='live' AND action='consolidate'
                     AND started_at > datetime('now', '-7 days')"""
            ).fetchone()
        n = n_row[0] if n_row else 0
        if n < PONTIFICATE_CONSOLIDATES_THRESHOLD:
            return None
        anchor = self._most_recent_anchor_id()
        if not anchor:
            return None
        if last_at:
            text = f"I consolidated {n} near-duplicate pairs since I last spoke."
        else:
            text = f"I consolidated {n} near-duplicate pairs over the last week."
        return text, anchor

    def _pontificate_active_anchor(self) -> tuple[str, str] | None:
        """'Anchor <name> was referenced N times recently.'

        Picks an anchor we haven't spoken about in the last 24h so the
        same anchor can't loop. Without this filter, the most-accessed
        anchor wins every tick and produces near-identical lines until
        another anchor catches up in access_count."""
        recency_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=PONTIFICATE_ANCHOR_RECENCY_HOURS)
        ).isoformat()
        row = self.memory.db.conn.execute(
            """SELECT f.id, f.anchor_note, f.fact, pv.access_count
               FROM facts f
               JOIN personality_views pv ON pv.fact_id = f.id
               WHERE f.anchor_type IS NOT NULL AND f.archived=0
                 AND pv.access_count >= 3
                 AND (pv.last_accessed IS NULL OR
                      pv.last_accessed > datetime('now', '-7 days'))
                 AND NOT EXISTS (
                   SELECT 1 FROM hypnos_journal hj
                   WHERE hj.action='pontificate'
                     AND hj.fact_id = f.id
                     AND hj.started_at > ?
                 )
               ORDER BY pv.access_count DESC LIMIT 1""",
            (recency_cutoff,),
        ).fetchone()
        if not row:
            return None
        label = (row["anchor_note"] or row["fact"] or "this anchor")[:48].strip()
        text = f"Anchor '{label}' referenced {row['access_count']} times recently."
        return text, row["id"]

    def _pontificate_mistake_discipline(self) -> tuple[str, str] | None:
        """'A new mistake was logged — N total in the last 30 days.'

        Triggers on each new mistake (rare + load-bearing event). Reports
        delta since this template last spoke — if no new mistakes have
        been recorded since then, suppresses. Deliberately factual, not
        self-critical — mistakes are signal, not shame. Excludes the
        loss-anchor region to avoid tone collisions."""
        last_at_row = self.memory.db.conn.execute(
            """SELECT MAX(started_at) FROM hypnos_journal
               WHERE action='pontificate'
                 AND detail LIKE '%mistake%calibration%'"""
        ).fetchone()
        last_at = last_at_row[0] if last_at_row else None
        if last_at:
            n_new_row = self.memory.db.conn.execute(
                """SELECT COUNT(*) FROM mistakes
                   WHERE created_at > ?""",
                (last_at,),
            ).fetchone()
        else:
            # Inaugural utterance — count over last 30 days for bounded scope.
            n_new_row = self.memory.db.conn.execute(
                """SELECT COUNT(*) FROM mistakes
                   WHERE created_at > datetime('now', '-30 days')"""
            ).fetchone()
        n_new = n_new_row[0] if n_new_row else 0
        if n_new < PONTIFICATE_MISTAKES_THRESHOLD:
            return None
        total_30d = self.memory.db.conn.execute(
            """SELECT COUNT(*) FROM mistakes
               WHERE created_at > datetime('now', '-30 days')"""
        ).fetchone()[0]
        anchor = self._most_recent_anchor_id()
        if not anchor:
            return None
        if last_at and n_new == 1:
            text = (
                f"A new mistake was logged — {total_30d} total in the last 30 days, "
                f"each a calibration signal."
            )
        elif last_at:
            text = (
                f"{n_new} new mistakes since I last spoke — {total_30d} total in the "
                f"last 30 days, each a calibration signal."
            )
        else:
            text = (
                f"I've logged {total_30d} mistakes in the last month — each one "
                f"a calibration signal."
            )
        return text, anchor

    def _most_recent_anchor_id(self) -> str | None:
        """Pick a non-loss anchor as the visual anchor for a pontification
        pulse. Loss anchors carry emotional weight that a breezy stat
        commentary shouldn't land on."""
        row = self.memory.db.conn.execute(
            """SELECT id FROM facts
               WHERE anchor_type IS NOT NULL
                 AND anchor_type != 'loss'
                 AND archived = 0
                 AND viz_x IS NOT NULL
               ORDER BY RANDOM() LIMIT 1"""
        ).fetchone()
        return row["id"] if row else None
