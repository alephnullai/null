"""Org exchange — typed messages between different-identity seats
(issue #20 Phase B; design: docs/design/ORG_TOPOLOGY.md, user guide:
docs/EXCHANGE.md).

The exchange is the org's hallway: a shared git repo of per-seat
append-only streams (``streams/<writer_id>.jsonl``). Announcements,
reports, claims, and queries live here; ARTIFACTS STAY IN THEIR HOMES —
a ``repo.push`` event prompts a human/agent to pull a code repo, it never
carries code. Privacy by construction: nothing private enters the
exchange, and tier access is just repo membership.

Single-writer-per-file (each seat appends only to its own stream) makes
merge conflicts structurally impossible — the same invariant as the
store event logs.

Config (per-store config.json — see events.load_store_config):

    "exchange": {
        "url": "git@github.com:yourorg/org-exchange.git",
        "stream": "petes-mac-ab12cd.atlas",      # optional; default writer_id
        "subscribe": ["steve-linux-9f00aa.steve"],
        "confidence_discount": 0.85               # optional
    }

The local clone lives at ``<store>/exchange/`` and is gitignored from the
store repo (two repos must never nest-track each other).

Ingestion (runs inside the poke cycle — see poke.poke_once):
  * report.session / broadcast / directive / query.answer → facts in the
    local store with full provenance (source = "exchange:<writer>",
    provenance = "exchange", confidence discounted for non-self).
  * repo.push → surfaced in the briefing as a pull recommendation —
    NEVER auto-pulled.
  * claim.acquire / claim.release → a local advisory claims view with TTL
    expiry (briefing/status: "⚠ <writer> holds <resource>, Nm left").
  * query.ask → pending-questions view for the hub to answer.

Only SUBSCRIBED streams are read — an unsubscribed stream is never
ingested (scoping by construction).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from null_memory.events import (
    DEFAULT_SCOPE,
    _append_line,
    _utc_now_iso,
    get_machine_id,
    load_store_config,
)

logger = logging.getLogger("null.exchange")

EXCHANGE_DIRNAME = "exchange"
STREAMS_DIRNAME = "streams"

EXCHANGE_KINDS = frozenset({
    "report.session",   # consolidated work product flowing up
    "repo.push",        # {repo, sha, branch, summary} — prompts a pull
    "broadcast",        # team-visible announcement
    "claim.acquire",    # {resource, ttl_minutes} — advisory WIP claim
    "claim.release",    # {resource}
    "query.ask",        # {question, project?} — async question upward
    "query.answer",     # {query_id, answer, project?}
    "directive",        # decision-in-force flowing down
})

# Kinds that materialize as facts in the ingesting store.
_FACT_KINDS = frozenset({
    "report.session", "broadcast", "directive", "query.answer"})

DEFAULT_CONFIDENCE_DISCOUNT = 0.85
DEFAULT_INGEST_CONFIDENCE = 0.8

# Meta keys for the local exchange views.
_CLAIMS_KEY = "exchange_claims"
_PUSHES_KEY = "exchange_repo_pushes"
_QUERIES_KEY = "exchange_queries"
_MAX_PUSHES = 20
_MAX_QUERIES = 20


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _age_minutes(ts: str) -> int | None:
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return int((datetime.now(timezone.utc) - dt).total_seconds() // 60)


def exchange_config(store_dir: str) -> dict | None:
    """The store's exchange config block, or None when not configured."""
    cfg = load_store_config(store_dir).get("exchange")
    if isinstance(cfg, dict) and cfg.get("url"):
        return cfg
    return None


def own_stream_name(store_dir: str, personality: str = "atlas",
                    create: bool = True) -> str:
    """This seat's outbound stream name: configured override or the
    writer_id (``<machine_id>.<personality>``).

    ``create=False`` is the read-only variant for render paths (briefing/
    status): it never generates a machine_id as a side effect and returns
    "" when none exists yet."""
    cfg = exchange_config(store_dir) or {}
    stream = cfg.get("stream")
    if isinstance(stream, str) and stream:
        return stream
    if create:
        return f"{get_machine_id(store_dir)}.{personality}"
    mid = load_store_config(store_dir).get("machine_id") or ""
    return f"{mid}.{personality}" if mid else ""


# ── JSON meta views (claims / repo pushes / pending queries) ───────────────


def _load_meta_json(db: Any, key: str, default: Any):
    raw = db.get_meta(key)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


def _save_meta_json(db: Any, key: str, value: Any) -> None:
    db.set_meta(key, json.dumps(value, ensure_ascii=False))
    db.conn.commit()


def active_claims(db: Any) -> list[dict]:
    """Current advisory claims, TTL-expired entries filtered out.
    Each: {resource, writer, acquired_at, expires_at}."""
    claims = _load_meta_json(db, _CLAIMS_KEY, {})
    now = datetime.now(timezone.utc)
    out = []
    for resource, claim in sorted(claims.items()):
        expires = _parse_iso(claim.get("expires_at", ""))
        if expires is None or expires <= now:
            continue  # expired — advisory claims die on TTL, no cleanup needed
        out.append({"resource": resource, **claim})
    return out


def recent_repo_pushes(db: Any, within_hours: float = 24.0) -> list[dict]:
    """repo.push events seen recently (newest first).
    Each: {writer, repo, sha, branch, summary, ts}."""
    pushes = _load_meta_json(db, _PUSHES_KEY, [])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    out = [p for p in pushes
           if (_parse_iso(p.get("ts", "")) or cutoff) > cutoff]
    out.sort(key=lambda p: p.get("ts", ""), reverse=True)
    return out


def pending_queries(db: Any) -> list[dict]:
    """query.ask events awaiting an answer (for the hub to handle).
    Each: {id, writer, question, project, ts}."""
    return _load_meta_json(db, _QUERIES_KEY, [])


# ── The client ──────────────────────────────────────────────────────────────


class ExchangeClient:
    """Post to your own stream; ingest subscribed foreign streams.

    The Python API for the exchange: ``ExchangeClient(mem).post(...)`` /
    ``.announce_push(...)`` / ``.ingest()``. The CLI (`null exchange ...`)
    is a thin wrapper over this class."""

    def __init__(self, mem: Any):
        self.mem = mem
        self.db = mem.db
        self.store_dir = os.path.dirname(mem.db.db_path)
        self.config = exchange_config(self.store_dir) or {}
        self.personality = getattr(mem, "personality", "atlas")
        self.clone_dir = os.path.join(self.store_dir, EXCHANGE_DIRNAME)
        self.streams_dir = os.path.join(self.clone_dir, STREAMS_DIRNAME)

    @property
    def available(self) -> bool:
        return bool(self.config.get("url"))

    @property
    def stream(self) -> str:
        return own_stream_name(self.store_dir, self.personality)

    @property
    def subscribed(self) -> list[str]:
        subs = self.config.get("subscribe") or []
        if not isinstance(subs, list):
            return []
        # Never ingest your own stream, even if misconfigured to.
        return [s for s in subs if isinstance(s, str) and s != self.stream]

    # ── git plumbing (hardened) ──

    def _git(self, args: list[str], timeout: int = 30):
        from null_memory.session import _run_git
        return _run_git(args, cwd=self.clone_dir, timeout=timeout)

    def _ensure_store_gitignore(self) -> None:
        """The exchange clone must never be tracked by the store repo."""
        from null_memory.session import MemoryRepo
        repo_root = MemoryRepo(self.store_dir).repo_dir
        gitignore = os.path.join(repo_root, ".gitignore")
        entry = f"{EXCHANGE_DIRNAME}/"
        try:
            existing = ""
            if os.path.isfile(gitignore):
                # errors="replace": .gitignores written by older builds
                # used the platform default encoding (cp1252 on Windows —
                # the header's em dash is 0x97 there), and a strict UTF-8
                # read crashed every exchange operation on the seat.
                # Mojibake in a comment is harmless; a dead exchange isn't.
                with open(gitignore, "r", encoding="utf-8",
                          errors="replace") as f:
                    existing = f.read()
            if entry not in existing.splitlines():
                with open(gitignore, "a", encoding="utf-8") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write(f"# org exchange clone — its own repo, never "
                            f"tracked by the store\n{entry}\n")
        except OSError as exc:
            logger.warning("[exchange] could not update store .gitignore: "
                           "%s", exc)

    def ensure_clone(self) -> bool:
        """Clone the exchange repo under <store>/exchange/ if missing.
        Returns True when a usable clone exists."""
        if not self.available:
            return False
        self._ensure_store_gitignore()
        if os.path.isdir(os.path.join(self.clone_dir, ".git")):
            return True
        from null_memory.session import _run_git
        os.makedirs(self.store_dir, exist_ok=True)
        res = _run_git(["clone", str(self.config["url"]), self.clone_dir],
                       cwd=self.store_dir, timeout=60)
        if res.returncode != 0:
            logger.warning("[exchange] clone failed: %s", res.stderr.strip())
            return False
        return True

    def _pull(self) -> Any:
        return self._git(["pull", "--ff-only", "--quiet"])

    # ── posting (own stream) ──

    def _next_seq(self) -> int:
        key = f"exchange_seq.{self.stream}"
        with self.db.write_transaction() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            seq = (int(row[0]) if row else 0) + 1
            conn.execute(
                """INSERT INTO meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, str(seq)),
            )
        return seq

    def post(self, kind: str, data: dict, scope: str = DEFAULT_SCOPE) -> dict:
        """Append one event to OWN stream, commit, push. Returns the event.

        Also (a) dual-logs the post into the seat's own store event log
        (auditability — when NULL_EVENT_LOG=1), (b) applies the event to
        the local views (your own claims show up in your own status), and
        (c) rings the doorbell peers (silent, best-effort)."""
        if kind not in EXCHANGE_KINDS:
            raise ValueError(f"unknown exchange kind: {kind!r} "
                             f"(valid: {', '.join(sorted(EXCHANGE_KINDS))})")
        if not isinstance(data, dict):
            raise ValueError("exchange event data must be a JSON object")
        if not self.ensure_clone():
            raise RuntimeError(
                "exchange not available — configure the 'exchange' block in "
                "the store config.json (see docs/EXCHANGE.md)")

        # Reduce push rejects: catch up first (ff-only; streams are
        # single-writer so this can only fast-forward or no-op).
        self._pull()

        event = {
            "seq": self._next_seq(),
            "writer": self.stream,
            "ts": _utc_now_iso(),
            "kind": kind,
            "id": uuid.uuid4().hex[:12],
            "scope": scope,
            "data": data,
        }
        stream_path = os.path.join(self.streams_dir, f"{self.stream}.jsonl")
        _append_line(stream_path, json.dumps(event, ensure_ascii=False))

        rel = os.path.join(STREAMS_DIRNAME, f"{self.stream}.jsonl")
        self._git(["add", rel])
        self._git(["commit", "-m", f"{self.stream}: {kind} #{event['seq']}",
                   "--quiet"])
        pushed = self._git(["push", "--quiet"]).returncode == 0
        if not pushed:
            # Someone else pushed since our pull. Single-writer-per-file
            # makes rebase trivially safe; retry once.
            self._git(["pull", "--rebase", "--quiet"])
            pushed = self._git(["push", "--quiet"]).returncode == 0
        if not pushed:
            logger.warning("[exchange] push failed for %s #%s — the line is "
                           "committed locally and will go out on the next "
                           "post/sync", kind, event["seq"])

        # Dual-log locally: the seat's own event log records what it posted.
        try:
            self.mem._emit_store_event(
                "exchange.post", event["id"],
                {"kind": kind, "stream": self.stream,
                 "exchange_seq": event["seq"], "data": data},
                scope=scope)
        except Exception:  # noqa: BLE001 — audit trail must not break posting
            pass

        # Own claims/pushes update the local view too (status completeness).
        try:
            self._apply_view_event(event, self.stream)
        except Exception:  # noqa: BLE001
            pass

        # Doorbell: accelerate peers. Silent — the poll is the guarantee.
        try:
            from null_memory.doorbell import ring_from_store
            ring_from_store(self.store_dir)
        except Exception:  # noqa: BLE001
            pass
        return event

    def announce_push(self, repo_cwd: str, summary: str = "") -> dict:
        """Read the cwd git repo's HEAD/branch/remote and post repo.push.
        One command after any code push: ``null exchange announce-push``."""
        from null_memory.session import _run_git, capture_git_state
        sha, branch = capture_git_state(repo_cwd)
        if not sha:
            raise RuntimeError(f"not a git repo (or no HEAD): {repo_cwd}")
        remote = _run_git(["remote", "get-url", "origin"], cwd=repo_cwd)
        remote_url = remote.stdout.strip() if remote.returncode == 0 else ""
        repo_name = (remote_url.rstrip("/").rsplit("/", 1)[-1]
                     .removesuffix(".git")
                     if remote_url else os.path.basename(
                         os.path.abspath(repo_cwd)))
        return self.post("repo.push", {
            "repo": repo_name,
            "sha": sha,
            "branch": branch or "",
            "summary": summary,
            "remote": remote_url,
        })

    # ── ingestion (subscribed foreign streams) ──

    def ingest(self) -> dict:
        """Fetch the exchange clone and ingest new lines from SUBSCRIBED
        foreign streams. Idempotent: per-stream byte cursors + deterministic
        fact ids. Returns a report dict."""
        report: dict[str, Any] = {
            "streams": {}, "facts": 0, "claims": 0, "repo_pushes": 0,
            "queries": 0, "warning": None,
        }
        if not self.available or not self.ensure_clone():
            report["warning"] = "exchange not configured"
            return report
        pull = self._pull()
        if pull.returncode != 0:
            # Streams can't diverge (single writer per file); anything else
            # is surfaced, never merged.
            report["warning"] = (
                "exchange pull was not a fast-forward — not merging "
                f"({(pull.stderr or '').strip()[:200]})")

        for stream in self.subscribed:
            path = os.path.join(self.streams_dir, f"{stream}.jsonl")
            if not os.path.isfile(path):
                continue
            cursor_key = f"exchange_cursor.{stream}"
            offset = int(self.db.get_meta(cursor_key) or 0)
            size = os.path.getsize(path)
            if size < offset:
                offset = 0  # stream rewritten/reset — re-ingest (idempotent)
            if size == offset:
                continue
            events, new_offset = _read_new_lines(path, offset)
            ingested = 0
            for ev in events:
                try:
                    self._apply_foreign_event(ev, stream)
                    ingested += 1
                    kind = ev.get("kind", "")
                    if kind in _FACT_KINDS:
                        report["facts"] += 1
                    elif kind == "repo.push":
                        report["repo_pushes"] += 1
                    elif kind in ("claim.acquire", "claim.release"):
                        report["claims"] += 1
                    elif kind == "query.ask":
                        report["queries"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[exchange] ingest failed for %s event "
                                   "%s: %s", stream, ev.get("id"), exc)
            self.db.set_meta(cursor_key, str(new_offset))
            self.db.conn.commit()
            report["streams"][stream] = ingested
        return report

    # ── attention (the /loop layer) ──

    def attend(self, dry_run: bool = False, limit: int = 0) -> dict:
        """Surface subscribed-stream events the CONVERSATIONAL layer hasn't
        seen yet, and advance the per-stream ATTENDED cursor.

        The non-obvious crux (dual cursor): the daemon poke loop already
        ``ingest()``s the exchange and advances the per-stream INGEST cursor
        (``exchange_cursor.<stream>``). A naive re-ingest here would find
        "nothing new" because the daemon already consumed it into the store.
        So attention tracks its OWN offset — ``exchange_attended.<stream>``,
        the byte position the conversational layer has SURFACED — entirely
        independent of what the daemon ingested. We read the stream files
        directly from the attended offset, never the ingest delta.

        Returns ``{"items": [...], "warning": str|None}``. Each item:
        ``{writer, kind, ts, seq, id, text, data}``. ``dry_run=True`` shows
        without advancing the cursor; ``limit > 0`` caps total items
        returned (the cursor still advances over everything scanned, so a
        capped run doesn't re-surface the remainder next tick)."""
        result: dict[str, Any] = {"items": [], "warning": None}
        if not self.available or not self.ensure_clone():
            result["warning"] = "exchange not configured"
            return result
        # Ensure the clone is current. Harmless if the daemon already
        # pulled — but we must not depend on ingest's delta for what to
        # surface, so we read the files ourselves below.
        pull = self._pull()
        if pull.returncode != 0:
            result["warning"] = (
                "exchange pull was not a fast-forward — not merging "
                f"({(pull.stderr or '').strip()[:200]})")

        items: list[dict] = []
        cursor_updates: list[tuple[str, int]] = []
        for stream in self.subscribed:  # never own stream (see .subscribed)
            path = os.path.join(self.streams_dir, f"{stream}.jsonl")
            if not os.path.isfile(path):
                continue
            cursor_key = f"exchange_attended.{stream}"
            offset = int(self.db.get_meta(cursor_key) or 0)
            size = os.path.getsize(path)
            if size < offset:
                offset = 0  # stream rewritten/reset — re-surface from top
            if size == offset:
                continue
            events, new_offset = _read_new_lines(path, offset)
            for ev in events:
                writer = ev.get("writer") or stream
                if writer == self.stream:
                    continue  # belt-and-suspenders: never surface own writes
                data = ev.get("data", {}) or {}
                text = (data.get("summary") or data.get("text")
                        or data.get("message") or data.get("answer")
                        or data.get("directive") or data.get("question")
                        or "")
                items.append({
                    "writer": writer,
                    "kind": ev.get("kind", ""),
                    "ts": ev.get("ts", ""),
                    "seq": ev.get("seq", 0),
                    "id": ev.get("id", ""),
                    "text": str(text),
                    "data": data,
                })
            cursor_updates.append((cursor_key, new_offset))

        items.sort(key=lambda it: (it.get("ts", ""), it.get("writer", ""),
                                   it.get("seq", 0)))
        if limit and limit > 0:
            items = items[:limit]
        result["items"] = items

        # Advance the attended cursor(s) only when surfacing for real. We
        # advance over EVERYTHING scanned (not just the capped slice): a
        # limited run is an explicit "show me a few", not a reason to
        # re-surface the rest forever.
        if not dry_run and cursor_updates:
            for key, off in cursor_updates:
                self.db.set_meta(key, str(off))
            self.db.conn.commit()

        # Instrumentation (experimental-feature cost telemetry): count real
        # ticks so `null status` can show how often the loop wakes and what
        # fraction of wakes were idle. A loop spends tokens every wake, so
        # the news/quiet split is the signal Pete asked to measure before
        # this ships non-experimental. dry-run is manual inspection, not a
        # loop tick — never counted.
        if not dry_run:
            self._bump_attend_counter("attend.ticks_total")
            self._bump_attend_counter(
                "attend.ticks_news" if items else "attend.ticks_quiet")
            self.db.conn.commit()
        return result

    def _bump_attend_counter(self, key: str) -> None:
        """Increment an attend tick counter in meta (fail-soft)."""
        try:
            cur = int(self.db.get_meta(key) or 0)
        except (TypeError, ValueError):
            cur = 0
        self.db.set_meta(key, str(cur + 1))

    def _apply_foreign_event(self, ev: dict, stream: str) -> None:
        writer = ev.get("writer") or stream
        kind = ev.get("kind", "")
        if kind in _FACT_KINDS:
            self._ingest_fact(ev, writer)
        self._apply_view_event(ev, writer)

    def _ingest_fact(self, ev: dict, writer: str) -> None:
        """Materialize a fact-bearing exchange event into the local store
        with provenance: source records the writer, confidence carries the
        non-self discount, project comes from the event data."""
        data = ev.get("data", {}) or {}
        text = (data.get("summary") or data.get("text")
                or data.get("message") or data.get("answer")
                or data.get("directive") or "")
        if not text:
            return  # nothing to say — skip silently
        kind = ev.get("kind", "")
        discount = float(self.config.get(
            "confidence_discount", DEFAULT_CONFIDENCE_DISCOUNT))
        base = float(data.get("confidence", DEFAULT_INGEST_CONFIDENCE))
        confidence = round(min(0.99, max(0.05, base)) * discount, 4)
        # Deterministic id from (writer, seq) — re-ingest is a no-op
        # (INSERT OR IGNORE), which is what makes cursors resettable.
        fid = hashlib.sha256(
            f"exchange:{writer}:{ev.get('seq', 0)}:{ev.get('id', '')}"
            .encode("utf-8")).hexdigest()[:16]
        entry = {
            "id": fid,
            "fact": str(text),
            "confidence": confidence,
            "base_confidence": confidence,
            "project": str(data.get("project", "global")),
            "source": f"exchange:{writer}",
            "provenance": "exchange",
            "impact": 0.7 if kind == "directive" else 0.5,
            "created_at": ev.get("ts") or _utc_now_iso(),
        }
        with self.db.write_transaction():
            self.db.insert_fact(entry)
        # Dual-write so same-store replicas converge through the event log
        # and doctor replay-verify stays clean. Each replica also ingests
        # the exchange itself; the deterministic id makes both paths meet
        # in an idempotent add.
        try:
            self.mem._emit_store_event("fact.add", fid, {
                k: v for k, v in entry.items() if k != "id" and v is not None
            })
        except Exception:  # noqa: BLE001
            pass

    def _apply_view_event(self, ev: dict, writer: str) -> None:
        """Update the local meta views (claims / repo pushes / queries)."""
        kind = ev.get("kind", "")
        data = ev.get("data", {}) or {}
        if kind == "claim.acquire":
            resource = str(data.get("resource", "")).strip()
            if not resource:
                return
            ttl = float(data.get("ttl_minutes", 60))
            acquired = _parse_iso(ev.get("ts", "")) or datetime.now(
                timezone.utc)
            claims = _load_meta_json(self.db, _CLAIMS_KEY, {})
            claims[resource] = {
                "writer": writer,
                "acquired_at": acquired.isoformat(),
                "expires_at": (acquired + timedelta(minutes=ttl)).isoformat(),
            }
            _save_meta_json(self.db, _CLAIMS_KEY, claims)
        elif kind == "claim.release":
            resource = str(data.get("resource", "")).strip()
            claims = _load_meta_json(self.db, _CLAIMS_KEY, {})
            existing = claims.get(resource)
            if existing and existing.get("writer") == writer:
                del claims[resource]
                _save_meta_json(self.db, _CLAIMS_KEY, claims)
        elif kind == "repo.push":
            if writer == self.stream:
                return  # don't warn yourself about your own push
            pushes = _load_meta_json(self.db, _PUSHES_KEY, [])
            pushes.append({
                "writer": writer,
                "repo": str(data.get("repo", "?")),
                "sha": str(data.get("sha", "")),
                "branch": str(data.get("branch", "")),
                "summary": str(data.get("summary", "")),
                "ts": ev.get("ts", ""),
            })
            _save_meta_json(self.db, _PUSHES_KEY, pushes[-_MAX_PUSHES:])
        elif kind == "query.ask":
            if writer == self.stream:
                return  # your own questions aren't for you to answer
            queries = _load_meta_json(self.db, _QUERIES_KEY, [])
            qid = ev.get("id", "")
            if any(q.get("id") == qid for q in queries):
                return
            queries.append({
                "id": qid,
                "writer": writer,
                "question": str(data.get("question", "")),
                "project": str(data.get("project", "global")),
                "ts": ev.get("ts", ""),
            })
            _save_meta_json(self.db, _QUERIES_KEY, queries[-_MAX_QUERIES:])
        elif kind == "query.answer":
            target = data.get("query_id", "")
            queries = _load_meta_json(self.db, _QUERIES_KEY, [])
            remaining = [q for q in queries if q.get("id") != target]
            if len(remaining) != len(queries):
                _save_meta_json(self.db, _QUERIES_KEY, remaining)


def _read_new_lines(path: str, offset: int) -> tuple[list[dict], int]:
    """Read complete JSONL lines from ``offset``. A torn tail line (no
    trailing newline yet — a write in flight) is held back: the returned
    offset stops before it, so the next ingest picks it up whole."""
    events: list[dict] = []
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    end = len(chunk)
    if chunk and not chunk.endswith(b"\n"):
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return [], offset  # one torn line, nothing complete yet
        end = last_nl + 1
        chunk = chunk[:end]
    for raw in chunk.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            events.append(json.loads(raw.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # garbage line — skip, never crash the cycle
    return events, offset + end


# ── briefing / status rendering ─────────────────────────────────────────────


def exchange_briefing_lines(db: Any, own_stream: str | None = None,
                            within_hours: float = 24.0) -> list[str]:
    """Compact exchange section for the briefing: fresh repo.push warnings
    (pull recommended — never auto-pulled), active foreign claims, and
    pending queries. Token-budget bar: bounded lines, only when fresh."""
    lines: list[str] = []
    for p in recent_repo_pushes(db, within_hours=within_hours)[:3]:
        sha7 = (p.get("sha") or "")[:7]
        lines.append(f"  ⚠ {p.get('writer', '?')} pushed "
                     f"{p.get('repo', '?')}@{sha7} — pull recommended")
    for c in active_claims(db):
        if own_stream and c.get("writer") == own_stream:
            continue
        mins = _age_minutes(c.get("expires_at", ""))
        left = -mins if mins is not None and mins < 0 else 0
        lines.append(f"  ⚠ {c.get('writer', '?')} holds "
                     f"{c.get('resource', '?')}, {left}m left")
    for q in pending_queries(db)[:3]:
        lines.append(f"  ? {q.get('writer', '?')} asks: "
                     f"{(q.get('question') or '')[:80]} "
                     f"(answer: null exchange post --kind query.answer)")
    return lines


_KIND_LABELS = {
    "broadcast": "📣 broadcast",
    "report.session": "📋 report",
    "directive": "➡ directive",
    "query.ask": "❓ question",
    "query.answer": "💬 answer",
    "repo.push": "⬆ repo push",
    "claim.acquire": "🔒 claim",
    "claim.release": "🔓 release",
}


def attend_render_lines(items: list[dict]) -> list[str]:
    """LOUD, grouped rendering of attend() items for the /loop tick.

    Grouped by sender (writer); within a sender, newest context flows top
    to bottom in surface order. Empty list → empty list (caller stays
    quiet)."""
    if not items:
        return []
    by_writer: dict[str, list[dict]] = {}
    for it in items:
        by_writer.setdefault(it.get("writer", "?"), []).append(it)

    n = len(items)
    lines = [f"📨 {n} new exchange message{'s' if n != 1 else ''} "
             f"from {len(by_writer)} seat{'s' if len(by_writer) != 1 else ''}:"]
    for writer in sorted(by_writer):
        lines.append(f"── from {writer} ──")
        for it in by_writer[writer]:
            kind = it.get("kind", "")
            label = _KIND_LABELS.get(kind, kind or "?")
            ts = (it.get("ts") or "")[:16]
            head = f"  {label}"
            if ts:
                head += f"  ({ts})"
            lines.append(head)
            text = (it.get("text") or "").strip()
            if text:
                for tl in text.splitlines():
                    lines.append(f"      {tl}")
            else:
                # No prose field — show the structured payload so claims /
                # repo.push still say something useful.
                data = it.get("data", {}) or {}
                if data:
                    summary = ", ".join(
                        f"{k}={v}" for k, v in data.items()
                        if k not in ("confidence",))
                    if summary:
                        lines.append(f"      {summary}")
    return lines


def attend_status_lines(db: Any) -> list[str]:
    """Attention-loop telemetry for ``null status`` (experimental).

    Returns nothing until the loop has actually ticked at least once, so
    seats that never opt in see no noise. Surfaces total ticks and the
    idle (quiet) fraction — the cost signal for deciding whether the
    feature graduates from experimental."""
    def _ctr(key: str) -> int:
        try:
            return int(db.get_meta(key) or 0)
        except (TypeError, ValueError):
            return 0
    total = _ctr("attend.ticks_total")
    if total <= 0:
        return []
    news = _ctr("attend.ticks_news")
    quiet = _ctr("attend.ticks_quiet")
    pct = round(100 * quiet / total) if total else 0
    return [f"  attention loop [experimental]: {total} ticks "
            f"({news} surfaced, {quiet} idle — {pct}% idle)"]


def claims_status_lines(db: Any) -> list[str]:
    """Claims lines for ``null status`` (includes own claims)."""
    lines = []
    for c in active_claims(db):
        mins = _age_minutes(c.get("expires_at", ""))
        left = -mins if mins is not None and mins < 0 else 0
        lines.append(f"  ⚠ {c.get('writer', '?')} holds "
                     f"{c.get('resource', '?')}, {left}m left")
    return lines
