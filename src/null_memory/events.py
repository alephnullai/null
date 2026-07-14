"""Event-sourced sync — Phase A event emitter (issue #20).

Design: docs/design/EVENT_SOURCED_SYNC.md. Each writer appends only to its
own log: ``<store>/events/<writer_id>.jsonl`` — one JSON event per line,
append-only. ``writer_id = <machine_id>.<personality>`` where machine_id is
a stable identity generated once into the store's config.json (NOT the
ephemeral per-process instance_id from the presence registry).

Phase A scope: dual-write + genesis export + doctor replay-verify, gated
behind ``NULL_EVENT_LOG=1`` (default OFF — zero behavior change when unset).
The replay engine lives in null_memory.replay.

Event schema (exactly per the design doc + the org-topology amendment):

    {"seq": 1042, "writer": "petes-mac.atlas", "ts": "...Z",
     "kind": "fact.add", "id": "c108ef0b9eef", "scope": "org",
     "data": {...}}

Never evented (per the doc): embeddings/FTS (derived), presence heartbeats
(ephemeral), access counters and decay (local statistics, recomputed),
doctor/self-heal actions (local repairs of local state).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("null.events")

EVENTS_DIRNAME = "events"
DEFAULT_SCOPE = "org"

# The kind namespace maps 1:1 onto the existing write surface (design doc).
# ``exchange.post`` is Phase B's dual-log kind: the seat's own event log
# records every event it posted to the org exchange (auditability) — replay
# treats it as a no-op (the exchange clone is the transport of record).
EVENT_KINDS = frozenset({
    "fact.add", "fact.update", "fact.forget", "fact.anchor",
    "decision.add", "outcome.add",
    "mistake.add",
    "reflection.add",
    "probe.add", "probe.result",
    "exemplar.add",
    "session.open", "session.close",
    "broadcast",
    "hypnos.promote", "hypnos.demote", "hypnos.synthesis",
    "exchange.post",
})

# Genesis files carry a manifest header line with this special kind.
GENESIS_KIND = "genesis"


def event_log_enabled() -> bool:
    """Phase A gate: everything event-log is behind NULL_EVENT_LOG=1."""
    return os.environ.get("NULL_EVENT_LOG") == "1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return slug or "machine"


def load_store_config(store_dir: str) -> dict:
    """Read the per-store config.json (machine_id, poke/exchange/doorbell
    settings — agent.config reads the same file and tolerates extra keys).
    Returns {} when missing or unreadable."""
    config_path = os.path.join(store_dir, "config.json")
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_store_config(store_dir: str, cfg: dict) -> None:
    """Atomic write (tmp + fsync + replace) of the per-store config.json —
    same durability idiom as AgentMemory._atomic_write_json."""
    config_path = os.path.join(store_dir, "config.json")
    os.makedirs(store_dir, exist_ok=True)
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, config_path)


def get_machine_id(store_dir: str) -> str:
    """Stable per-machine identity, generated once into the store's
    config.json. Idempotent: subsequent calls return the stored value."""
    cfg = load_store_config(store_dir)
    mid = cfg.get("machine_id")
    if isinstance(mid, str) and mid:
        return mid
    mid = f"{_slug(socket.gethostname())}-{secrets.token_hex(3)}"
    cfg["machine_id"] = mid
    save_store_config(store_dir, cfg)
    return mid


class EventEmitter:
    """Append-only JSONL event writer for one (machine, personality) pair.

    Per-writer monotonic ``seq`` lives in the store's meta table
    (``event_seq.<writer_id>``), allocated under write_transaction so two
    processes on the same machine can never mint the same seq. Appends are
    single write() + fsync on an O_APPEND fd."""

    def __init__(self, store_dir: str, db: Any, personality: str = "atlas"):
        self.store_dir = store_dir
        self.db = db
        self.personality = personality
        self.machine_id = get_machine_id(store_dir)
        self.writer_id = f"{self.machine_id}.{personality}"
        self.events_dir = os.path.join(store_dir, EVENTS_DIRNAME)
        self.log_path = os.path.join(self.events_dir, f"{self.writer_id}.jsonl")
        self._lock = threading.Lock()

    # ── seq counter ──

    @property
    def _seq_key(self) -> str:
        return f"event_seq.{self.writer_id}"

    def current_seq(self) -> int:
        row = self.db.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (self._seq_key,)
        ).fetchone()
        return int(row[0]) if row else 0

    def _next_seq(self) -> int:
        with self.db.write_transaction() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (self._seq_key,)
            ).fetchone()
            seq = (int(row[0]) if row else 0) + 1
            conn.execute(
                """INSERT INTO meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (self._seq_key, str(seq)),
            )
        return seq

    # ── emit ──

    def emit(self, kind: str, entity_id: Any, data: dict,
             scope: str = DEFAULT_SCOPE) -> dict | None:
        """Append one event line. Returns the event dict, or None when the
        event log is disabled. Key order matches the design doc schema."""
        if not event_log_enabled():
            return None
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")
        with self._lock:
            event = {
                "seq": self._next_seq(),
                "writer": self.writer_id,
                "ts": _utc_now_iso(),
                "kind": kind,
                "id": str(entity_id),
                "scope": scope,
                "data": data,
            }
            _append_line(self.log_path, json.dumps(event, ensure_ascii=False))
        return event


def _append_line(path: str, line: str) -> None:
    """Atomic, durable append: one write() on an O_APPEND fd + fsync."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = (line + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


# ── Genesis export ─────────────────────────────────────────────────────────
#
# Exports the current db state as add-events into
# events/genesis.<writer_id>.jsonl — every live entity, ordered
# deterministically (per table, by created_at then id). The first line is a
# manifest header carrying the per-writer high-water seq at export time, so
# replay can skip log events already folded into the snapshot.

_FACT_DATA_FIELDS = (
    "fact", "confidence", "base_confidence", "project", "source",
    "provenance", "impact", "session_id", "created_at", "tier",
    "forgotten", "archived", "superseded_by",
    "anchor_type", "anchor_note", "anchor_at",
    "crystallized_into", "crystallized_from",
)


def _fact_event_data(row: dict) -> dict:
    data = {}
    for field in _FACT_DATA_FIELDS:
        if field not in row:
            continue
        value = row[field]
        if field in ("forgotten", "archived"):
            if value:
                data[field] = 1
            continue
        if value is None:
            continue
        data[field] = value
    return data


def _parse_json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def genesis_path_for(emitter: EventEmitter) -> str:
    return os.path.join(emitter.events_dir,
                        f"{GENESIS_KIND}.{emitter.writer_id}.jsonl")


def _genesis_rows(db: Any) -> list[tuple[str, str, str, dict]]:
    """Yield (kind, entity_id, ts, data) for every entity in the store,
    deterministically ordered. Personality-attributed tables are scoped to
    this connection's personality (unified stores); facts are the shared
    knowledge plane and export unscoped."""
    out: list[tuple[str, str, str, dict]] = []
    conn = db.conn

    rows = conn.execute(
        "SELECT * FROM facts ORDER BY created_at, id").fetchall()
    for r in rows:
        row = dict(r)
        out.append(("fact.add", row["id"],
                    row.get("created_at") or _utc_now_iso(),
                    _fact_event_data(row)))

    pred, params = db._personality_predicate()
    where = f" WHERE {pred}" if pred else ""

    for r in conn.execute(
            f"SELECT * FROM decisions{where} ORDER BY id", params).fetchall():
        row = dict(r)
        out.append(("decision.add", str(row["id"]),
                    row.get("created_at") or _utc_now_iso(), {
            "decision": row.get("decision", ""),
            "reasoning": row.get("reasoning", ""),
            "project": row.get("project", "global"),
            "session_id": row.get("session_id"),
            "trace": _parse_json_list(row.get("trace")),
            "created_at": row.get("created_at"),
        }))

    if pred:
        outcome_rows = conn.execute(
            """SELECT o.* FROM decision_outcomes o
               JOIN decisions d ON o.decision_id = d.id
               WHERE d.personality = ? ORDER BY o.id""",
            params).fetchall()
    else:
        outcome_rows = conn.execute(
            "SELECT * FROM decision_outcomes ORDER BY id").fetchall()
    for r in outcome_rows:
        row = dict(r)
        success = row.get("success")
        out.append(("outcome.add", str(row["id"]),
                    row.get("recorded_at") or _utc_now_iso(), {
            "decision_id": row.get("decision_id"),
            "outcome": row.get("outcome", ""),
            "success": None if success is None else bool(success),
            "recorded_at": row.get("recorded_at"),
        }))

    for r in conn.execute(
            f"SELECT * FROM mistakes{where} ORDER BY id", params).fetchall():
        row = dict(r)
        out.append(("mistake.add", str(row["id"]),
                    row.get("created_at") or _utc_now_iso(), {
            "mistake": row.get("mistake", ""),
            "why": row.get("why", ""),
            "project": row.get("project", "global"),
            "confidence": row.get("confidence", 0.95),
            "session_id": row.get("session_id"),
            "created_at": row.get("created_at"),
        }))

    for r in conn.execute(
            f"SELECT * FROM reflections{where} ORDER BY id", params).fetchall():
        row = dict(r)
        out.append(("reflection.add", str(row["id"]),
                    row.get("created_at") or _utc_now_iso(), {
            "went_well": row.get("went_well", ""),
            "missed": row.get("missed", ""),
            "do_differently": row.get("do_differently", ""),
            "project": row.get("project", "global"),
            "session_id": row.get("session_id"),
            "created_at": row.get("created_at"),
        }))

    for r in conn.execute(
            f"SELECT * FROM exemplars{where} ORDER BY id", params).fetchall():
        row = dict(r)
        out.append(("exemplar.add", str(row["id"]),
                    row.get("created_at") or _utc_now_iso(), {
            "scenario": row.get("scenario", ""),
            "user_text": row.get("user_text", ""),
            "agent_text": row.get("agent_text", ""),
            "calibration": row.get("calibration", ""),
            "tags": _parse_json_list(row.get("tags")),
            "created_at": row.get("created_at"),
        }))

    for r in conn.execute(
            f"SELECT * FROM probes{where} ORDER BY id", params).fetchall():
        row = dict(r)
        data = {
            "question": row.get("question", ""),
            "expected": row.get("expected", ""),
            "fact_id": row.get("fact_id"),
            "probe_type": row.get("probe_type", "user"),
            "created_at": row.get("created_at"),
        }
        # Carry probe-result state so a genesis snapshot taken mid-life
        # reproduces the run/pass counters.
        for field in ("run_count", "pass_count", "last_result", "last_run"):
            if row.get(field):
                data[field] = row[field]
        out.append(("probe.add", str(row["id"]),
                    row.get("created_at") or _utc_now_iso(), data))

    return out


def export_genesis(mem: Any, force: bool = False) -> dict:
    """Export current db state as add-events into
    events/genesis.<writer_id>.jsonl. Idempotent: refuses if this writer's
    genesis already exists unless force=True.

    Returns {"path", "count", "writer"}."""
    if not event_log_enabled():
        raise RuntimeError(
            "event log is disabled — set NULL_EVENT_LOG=1 (Phase A gate)")
    emitter = mem.events
    path = genesis_path_for(emitter)
    if os.path.exists(path) and not force:
        raise FileExistsError(path)

    rows = _genesis_rows(mem.db)
    now = _utc_now_iso()
    lines = []
    header = {
        "seq": 0,
        "writer": emitter.writer_id,
        "ts": now,
        "kind": GENESIS_KIND,
        "id": emitter.writer_id,
        "scope": DEFAULT_SCOPE,
        "data": {"high_water": {emitter.writer_id: emitter.current_seq()}},
    }
    lines.append(json.dumps(header, ensure_ascii=False))
    for i, (kind, entity_id, ts, data) in enumerate(rows, start=1):
        lines.append(json.dumps({
            "seq": i,
            "writer": emitter.writer_id,
            "ts": ts,
            "kind": kind,
            "id": entity_id,
            "scope": DEFAULT_SCOPE,
            "data": data,
        }, ensure_ascii=False))

    os.makedirs(emitter.events_dir, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    return {"path": path, "count": len(rows), "writer": emitter.writer_id}
