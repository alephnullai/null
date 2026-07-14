"""Probes — calibration + continuity probe generation, execution, scoring.

Extracted from agent.py (P2 god-object split). Contains ProbesMixin:
  * continuity probes (Phase 2c) — generated from emotional anchors via
    deployment-configurable templates (identity.json ``probe_templates`` /
    ``chain_probes``), with a generic anchor-type-derived package default
  * calibration probes — entity extraction (`auto_generate_probes`),
    user probes (`add_probe`), probe execution (`run_probes`,
    `_execute_probe`, `_run_system_probes`), regression checks
    (`validate_after_learn`, `check_fact_reliability`)

Mixed into AgentMemory; methods rely on the host's db / recall / learn /
identity / personality attributes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import ClassVar

from null_memory.memory.recall import _STOP_WORDS


class ProbesMixin:
    """Probe generation + scoring methods for AgentMemory."""

    # ── Continuity Probes (Phase 2c — third proof of identity) ──

    # Generic per-anchor-type questions used when the deployment doesn't
    # configure ``probe_templates`` in identity.json. Derived from anchor
    # TYPE only; ``{name}`` is filled with the agent's name and the
    # expected answer is drawn from the anchor's own text keywords — no
    # person names, no product names ship in the package.
    _GENERIC_PROBE_QUESTIONS: ClassVar[dict[str, str]] = {
        "origin": "Why does {name} exist?",
        "loss": "What loss shaped {name}'s memory?",
        "commitment": "What commitment is {name} built around?",
        "turning_point": "What turning point changed how {name} works?",
        "joy": "What memory brings {name} joy?",
    }
    _GENERIC_PROBE_FALLBACK_QUESTION: ClassVar[str] = (
        "What does {name} remember about this {anchor_type} moment?"
    )

    def _probe_template_config(self) -> tuple[dict | None, list[dict]]:
        """Deployment-configured probe templates from identity.json.

        Returns (direct_templates, chain_probes) where direct_templates is
        ``{anchor_type: [{"question", "expected"}, ...]}`` or None when the
        deployment hasn't configured any (→ generic fallback), and
        chain_probes is ``[{"question", "expected"}, ...]`` (empty list
        when unconfigured — chain probes encode deployment-specific
        multi-anchor knowledge, so the package ships none).
        """
        direct = self.identity.get("probe_templates")
        if not isinstance(direct, dict) or not direct:
            direct = None
        chain = self.identity.get("chain_probes")
        if not isinstance(chain, list):
            chain = []
        return direct, chain

    @staticmethod
    def _anchor_keywords(fact_text: str, limit: int = 2) -> str:
        """Significant keywords from an anchor's own text — used as the
        expected answer for generic probes so a fresh install's probes
        contain only the deployment's own words."""
        words = []
        for raw in (fact_text or "").split():
            w = raw.strip(".,;:!?\"'()[]{}—–-/\\").lower()
            if len(w) > 3 and w not in _STOP_WORDS and w not in words:
                words.append(w)
            if len(words) >= limit:
                break
        return " ".join(words)

    def generate_continuity_probes(self, *, clear_existing: bool = False) -> dict:
        """Populate the probes table with continuity-type probes derived
        from current emotional anchors.

        Direct probes: when identity.json carries ``probe_templates``
        (``{anchor_type: [{"question", "expected"}...]}``), templates are
        matched to anchors by type + content keyword (a template applies
        only when the anchor text contains the first word of its expected
        answer). Otherwise a generic per-type question is generated with
        the expected answer drawn from the anchor's own text.

        Chain probes: from identity.json ``chain_probes``
        (``[{"question", "expected"}...]``); none by default.

        Idempotent: existing probes with matching (question, expected) are
        not re-inserted unless ``clear_existing=True``.
        """
        if not getattr(self.db, "unified", False):
            raise RuntimeError(
                "Continuity probes require the unified DB (schema v13)."
            )
        if clear_existing:
            self.db.conn.execute(
                "DELETE FROM probes WHERE probe_type = 'continuity'"
            )
        existing = {
            (row[0], row[1])
            for row in self.db.conn.execute(
                "SELECT question, expected FROM probes WHERE probe_type = 'continuity'"
            ).fetchall()
        }

        stats = {"direct": 0, "chain": 0, "skipped_existing": 0}
        inserted: list[tuple[int, dict]] = []
        anchors = self.db.get_anchors()
        now = datetime.now(timezone.utc).isoformat()
        direct_templates, chain_seeds = self._probe_template_config()

        # Direct probes — one or two per anchor.
        for anchor in anchors:
            atype = anchor.get("anchor_type")
            fact_lower = (anchor.get("fact") or "").lower()

            if direct_templates is not None:
                # Configured path: matched by type + content keyword.
                candidates = []
                for tpl in direct_templates.get(atype, []):
                    # Only use this template if the anchor fact contains
                    # its expected token — filters each probe to the
                    # anchor it was written for.
                    if tpl["expected"].lower().split()[0] not in fact_lower:
                        continue
                    candidates.append(
                        {"question": tpl["question"],
                         "expected": tpl["expected"]}
                    )
            else:
                # Generic path: anchor-type question, anchor-text answer.
                expected = self._anchor_keywords(anchor.get("fact") or "")
                if not expected:
                    continue
                question_tpl = self._GENERIC_PROBE_QUESTIONS.get(
                    atype, self._GENERIC_PROBE_FALLBACK_QUESTION,
                )
                candidates = [{
                    "question": question_tpl.format(
                        name=self.name, anchor_type=atype,
                    ),
                    "expected": expected,
                }]

            for tpl in candidates:
                key = (tpl["question"], tpl["expected"])
                if key in existing:
                    stats["skipped_existing"] += 1
                    continue
                cursor = self.db.conn.execute(
                    """INSERT INTO probes (question, expected, fact_id,
                       probe_type, personality, created_at)
                       VALUES (?, ?, ?, 'continuity', ?, ?)""",
                    (tpl["question"], tpl["expected"], anchor["id"],
                     self.personality, now),
                )
                inserted.append((cursor.lastrowid, {
                    "question": tpl["question"],
                    "expected": tpl["expected"],
                    "fact_id": anchor["id"],
                    "probe_type": "continuity",
                    "created_at": now,
                }))
                stats["direct"] += 1
                existing.add(key)

        # Chain probes — multi-anchor questions, not bound to a single
        # fact. Deployment-configured only (identity.json "chain_probes").
        for seed in chain_seeds:
            key = (seed["question"], seed["expected"])
            if key in existing:
                stats["skipped_existing"] += 1
                continue
            cursor = self.db.conn.execute(
                """INSERT INTO probes (question, expected, fact_id,
                   probe_type, personality, created_at)
                   VALUES (?, ?, NULL, 'continuity', ?, ?)""",
                (seed["question"], seed["expected"], self.personality, now),
            )
            inserted.append((cursor.lastrowid, {
                "question": seed["question"],
                "expected": seed["expected"],
                "fact_id": None,
                "probe_type": "continuity",
                "created_at": now,
            }))
            stats["chain"] += 1
            existing.add(key)

        self.db.conn.commit()
        for probe_id, data in inserted:
            self._emit_store_event("probe.add", probe_id, data)
        return stats

    def run_continuity_probes(self, limit: int = 50) -> dict:
        """Run every continuity probe and return aggregate + per-probe results.

        Each probe passes if the expected token(s) appear (case-insensitive
        substring match) in the top-5 recall results for the question.
        Updates probes.run_count, pass_count, last_run, last_result.
        """
        if not getattr(self.db, "unified", False):
            raise RuntimeError("Unified DB required.")
        rows = self.db.conn.execute(
            """SELECT id, question, expected, fact_id FROM probes
               WHERE probe_type = 'continuity' LIMIT ?""",
            (limit,),
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        passed = 0
        details: list[dict] = []
        probe_results: list[tuple[int, bool, str]] = []
        for pid, question, expected, fact_id in rows:
            # Widen to top-10 — continuity probes test whether the memory is
            # retrievable AT ALL under a natural question, not rank-#1 precision.
            results = self.recall(question, limit=10, include_mistakes=False, _emit_event=False)
            # Token-subset match: every significant word in expected must
            # appear somewhere in the fact text (order-independent). Forgives
            # small grammatical drift ("quit the day job" ↔ "quit his day job").
            expected_tokens = [
                t for t in expected.lower().split()
                if len(t) > 2 and t not in _STOP_WORDS
            ]
            hit = False
            rank: int | None = None
            for i, entry in enumerate(results, start=1):
                text = (entry.get("fact") or entry.get("mistake") or "").lower()
                if expected.lower() in text or (
                    expected_tokens
                    and all(tok in text for tok in expected_tokens)
                ):
                    hit = True
                    rank = i
                    break
            result_str = f"pass@rank={rank}" if hit else "fail"
            self.db.conn.execute(
                """UPDATE probes SET last_run = ?, last_result = ?,
                   run_count = COALESCE(run_count, 0) + 1,
                   pass_count = COALESCE(pass_count, 0) + ?
                   WHERE id = ?""",
                (now, result_str, 1 if hit else 0, pid),
            )
            probe_results.append((pid, hit, result_str))
            passed += 1 if hit else 0
            details.append({
                "id": pid,
                "question": question,
                "expected": expected,
                "passed": hit,
                "rank": rank,
            })
        self.db.conn.commit()
        for pid, hit, result_str in probe_results:
            self._emit_store_event("probe.result", pid,
                                   {"passed": hit, "result": result_str})
        total = len(rows)
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "score": passed / total if total else 1.0,
            "details": details,
        }

    # ── Calibration Probes ──

    # Patterns for auto-generating probes from facts containing specific details.
    # Tuned for precision over recall — only generate probes for details specific
    # enough to be worth verifying. Generic numbers like "1" or "$1" are excluded.
    _ENTITY_PATTERNS = [
        # Dates: "April 19, 2018", "2026-03-19", "March 2026"
        (r'\b(\w+ \d{1,2},?\s*\d{4})\b', "date"),
        (r'\b(\d{4}-\d{2}-\d{2})\b', "date"),
        (r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b', "date"),
        # Jersey/unit numbers: "#4" but only 2+ digits or contextually significant
        (r'#(\d{2,})\b', "number"),
        # Quantities with specific units (must be 3+ digits to be interesting)
        (r'\b(\d{3,}(?:\.\d+)?)\s+(?:tests|positions|facts|mistakes|tools|languages|sessions)', "quantity"),
        # Dollar amounts >= $5 (small amounts are too generic)
        (r'\$(\d+(?:\.\d+)?)', "dollar_amount"),
        # Versions: v2.1.0 etc
        (r'\bv(\d+\.\d+\.\d+)\b', "version"),
        # Proper nouns following "is", "named", "called" (2+ chars)
        (r'\b(?:is|named|called)\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)*)', "name"),
    ]

    def add_probe(self, question: str, expected: str,
                  fact_id: str | None = None,
                  probe_type: str = "user") -> dict:
        """Add a calibration probe."""
        probe = self.db.insert_probe(question, expected, fact_id, probe_type)
        self._emit_store_event("probe.add", probe["id"], {
            "question": question,
            "expected": expected,
            "fact_id": fact_id,
            "probe_type": probe_type,
            "created_at": probe["created_at"],
        })
        return probe

    def auto_generate_probes(self, fact: str, fact_id: str) -> list[dict]:
        """Extract entities from a fact and generate calibration probes.

        Returns list of created probes. Only generates if the fact contains
        specific verifiable details (dates, numbers, proper nouns).
        Tuned for precision: skips generic/low-value entities.
        """
        created = []
        # Extract a short context snippet (first ~40 chars of the fact)
        context_words = fact.split()[:6]
        context = " ".join(context_words)

        for pattern, entity_type in self._ENTITY_PATTERNS:
            matches = re.findall(pattern, fact)
            if not matches:
                continue
            for match in matches[:2]:  # Cap at 2 probes per pattern per fact
                if isinstance(match, tuple):
                    match = match[0]

                # Filter out low-value matches
                if entity_type == "dollar_amount":
                    try:
                        if float(match) < 5:
                            continue  # Skip small dollar amounts
                    except ValueError:
                        continue
                elif entity_type == "number":
                    try:
                        if int(match) < 10:
                            continue  # Single digit numbers are too generic
                    except ValueError:
                        continue

                # Build a question with context from the fact
                if entity_type == "date":
                    question = f"What date: {match} (re: {context})?"
                    expected = match
                elif entity_type == "number":
                    question = f"What is #{match} (re: {context})?"
                    expected = f"#{match}"
                elif entity_type == "quantity":
                    question = f"How many: {match} (re: {context})?"
                    expected = match
                elif entity_type == "dollar_amount":
                    question = f"What is ${match} for (re: {context})?"
                    expected = f"${match}"
                elif entity_type == "version":
                    question = f"What is at v{match} (re: {context})?"
                    expected = f"v{match}"
                elif entity_type == "name":
                    question = f"Who/what is {match} (re: {context})?"
                    expected = match
                else:
                    continue

                # Check we don't already have a probe for this fact+expected
                existing = self.db.get_probes()
                duplicate = any(
                    p["fact_id"] == fact_id and p["expected"] == expected
                    for p in existing
                )
                if not duplicate:
                    probe = self.db.insert_probe(
                        question, expected, fact_id, probe_type="auto"
                    )
                    self._emit_store_event("probe.add", probe["id"], {
                        "question": question,
                        "expected": expected,
                        "fact_id": fact_id,
                        "probe_type": "auto",
                        "created_at": probe["created_at"],
                    })
                    created.append(probe)
        return created

    def validate_after_learn(self, learned_fact: str) -> list[dict]:
        """After learning a new fact, check if any existing probes now fail.

        Finds probes whose questions or expected values share keywords with
        the learned fact, re-runs them, and returns any that now fail.
        """
        all_probes = self.db.get_probes()
        if not all_probes:
            return []

        # Extract significant words from the learned fact
        words = set(
            w.lower() for w in re.split(r'\W+', learned_fact)
            if len(w) > 3
        )
        if not words:
            return []

        # Find probes related to this fact by keyword overlap
        related = []
        for probe in all_probes:
            probe_words = set(
                w.lower() for w in re.split(r'\W+',
                    probe["question"] + " " + probe["expected"])
                if len(w) > 3
            )
            overlap = words & probe_words
            if len(overlap) >= 1:
                related.append(probe)

        if not related:
            return []

        # Re-run related probes and collect failures
        broken = []
        for probe in related[:10]:  # Cap to avoid excessive recall calls
            detail = self._execute_probe(probe)
            if not detail["passed"]:
                broken.append(probe)
        return broken

    def check_fact_reliability(self, fact_ids: list[str]) -> dict[str, str]:
        """Check probe health for a list of fact IDs.

        Returns a dict of fact_id -> warning message for facts with failing probes.
        Facts with no probes or all-passing probes are omitted.
        """
        if not fact_ids:
            return {}
        probes_by_fact = self.db.get_probes_for_facts(fact_ids)
        warnings: dict[str, str] = {}
        for fid, probes in probes_by_fact.items():
            for p in probes:
                run_count = p.get("run_count", 0)
                if run_count == 0:
                    continue
                pass_count = p.get("pass_count", 0)
                pass_rate = pass_count / run_count
                if pass_rate < 0.5:
                    warnings[fid] = (
                        f"CAUTION: probe \"{p['question'][:50]}\" "
                        f"pass rate {pass_rate:.0%} ({pass_count}/{run_count})"
                    )
                elif p.get("last_result") == "fail":
                    warnings[fid] = (
                        f"CAUTION: probe \"{p['question'][:50]}\" "
                        f"failed on last run"
                    )
        return warnings

    def run_probes(self, probe_type: str | None = None,
                   include_system: bool = True) -> dict:
        """Run calibration probes and return results.

        Returns: {
            "total": int, "passed": int, "failed": int,
            "score": float (0-1), "details": list[dict]
        }
        """
        results = {"total": 0, "passed": 0, "failed": 0, "details": []}

        # Layer 1: System probes (always run if include_system)
        if include_system and probe_type in (None, "system"):
            system_results = self._run_system_probes()
            results["details"].extend(system_results)

        # Layer 2+3: Stored probes (auto + user)
        probes = self.db.get_probes(probe_type=probe_type)
        for probe in probes:
            detail = self._execute_probe(probe)
            results["details"].append(detail)

        results["total"] = len(results["details"])
        results["passed"] = sum(1 for d in results["details"] if d["passed"])
        results["failed"] = results["total"] - results["passed"]
        results["score"] = (
            results["passed"] / results["total"]
            if results["total"] > 0 else 1.0
        )
        return results

    def _execute_probe(self, probe: dict) -> dict:
        """Run a single probe against recall."""
        question = probe["question"]
        expected = probe["expected"]
        fact_id = probe.get("fact_id")

        recall_results = self.recall(question, limit=5, _emit_event=False)
        passed = False
        matched_rank = None

        for i, entry in enumerate(recall_results):
            entry_text = entry.get("fact", entry.get("mistake", ""))
            # Check if expected string appears in the recalled fact
            if expected.lower() in entry_text.lower():
                passed = True
                matched_rank = i + 1
                break
            # Also check if the specific fact_id was returned
            if fact_id and entry.get("id") == fact_id:
                if expected.lower() in entry_text.lower():
                    passed = True
                    matched_rank = i + 1
                    break

        # Update the probe record
        if probe.get("id"):
            self.db.update_probe_result(probe["id"], passed)
            self._emit_store_event(
                "probe.result", probe["id"],
                {"passed": passed, "result": "pass" if passed else "fail"})

        return {
            "probe_id": probe.get("id"),
            "question": question,
            "expected": expected,
            "probe_type": probe.get("probe_type", "unknown"),
            "passed": passed,
            "matched_rank": matched_rank,
            "fact_id": fact_id,
        }

    def _run_system_probes(self) -> list[dict]:
        """Layer 1: Built-in probes that test Null itself.

        Uses temporary data, cleaned up after. Tests:
        1. Learn/recall roundtrip
        2. Contradiction detection
        3. Full-text retrieval (no truncation loss)
        """
        results = []
        test_project = "__null_calibration_test__"
        test_facts = []

        # Event-log suppression (issue #20): these probes learn temp facts
        # and hard-delete them in the finally block below — ephemeral test
        # scaffolding must never enter the append-only event log.
        self._events_suppressed = True
        try:
            # Probe 1: Learn/recall roundtrip
            test_fact = "Calibration test entity XQ7 was created on January 15, 2025"
            entry = self.learn(test_fact, confidence=0.9,
                               project=test_project, source="explicit")
            test_facts.append(entry["id"])
            recall_hits = self.recall("XQ7 January 2025", project=test_project, limit=3, _emit_event=False)
            found = any("XQ7" in r.get("fact", "") for r in recall_hits)
            results.append({
                "probe_id": None,
                "question": "Can recall find a just-learned fact?",
                "expected": "XQ7",
                "probe_type": "system",
                "passed": found,
                "matched_rank": next(
                    (i + 1 for i, r in enumerate(recall_hits) if "XQ7" in r.get("fact", "")),
                    None
                ),
                "fact_id": entry["id"],
            })

            # Probe 2: Specific detail retrieval
            test_fact2 = "Calibration probe agent NR9 wears jersey number 42 in basketball"
            entry2 = self.learn(test_fact2, confidence=0.9,
                                project=test_project, source="explicit")
            test_facts.append(entry2["id"])
            recall_hits2 = self.recall("NR9 jersey number", project=test_project, limit=3, _emit_event=False)
            found2 = any("42" in r.get("fact", "") and "NR9" in r.get("fact", "")
                         for r in recall_hits2)
            results.append({
                "probe_id": None,
                "question": "Can recall retrieve specific numbers from facts?",
                "expected": "42",
                "probe_type": "system",
                "passed": found2,
                "matched_rank": next(
                    (i + 1 for i, r in enumerate(recall_hits2)
                     if "42" in r.get("fact", "")),
                    None
                ),
                "fact_id": entry2["id"],
            })

            # Probe 3: Contradiction detection
            contradiction = self.check_contradiction(
                "NR9 wears jersey number 99", test_project
            )
            has_contradiction = contradiction is not None
            results.append({
                "probe_id": None,
                "question": "Does contradiction detection catch conflicting facts?",
                "expected": "contradiction detected",
                "probe_type": "system",
                "passed": has_contradiction,
                "matched_rank": None,
                "fact_id": None,
            })

        finally:
            # Clean up test data
            for fid in test_facts:
                self.db.conn.execute(
                    "DELETE FROM facts WHERE id = ? AND project = ?",
                    (fid, test_project),
                )
                # Auto-generated probes for the temp facts are orphans once
                # the facts are hard-deleted — remove them too.
                self.db.delete_probes_for_fact(fid)
            self.db.conn.execute(
                "DELETE FROM facts WHERE project = ?", (test_project,)
            )
            self.db.delete_probes_for_fact("__system__")
            self.db.conn.commit()
            self._events_suppressed = False

        return results
