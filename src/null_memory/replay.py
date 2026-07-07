"""Event replay engine — Phase A (issue #20). Phase B's foundation.

Materializes per-writer event logs (genesis snapshots + append-only
``<writer_id>.jsonl`` files) into a SQLite db, and verifies the result
against the live store (doctor's replay-verify check).

Pure by design: stdlib + null_memory.db only — no git, no network, no
embeddings. Replay applies events through direct row writes (the live code
paths minus side effects: no re-embedding, no probe auto-generation, no
sync). Deterministic: genesis events apply first, then log events sorted by
``(ts, writer, seq)``; adds are idempotent (INSERT OR IGNORE), updates are
field-level last-writer-wins.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from null_memory.events import EVENTS_DIRNAME, GENESIS_KIND

# Columns replay knows how to write for facts. The legacy per-personality
# schema lacks the unified-only columns (anchors, crystallize lineage);
# ensure_fact_columns() adds them to the target so events from a unified
# store replay losslessly.
_FACT_COLUMNS = (
    "fact", "confidence", "base_confidence", "project", "source",
    "provenance", "impact", "session_id", "created_at", "last_accessed",
    "access_count", "last_verified", "verified_by", "superseded_by",
    "forgotten", "archived", "tier",
    "anchor_type", "anchor_note", "anchor_at",
    "crystallized_into", "crystallized_from",
)

_EXTRA_FACT_COLUMNS = {
    "anchor_type": "TEXT",
    "anchor_note": "TEXT",
    "anchor_at": "TEXT",
    "crystallized_into": "TEXT",
    "crystallized_from": "TEXT",
}

_FACT_UPDATE_FIELDS = (
    "fact", "confidence", "base_confidence", "impact", "source",
    "provenance", "project", "tier", "superseded_by", "archived",
    "crystallized_into",
)


def ensure_fact_columns(conn: Any) -> None:
    """Add unified-only fact columns to a legacy-schema target (idempotent)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(facts)")}
    for col, sql_type in _EXTRA_FACT_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE facts ADD COLUMN {col} {sql_type}")
    conn.commit()


def _coerce_json_text(value: Any) -> Any:
    """Lists serialize to JSON text for TEXT columns; strings pass through."""
    if isinstance(value, list):
        return json.dumps(value)
    return value


def load_events(events_dir: str) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Read every genesis + log file under events_dir.

    Returns (genesis_events, log_events, high_water) where high_water maps
    writer_id -> seq already folded into a genesis snapshot (log events at
    or below it are skipped by materialize)."""
    genesis_events: list[dict] = []
    log_events: list[dict] = []
    high_water: dict[str, int] = {}
    if not os.path.isdir(events_dir):
        return genesis_events, log_events, high_water

    names = sorted(os.listdir(events_dir))
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(events_dir, name)
        is_genesis = name.startswith(f"{GENESIS_KIND}.")
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn tail line — tolerated, surfaced by verify
                if event.get("kind") == GENESIS_KIND:
                    for writer, seq in (
                            event.get("data", {}).get("high_water") or {}).items():
                        high_water[writer] = max(
                            high_water.get(writer, 0), int(seq))
                    continue
                if is_genesis:
                    genesis_events.append(event)
                else:
                    log_events.append(event)

    log_events.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""),
                                   e.get("seq", 0)))
    return genesis_events, log_events, high_water


# ── Event application ──────────────────────────────────────────────────────


def _apply_fact_add(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    cols = ["id"]
    vals: list[Any] = [ev["id"]]
    for col in _FACT_COLUMNS:
        if col in data:
            cols.append(col)
            vals.append(_coerce_json_text(data[col]))
    if "created_at" not in cols:
        cols.append("created_at")
        vals.append(ev.get("ts", ""))
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT OR IGNORE INTO facts ({', '.join(cols)}) "
        f"VALUES ({placeholders})", vals)


def _apply_fact_update(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    sets, vals = [], []
    for field in _FACT_UPDATE_FIELDS:
        if field in data:
            sets.append(f"{field} = ?")
            vals.append(_coerce_json_text(data[field]))
    if not sets:
        return
    vals.append(ev["id"])
    conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE id = ?", vals)


def _apply_fact_forget(conn: Any, ev: dict) -> None:
    conn.execute("UPDATE facts SET forgotten = 1 WHERE id = ?", (ev["id"],))


def _apply_fact_anchor(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    conn.execute(
        "UPDATE facts SET anchor_type = ?, anchor_note = ?, anchor_at = ? "
        "WHERE id = ?",
        (data.get("anchor_type"), data.get("note", ""),
         data.get("anchor_at") or ev.get("ts"), ev["id"]))


def _apply_tier(conn: Any, ev: dict) -> None:
    tier = ev.get("data", {}).get("tier")
    if tier:
        conn.execute("UPDATE facts SET tier = ? WHERE id = ?",
                     (tier, ev["id"]))


def _apply_decision_add(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    conn.execute(
        """INSERT OR IGNORE INTO decisions
           (id, decision, reasoning, project, session_id, trace, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (int(ev["id"]), data.get("decision", ""), data.get("reasoning", ""),
         data.get("project", "global"), data.get("session_id"),
         json.dumps(data.get("trace", [])),
         data.get("created_at") or ev.get("ts", "")))


def _apply_outcome_add(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    success = data.get("success")
    conn.execute(
        """INSERT OR IGNORE INTO decision_outcomes
           (id, decision_id, outcome, success, recorded_at)
           VALUES (?, ?, ?, ?, ?)""",
        (int(ev["id"]), data.get("decision_id"), data.get("outcome", ""),
         None if success is None else (1 if success else 0),
         data.get("recorded_at") or ev.get("ts", "")))


def _apply_mistake_add(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    conn.execute(
        """INSERT OR IGNORE INTO mistakes
           (id, mistake, why, project, confidence, session_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (int(ev["id"]), data.get("mistake", ""), data.get("why", ""),
         data.get("project", "global"), data.get("confidence", 0.95),
         data.get("session_id"), data.get("created_at") or ev.get("ts", "")))


def _apply_reflection_add(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    conn.execute(
        """INSERT OR IGNORE INTO reflections
           (id, went_well, missed, do_differently, project, session_id,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (int(ev["id"]), data.get("went_well", ""), data.get("missed", ""),
         data.get("do_differently", ""), data.get("project", "global"),
         data.get("session_id"), data.get("created_at") or ev.get("ts", "")))


def _apply_exemplar_add(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    conn.execute(
        """INSERT OR IGNORE INTO exemplars
           (id, scenario, user_text, agent_text, calibration, tags,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (int(ev["id"]), data.get("scenario", ""), data.get("user_text", ""),
         data.get("agent_text", ""), data.get("calibration", ""),
         json.dumps(data.get("tags", [])),
         data.get("created_at") or ev.get("ts", "")))


def _apply_probe_add(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    conn.execute(
        """INSERT OR IGNORE INTO probes
           (id, question, expected, fact_id, probe_type, created_at,
            run_count, pass_count, last_result, last_run)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (int(ev["id"]), data.get("question", ""), data.get("expected", ""),
         data.get("fact_id"), data.get("probe_type", "user"),
         data.get("created_at") or ev.get("ts", ""),
         data.get("run_count", 0), data.get("pass_count", 0),
         data.get("last_result"), data.get("last_run")))


def _apply_probe_result(conn: Any, ev: dict) -> None:
    data = ev.get("data", {})
    passed = bool(data.get("passed"))
    conn.execute(
        """UPDATE probes SET last_run = ?, last_result = ?,
           run_count = COALESCE(run_count, 0) + 1,
           pass_count = COALESCE(pass_count, 0) + ?
           WHERE id = ?""",
        (ev.get("ts", ""),
         data.get("result", "pass" if passed else "fail"),
         1 if passed else 0, int(ev["id"])))


def _apply_noop(conn: Any, ev: dict) -> None:
    # session.open/close and broadcast carry no store-table state in
    # Phase A (sessions are JSON files; broadcasts live in the hub db).
    return


_APPLIERS = {
    "fact.add": _apply_fact_add,
    "fact.update": _apply_fact_update,
    "fact.forget": _apply_fact_forget,
    "fact.anchor": _apply_fact_anchor,
    "hypnos.promote": _apply_tier,
    "hypnos.demote": _apply_tier,
    "hypnos.synthesis": _apply_fact_add,
    "decision.add": _apply_decision_add,
    "outcome.add": _apply_outcome_add,
    "mistake.add": _apply_mistake_add,
    "reflection.add": _apply_reflection_add,
    "exemplar.add": _apply_exemplar_add,
    "probe.add": _apply_probe_add,
    "probe.result": _apply_probe_result,
    "session.open": _apply_noop,
    "session.close": _apply_noop,
    "broadcast": _apply_noop,
    # Phase B: audit record of an event posted to the org exchange. The
    # exchange clone is the transport of record; replay does nothing.
    "exchange.post": _apply_noop,
}


def apply_events(db: Any, events: list[dict]) -> dict:
    """Apply pre-ordered events to a NullDB target. Returns counters."""
    stats = {"applied": 0, "skipped_unknown": 0}
    conn = db.conn
    for ev in events:
        applier = _APPLIERS.get(ev.get("kind", ""))
        if applier is None:
            stats["skipped_unknown"] += 1
            continue
        applier(conn, ev)
        stats["applied"] += 1
    conn.commit()
    return stats


def materialize(events_dir: str, db: Any) -> dict:
    """Replay genesis + logs from events_dir into db (full rebuild order:
    genesis first, then logs sorted by (ts, writer, seq), skipping log
    events already folded into a genesis snapshot)."""
    genesis_events, log_events, high_water = load_events(events_dir)
    fresh = [
        ev for ev in log_events
        if ev.get("seq", 0) > high_water.get(ev.get("writer", ""), 0)
    ]
    ensure_fact_columns(db.conn)
    stats = apply_events(db, genesis_events + fresh)
    stats["genesis_events"] = len(genesis_events)
    stats["log_events"] = len(fresh)
    stats["log_events_in_snapshot"] = len(log_events) - len(fresh)
    return stats


# ── Replay-verify (doctor check) ───────────────────────────────────────────
#
# Field comparison is deliberately limited to evented state: local
# statistics (access counters, last_accessed, decay-driven archived flags,
# salience/maintenance confidence adjustments) are recomputed locally and
# never evented (design doc), so the diff checks identity-bearing fields
# only — entity id sets per kind, fact tombstones, and a sample of primary
# content fields.

_VERIFY_SAMPLE_FIELDS = {
    "fact": ("fact", "project"),
    "decision": ("decision", "project"),
    "mistake": ("mistake", "why"),
    "reflection": ("went_well", "missed", "do_differently"),
    "exemplar": ("scenario", "user_text", "agent_text"),
    "outcome": ("decision_id", "outcome"),
    "probe": ("question", "expected", "fact_id"),
}

_VERIFY_TABLES = {
    "fact": "facts",
    "decision": "decisions",
    "mistake": "mistakes",
    "reflection": "reflections",
    "exemplar": "exemplars",
    "outcome": "decision_outcomes",
    "probe": "probes",
}

_PERSONALITY_SCOPED = {"decision", "mistake", "reflection", "exemplar", "probe"}


def _rows_by_id(db: Any, kind: str, scoped: bool) -> dict[str, dict]:
    table = _VERIFY_TABLES[kind]
    pred, params = ("", [])
    if scoped:
        if kind == "outcome":
            # No personality column — scope through the owning decision.
            o_pred, o_params = db._personality_predicate("d.personality")
            if o_pred:
                rows = db.conn.execute(
                    f"""SELECT o.* FROM decision_outcomes o
                        JOIN decisions d ON o.decision_id = d.id
                        WHERE {o_pred}""", o_params).fetchall()
                return {str(dict(r)["id"]): dict(r) for r in rows}
        elif kind in _PERSONALITY_SCOPED:
            pred, params = db._personality_predicate()
    where = f" WHERE {pred}" if pred else ""
    rows = db.conn.execute(f"SELECT * FROM {table}{where}", params).fetchall()
    return {str(dict(r)["id"]): dict(r) for r in rows}


def verify_against(live_db: Any, replay_db: Any,
                   sample_size: int = 50) -> dict:
    """Diff a replayed db against the live store.

    Returns {"clean", "drift", "details", "counts"} — drift is the number
    of discrepancies (missing/extra entities, tombstone mismatches, sampled
    field mismatches)."""
    details: list[str] = []
    counts: dict[str, dict[str, int]] = {}

    for kind in _VERIFY_TABLES:
        live = _rows_by_id(live_db, kind, scoped=True)
        replayed = _rows_by_id(replay_db, kind, scoped=False)
        counts[kind] = {"live": len(live), "replayed": len(replayed)}

        live_ids = set(live)
        replay_ids = set(replayed)
        for missing in sorted(live_ids - replay_ids)[:10]:
            details.append(f"{kind} {missing}: in live db, missing from replay")
        n_missing = len(live_ids - replay_ids)
        if n_missing > 10:
            details.append(f"{kind}: ... and {n_missing - 10} more missing")
        for extra in sorted(replay_ids - live_ids)[:10]:
            details.append(f"{kind} {extra}: in replay, missing from live db")
        n_extra = len(replay_ids - live_ids)
        if n_extra > 10:
            details.append(f"{kind}: ... and {n_extra - 10} more extra")

        common = sorted(live_ids & replay_ids)
        if kind == "fact":
            # Tombstones are evented — forgotten flags must agree on every
            # common id.
            for fid in common:
                if bool(live[fid].get("forgotten")) != bool(
                        replayed[fid].get("forgotten")):
                    details.append(
                        f"fact {fid}: forgotten flag mismatch "
                        f"(live={live[fid].get('forgotten')}, "
                        f"replay={replayed[fid].get('forgotten')})")

        # Spot-check field equality on a deterministic sample.
        fields = _VERIFY_SAMPLE_FIELDS[kind]
        step = max(1, len(common) // sample_size) if common else 1
        for entity_id in common[::step][:sample_size]:
            for field in fields:
                lv = live[entity_id].get(field)
                rv = replayed[entity_id].get(field)
                if lv != rv:
                    details.append(
                        f"{kind} {entity_id}: field {field!r} differs "
                        f"(live={lv!r}, replay={rv!r})")

    drift = len(details)
    return {"clean": drift == 0, "drift": drift, "details": details,
            "counts": counts}


def replay_verify(live_db: Any, events_dir: str,
                  sample_size: int = 50) -> dict:
    """Materialize genesis + logs into a temp db and diff against the live
    store. The temp db is discarded; the live db is only read."""
    from null_memory.db import NullDB

    with tempfile.TemporaryDirectory(prefix="null-replay-verify-") as tmp:
        replay_db = NullDB(tmp, personality=getattr(
            live_db, "personality", "atlas"))
        try:
            replay_db.initialize()
            report = dict(materialize(events_dir, replay_db))
            verdict = verify_against(live_db, replay_db,
                                     sample_size=sample_size)
        finally:
            replay_db.close()
    verdict["replay_stats"] = {
        k: report.get(k) for k in
        ("applied", "skipped_unknown", "genesis_events", "log_events",
         "log_events_in_snapshot")
    }
    return verdict
