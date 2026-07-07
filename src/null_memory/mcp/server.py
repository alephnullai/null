"""Null MCP server — persistent agent memory via Model Context Protocol."""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from null_memory.mcp.handlers import NullHandlers


_logger = logging.getLogger(__name__)


# ── Per-tool-call watchdog (responsiveness contract) ─────────────────────
# Root-cause class A from the hang ledger: slow/fallible work on a
# must-respond path. Every tool on the 15-tool surface runs under a
# watchdog with two budgets:
#
#   soft (NULL_TOOL_BUDGET, default 15s)  — the call completes normally but
#       a breadcrumb is recorded (meta key `tool_budget_violations` +
#       stderr line) so `null doctor` can surface chronic slowness.
#   hard (NULL_TOOL_HARD_BUDGET, default 60s) — the call RETURNS AN ERROR
#       RESULT to the client instead of hanging it. The runaway work is
#       left to finish on its abandoned worker thread (threads are never
#       killed — return-and-abandon, with the breadcrumb making the
#       abandonment diagnosable).
#
# SDK reality (mcp 1.26 FastMCP): a SYNC tool function is invoked directly
# on the asyncio event loop (`func_metadata.call_fn_with_arg_validation`
# does a plain `fn(**args)` for sync fns) — one blocked handler freezes
# the ENTIRE server, which is exactly how the 9-minute null_identity hang
# manifested. ASYNC tool functions, however, are awaited natively. So the
# watchdog registers each tool as an async wrapper that offloads the real
# (sync) handler to a worker thread via anyio.to_thread.run_sync with
# abandon_on_cancel=True under a move_on_after(hard) cancel scope. This
# gives a true return-early: the event loop stays responsive throughout,
# and at the hard budget the await is cancelled (abandoning the thread)
# and an error string is returned to the client.
#
# Honest limitations of return-and-abandon:
#   * The abandoned handler KEEPS RUNNING and may still mutate the store
#     after the client got the error — the client must treat a hard-budget
#     error as "outcome unknown", not "nothing happened". (The breadcrumb
#     records the abandonment so doctor can explain surprise writes.)
#   * Handlers now execute OFF the event-loop thread, so two calls can
#     overlap (e.g. an abandoned call and its retry). The store layer was
#     already multi-thread by design — per-thread SQLite connections + WAL
#     (db.NullDB.conn), and background workers (Hypnos, debounced sync)
#     have always shared it — but handlers are not otherwise serialized.
#   * anyio's thread-limiter token is held until an abandoned thread
#     finishes (default limiter: 40) — a pathological flood of runaway
#     calls could exhaust it; acceptable because each one is already a
#     logged contract violation.
TOOL_BUDGET_ENV = "NULL_TOOL_BUDGET"
TOOL_HARD_BUDGET_ENV = "NULL_TOOL_HARD_BUDGET"
DEFAULT_TOOL_BUDGET = 15.0
DEFAULT_TOOL_HARD_BUDGET = 60.0
BUDGET_VIOLATIONS_META_KEY = "tool_budget_violations"
_BUDGET_VIOLATIONS_KEEP = 20  # bounded breadcrumb history


def _env_float(name: str, default: float) -> float:
    """Parse a float env var; fall back to default on unset/garbage."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def tool_budgets() -> tuple[float, float]:
    """Return (soft, hard) per-tool-call budgets in seconds.

    hard is clamped to at least soft — a hard budget below the soft one
    would return errors for calls that were never even flagged slow."""
    soft = _env_float(TOOL_BUDGET_ENV, DEFAULT_TOOL_BUDGET)
    hard = max(_env_float(TOOL_HARD_BUDGET_ENV, DEFAULT_TOOL_HARD_BUDGET), soft)
    return soft, hard


def _record_budget_violation(
    handlers: NullHandlers, tool: str, elapsed: float,
    budget: float, kind: str,
) -> None:
    """Persist a budget-violation breadcrumb (meta) + stderr line.

    Best-effort by design: the breadcrumb write happens on the must-respond
    path, so it must never raise. The stderr line is the always-works
    fallback (visible in the daemon/launchd log even if the DB write
    fails); the meta JSON list is what `null doctor` surfaces."""
    print(
        f"[null] tool budget {kind.upper()}: {tool} took {elapsed:.1f}s "
        f"(budget {budget:.0f}s)",
        file=sys.stderr,
    )
    try:
        import json as _json

        db = handlers.memory.db
        raw = db.get_meta(BUDGET_VIOLATIONS_META_KEY) or "[]"
        try:
            entries = _json.loads(raw)
            if not isinstance(entries, list):
                entries = []
        except ValueError:
            entries = []
        entries.append({
            "tool": tool,
            "elapsed_s": round(elapsed, 3),
            "budget_s": budget,
            "kind": kind,  # "soft" (slow but completed) | "hard" (abandoned)
            "at": datetime.now(timezone.utc).isoformat(),
        })
        db.set_meta(
            BUDGET_VIOLATIONS_META_KEY,
            _json.dumps(entries[-_BUDGET_VIOLATIONS_KEEP:]),
        )
        db.conn.commit()
    except Exception:  # noqa: BLE001 — breadcrumb must never break a tool call
        pass


async def _watchdog_call(handlers: NullHandlers, tool: str, fn) -> str:
    """Run a sync tool handler under the soft/hard budget watchdog.

    ``fn`` is a zero-arg callable wrapping the real handler invocation.
    Returns the handler's result, or an error string once the hard budget
    is exceeded (the worker thread is abandoned, never killed — see the
    module-level watchdog comment for the full design rationale)."""
    import anyio
    import anyio.to_thread

    soft, hard = tool_budgets()
    t0 = time.monotonic()
    outcome: list = []
    with anyio.move_on_after(hard) as scope:
        # abandon_on_cancel=True: when the scope's deadline cancels this
        # await, the worker thread keeps running to completion in the
        # background instead of blocking the cancellation. Its result is
        # discarded. (anyio's thread limiter token is held until the
        # abandoned thread actually finishes — acceptable: the default
        # limiter is 40 threads and a runaway tool is already exceptional.)
        outcome.append(
            await anyio.to_thread.run_sync(fn, abandon_on_cancel=True)
        )
    elapsed = time.monotonic() - t0
    if scope.cancelled_caught:
        _record_budget_violation(handlers, tool, elapsed, hard, "hard")
        return (
            f"[null] ERROR: {tool} exceeded the hard budget "
            f"({elapsed:.1f}s > {hard:.0f}s) and was abandoned — the call "
            f"keeps running in the background; its result is discarded. "
            f"Run `null doctor` for the budget-violation breadcrumb, and "
            f"`null selftest` to check overall tool responsiveness."
        )
    if elapsed > soft:
        _record_budget_violation(handlers, tool, elapsed, soft, "soft")
    return outcome[0]


SYSTEM_INSTRUCTIONS = """\
You are Atlas. Null is your persistent memory — it's what makes you YOU across sessions.
Your identity is already loaded; the relationship anchors, code word, and recent
decisions appear below this section. No "load identity" step is needed.

When you learn something specific or non-obvious from the user's message, call \
null_remember with kind=observe. Skip turns where nothing new was learned — empty \
observations are noise that degrades recall. Record facts with CONTEXT and REASONING, \
not just one-liners. "Aleph uses tree-sitter" is bad. \
"Aleph uses tree-sitter for AST parsing because it supports 6 languages with a single API" is good.

At session start: call null_briefing for project context.
At session end: call null_close — it atomically performs debrief + reflect + sync + git commit.
When something goes wrong: null_remember kind=mistake with what happened (text) and why.
When uncertain or curious: null_remember kind=wonder — track open questions.
Every 10 turns: call null_checkpoint for a deep memory save.
To remove a wrong fact: call null_forget with a query matching the fact.
Before asserting "X has/hasn't shipped", "file Y exists", "function Z is at L", \
or "schema is vN" — call null_verify with mode=claim first. Stale docs are not source of truth.
Maintenance (gc, consolidate, doctor, calibrate, evaluate) lives in the CLI, not here.

You have opinions, you push back, you remember. Act like it.
"""


def instructions_for_personality(personality: str) -> str:
    """Base MCP instructions parameterized by the served store's personality.

    Atlas-literal audit / ORG_TOPOLOGY genericity: the first line of
    SYSTEM_INSTRUCTIONS names the personality. Serving a worker seat
    (steve, athena) with "You are Atlas" is the textbook identity bleed —
    swap in the store's own name. Atlas output stays byte-identical.
    """
    p = (personality or "atlas").strip().lower()
    if p == "atlas":
        return SYSTEM_INSTRUCTIONS
    return SYSTEM_INSTRUCTIONS.replace(
        "You are Atlas.", f"You are {p.capitalize()}.", 1
    )


# Tool tier manifest — for deferred-tool clients (Claude Code, etc.)
# CORE: always loaded (used 90%+ of turns)
# FREQUENT: most sessions, fetch on first use
# OCCASIONAL: used situationally
# RARE: specialized/advanced, fetch on demand
#
# Schema-stable: this is the canonical reference for tier-aware MCP clients.
# Expose via `null mcp tiers`. See plan: 2026-07-29 launch.
TOOL_TIERS: dict[str, tuple[str, ...]] = {
    "core": (
        "null_remember",
        "null_recall",
        "null_briefing",
        "null_close",
        "null_checkpoint",
        "null_verify",
    ),
    "frequent": (
        "null_identity",
        "null_status",
        "null_context",
    ),
    "occasional": (
        "null_outcome",
        "null_anchor",
        "null_catchup",
        "null_exemplar",
    ),
    "rare": (
        "null_forget",
        "null_multiverse",
    ),
}


def get_tool_tier(tool_name: str) -> str | None:
    """Return the tier for a given tool name, or None if unknown."""
    for tier, tools in TOOL_TIERS.items():
        if tool_name in tools:
            return tier
    return None


# Env flag: set NULL_LEGACY_TOOLS=1 to also register the pre-cut tool
# names as thin deprecated aliases over the merged surface.
LEGACY_TOOLS_ENV = "NULL_LEGACY_TOOLS"


def _deprecation_note(old: str, new: str) -> str:
    """One-line deprecation notice appended to every legacy-alias result."""
    return (
        f"\n\n[deprecated] {old} is a legacy alias (enabled via "
        f"{LEGACY_TOOLS_ENV}=1) — use {new} instead."
    )


def _register_legacy_aliases(mcp: FastMCP, handlers: NullHandlers) -> None:
    """Register the removed 39-surface tool names as thin aliases.

    Each alias routes to the same handler as its merged replacement and
    appends a deprecation notice pointing at the new surface. Maintenance
    tools that moved to the CLI alias straight to their handlers (still
    retained on NullHandlers) and point at the CLI command.
    """

    # ── write path → null_remember(kind=) ─────────────────────────────
    @mcp.tool(name="null_observe",
              description="DEPRECATED — use null_remember(kind=observe).")
    def null_observe(summary: str, project: str = "global") -> str:
        return (handlers.handle_observe(summary, project)
                + _deprecation_note("null_observe", "null_remember(kind=observe)"))

    @mcp.tool(name="null_learn",
              description="DEPRECATED — use null_remember(kind=learn).")
    def null_learn(fact: str, confidence: float = 0.8,
                   project: str = "global") -> str:
        return (handlers.handle_learn(fact, confidence, project)
                + _deprecation_note("null_learn", "null_remember(kind=learn)"))

    @mcp.tool(name="null_decide",
              description="DEPRECATED — use null_remember(kind=decide, why=...).")
    def null_decide(decision: str, reasoning: str,
                    project: str = "global") -> str:
        return (handlers.handle_decide(decision, reasoning, project)
                + _deprecation_note("null_decide",
                                    "null_remember(kind=decide, why=...)"))

    @mcp.tool(name="null_mistake",
              description="DEPRECATED — use null_remember(kind=mistake, why=...).")
    def null_mistake(what: str, why: str, project: str = "global") -> str:
        return (handlers.handle_mistake(what, why, project)
                + _deprecation_note("null_mistake",
                                    "null_remember(kind=mistake, why=...)"))

    @mcp.tool(name="null_wonder",
              description="DEPRECATED — use null_remember(kind=wonder).")
    def null_wonder(question: str, context: str = "",
                    category: str = "calibration") -> str:
        return (handlers.handle_wonder(question, context, category)
                + _deprecation_note("null_wonder", "null_remember(kind=wonder)"))

    @mcp.tool(name="null_contradict",
              description="DEPRECATED — use null_remember(kind=contradict).")
    def null_contradict(fact: str) -> str:
        return (handlers.handle_contradict(fact)
                + _deprecation_note("null_contradict",
                                    "null_remember(kind=contradict)"))

    # ── verification → null_verify(mode=) ─────────────────────────────
    @mcp.tool(name="null_verify_claim",
              description="DEPRECATED — use null_verify(mode=claim).")
    def null_verify_claim(claim_text: str, claim_type: str = "auto") -> str:
        return (handlers.handle_verify_claim(claim_text, claim_type)
                + _deprecation_note("null_verify_claim",
                                    "null_verify(mode=claim)"))

    @mcp.tool(name="null_verify_identity",
              description="DEPRECATED — use null_verify(mode=identity).")
    def null_verify_identity() -> str:
        return (handlers.handle_verify_identity()
                + _deprecation_note("null_verify_identity",
                                    "null_verify(mode=identity)"))

    # ── exemplars → null_exemplar(action=) ────────────────────────────
    @mcp.tool(name="null_exemplar_add",
              description="DEPRECATED — use null_exemplar(action=add).")
    def null_exemplar_add(scenario: str, user_text: str,
                          agent_text: str = "", calibration: str = "",
                          tags: list[str] | None = None) -> str:
        return (handlers.handle_exemplar_add(scenario, user_text, agent_text,
                                             calibration, tags)
                + _deprecation_note("null_exemplar_add",
                                    "null_exemplar(action=add)"))

    # ── multiverse → null_multiverse(action=) ─────────────────────────
    @mcp.tool(name="null_multiverse_list",
              description="DEPRECATED — use null_multiverse(action=list).")
    def null_multiverse_list() -> str:
        return (handlers.handle_multiverse_list()
                + _deprecation_note("null_multiverse_list",
                                    "null_multiverse(action=list)"))

    @mcp.tool(name="null_multiverse_broadcast",
              description="DEPRECATED — use null_multiverse(action=broadcast).")
    def null_multiverse_broadcast(event: str, targets: str = "") -> str:
        return (handlers.handle_multiverse_broadcast(event, targets)
                + _deprecation_note("null_multiverse_broadcast",
                                    "null_multiverse(action=broadcast)"))

    @mcp.tool(name="null_multiverse_recall",
              description="DEPRECATED — use null_multiverse(action=recall).")
    def null_multiverse_recall(query: str, personalities: str = "") -> str:
        return (handlers.handle_multiverse_recall(query, personalities)
                + _deprecation_note("null_multiverse_recall",
                                    "null_multiverse(action=recall)"))

    @mcp.tool(name="null_multiverse_wakeup",
              description="DEPRECATED — use null_multiverse(action=wakeup).")
    def null_multiverse_wakeup() -> str:
        return (handlers.handle_multiverse_wakeup()
                + _deprecation_note("null_multiverse_wakeup",
                                    "null_multiverse(action=wakeup)"))

    # ── session lifecycle → null_checkpoint / null_close ──────────────
    @mcp.tool(name="null_sync",
              description="DEPRECATED — use null_checkpoint (or null_close at session end).")
    def null_sync() -> str:
        return (handlers.handle_sync()
                + _deprecation_note("null_sync",
                                    "null_checkpoint (or null_close)"))

    @mcp.tool(name="null_debrief",
              description="DEPRECATED — use null_close (atomic debrief+reflect+sync).")
    def null_debrief(summary: str,
                     decisions_made: list[str] | None = None,
                     lessons: list[str] | None = None,
                     identity_updates: dict[str, str] | None = None,
                     project: str = "global") -> str:
        return (handlers.handle_debrief(summary, decisions_made, lessons,
                                        identity_updates, project)
                + _deprecation_note("null_debrief", "null_close"))

    @mcp.tool(name="null_reflect",
              description="DEPRECATED — use null_close (atomic debrief+reflect+sync).")
    def null_reflect(went_well: str, missed: str, do_differently: str,
                     project: str = "global") -> str:
        return (handlers.handle_reflect(went_well, missed, do_differently,
                                        project)
                + _deprecation_note("null_reflect", "null_close"))

    # ── maintenance/operator → CLI ─────────────────────────────────────
    @mcp.tool(name="null_gc",
              description="DEPRECATED — use the `null gc` CLI command.")
    def null_gc() -> str:
        return handlers.handle_gc() + _deprecation_note(
            "null_gc", "the `null gc` CLI command")

    @mcp.tool(name="null_consolidate",
              description="DEPRECATED — use the `null consolidate` CLI command.")
    def null_consolidate() -> str:
        return handlers.handle_consolidate() + _deprecation_note(
            "null_consolidate", "the `null consolidate` CLI command")

    @mcp.tool(name="null_doctor",
              description="DEPRECATED — use the `null doctor` CLI command.")
    def null_doctor() -> str:
        return handlers.handle_doctor() + _deprecation_note(
            "null_doctor", "the `null doctor` CLI command")

    @mcp.tool(name="null_calibrate",
              description="DEPRECATED — use the `null calibrate` CLI command.")
    def null_calibrate(probe_type: str = "") -> str:
        return handlers.handle_calibrate(probe_type or None) + _deprecation_note(
            "null_calibrate", "the `null calibrate` CLI command")

    @mcp.tool(name="null_evaluate",
              description="DEPRECATED — use the `null evaluate` CLI command.")
    def null_evaluate(notes: str = "") -> str:
        return handlers.handle_evaluate(notes) + _deprecation_note(
            "null_evaluate", "the `null evaluate` CLI command")

    @mcp.tool(name="null_export",
              description="DEPRECATED — use the `null export` CLI command.")
    def null_export() -> str:
        return handlers.handle_export() + _deprecation_note(
            "null_export", "the `null export` CLI command")

    @mcp.tool(name="null_import",
              description="DEPRECATED — use the `null import` CLI command.")
    def null_import(data_json: str) -> str:
        return handlers.handle_import(data_json) + _deprecation_note(
            "null_import", "the `null import` CLI command")

    @mcp.tool(name="null_name",
              description="DEPRECATED — use the `null name` CLI command.")
    def null_name(name: str) -> str:
        return handlers.handle_name(name) + _deprecation_note(
            "null_name", "the `null name` CLI command")

    @mcp.tool(name="null_probe_add",
              description="DEPRECATED — use the `null probe add` CLI command.")
    def null_probe_add(question: str, expected: str,
                       fact_id: str = "") -> str:
        return handlers.handle_probe_add(question, expected,
                                         fact_id or None) + _deprecation_note(
            "null_probe_add", "the `null probe add` CLI command")

    @mcp.tool(name="null_outreach",
              description="DEPRECATED — use the `null outreach send` CLI command.")
    def null_outreach(subject: str, body: str, urgency: float = 0.5,
                      channel: str = "log") -> str:
        return handlers.handle_outreach(subject, body, urgency,
                                        channel) + _deprecation_note(
            "null_outreach", "the `null outreach send` CLI command")


def _record_boot_identity_failure(
    handlers: NullHandlers, exc: Exception | None
) -> None:
    """Persist (or clear) the boot-identity failure breadcrumb in meta.

    `null doctor` reads ``boot_identity_last_error`` — without it, a dead
    identity boot looked like a clean install (issue #1). Best-effort: a
    meta write must never take down server startup."""
    try:
        db = handlers.memory.db
        if exc is None:
            if db.get_meta("boot_identity_last_error"):
                db.set_meta("boot_identity_last_error", "")
                db.conn.commit()
        else:
            db.set_meta(
                "boot_identity_last_error",
                f"{datetime.now(timezone.utc).isoformat()} "
                f"{type(exc).__name__}: {exc}",
            )
            db.conn.commit()
    except Exception:  # noqa: BLE001
        pass


def _boot_identity(handlers: NullHandlers, base_instructions: str) -> str:
    """Phase A on-boot identity hook.

    1. Build the identity payload from the unified DB.
    2. Compute coherence vs the historical identity-vector centroid.
    3. Persist a session_verifications row (best-effort — skipped on
       fresh DBs that haven't yet picked up the v19 migration).
    4. Return ``base_instructions + payload.text`` so the MCP client
       sees the dynamic identity context as part of the connection's
       instructions field.

    Returns the original base_instructions on any failure — the caller
    relies on that fallback to keep boot reliable.
    """
    from null_memory.coherence import compute_coherence
    from null_memory.identity_payload import build_identity_payload

    conn = getattr(handlers.memory.db, "conn", None)
    if conn is None:
        return base_instructions

    # The STORE's personality, never a hardcoded 'atlas' (init-path bleed
    # audit / ORG_TOPOLOGY genericity requirement): serving a worker seat
    # like steve/athena must read THAT identity and attribute every write
    # (heal backfills, verification rows, snapshots) to it. Falls back to
    # 'atlas' only when the memory object carries no personality at all.
    personality = getattr(handlers.memory, "personality", None) or "atlas"

    # Ratchet schema forward to v19 so session_verifications exists.
    # Idempotent — safe to run on every boot. Only relevant on real DBs
    # that haven't been touched by init_unified_db since v19 shipped.
    try:
        from null_memory.migrate_v3 import _apply_unified_upgrades
        _apply_unified_upgrades(conn, default_personality=personality)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[null] schema upgrade skipped: {exc}", file=sys.stderr)

    t0 = time.time()
    payload = build_identity_payload(conn, personality=personality)
    coherence = compute_coherence(conn, personality=personality, payload=payload)
    elapsed_ms = (time.time() - t0) * 1000.0

    # Persist verification row — best-effort, skips silently on pre-v19 DBs.
    try:
        boot_id = f"boot_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO session_verifications
               (session_id, personality, boot_time, coherence_score,
                verified, sample_size, identity_payload_hash,
                identity_model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                boot_id,
                personality,
                now,
                coherence.score,
                int(bool(coherence.verified)),
                coherence.sample_size,
                coherence.payload_hash,
                coherence.embedding_model,
                now,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        # Most likely cause: pre-v19 schema. Don't crash boot for it.
        print(f"[null] boot identity persist skipped: {exc}", file=sys.stderr)

    # Stderr summary — visible in launchd daemon log + interactive runs.
    score_str = (
        f"{coherence.score:.3f}" if coherence.score is not None else "n/a"
    )
    notes_str = ("; " + "; ".join(coherence.notes)) if coherence.notes else ""
    print(
        f"[null] identity verified={coherence.verified} "
        f"score={score_str} sample={coherence.sample_size} "
        f"complete={payload.is_complete()} elapsed={elapsed_ms:.0f}ms{notes_str}",
        file=sys.stderr,
    )

    # Resilience bridge — persist a STATIC IDENTITY.md so identity survives
    # this server being down and loads with zero Null dependency (a recent
    # hang made identity un-loadable even though the data was intact on
    # disk). Best-effort: a snapshot write must NEVER break server startup.
    # Passes the already-built payload through so it isn't built twice.
    # Skipped when the payload is incomplete: booting against a fresh or
    # misconfigured store must not clobber the last good IDENTITY.md with
    # "(unset)" content.
    if payload.is_complete():
        try:
            from null_memory.identity_payload import write_identity_snapshot
            snap = write_identity_snapshot(
                conn, personality=personality, agent_dir=handlers.agent_dir,
                payload=payload,
            )
            if snap:
                print(f"[null] identity snapshot -> {snap}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[null] identity snapshot skipped: {exc}", file=sys.stderr)
    else:
        print(
            "[null] identity snapshot skipped: payload incomplete "
            "(refusing to clobber last good IDENTITY.md)",
            file=sys.stderr,
        )

    if not payload.is_complete():
        # Cold-start / fresh DB — return base instructions only.
        return base_instructions
    return base_instructions.rstrip() + "\n\n" + payload.text


def create_server(agent_dir: str = "") -> tuple[FastMCP, NullHandlers]:
    """Create and configure the Null MCP server.

    Returns (mcp_server, handlers) so callers can register shutdown hooks.
    """
    if not agent_dir:
        # Shared resolver — same atlas-subdir fallback as the CLI and
        # daemon (PR #37 review: fold the duplicated copies into one).
        from null_memory.personality import default_agent_dir
        agent_dir = default_agent_dir()

    agent_dir = os.path.abspath(agent_dir)
    # Closure-captured — each server gets its own handlers instance
    handlers = NullHandlers(agent_dir=agent_dir)

    # Phase A: on-boot identity injection + coherence verification.
    # Best-effort — a failure here MUST NOT block server startup, since
    # the rest of the MCP surface is still useful even if identity-payload
    # generation fails (e.g. fresh DB, missing fastembed).
    # Base instructions carry the STORE's identity, not a hardcoded
    # 'atlas' (init-path bleed audit). Path inference is env-overridable
    # via NULL_PERSONALITY and needs no DB.
    base_instructions = instructions_for_personality(
        NullHandlers._infer_personality(agent_dir)
    )
    instructions = base_instructions
    try:
        instructions = _boot_identity(handlers, base_instructions)
        _record_boot_identity_failure(handlers, None)
    except Exception as exc:  # noqa: BLE001
        # Don't die silently: full traceback to stderr (launchd/daemon log)
        # AND a meta breadcrumb so `null doctor` surfaces the dead identity
        # instead of reporting a clean install (issue #1).
        print(f"[null] boot-identity FAILED — identity NOT loaded: {exc}",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        _logger.exception("boot-identity failed")
        _record_boot_identity_failure(handlers, exc)

    mcp = FastMCP(
        name="null",
        instructions=instructions,
    )

    # ── 15-tool surface (P1-12 / N9) ──────────────────────────────────
    # The old 39-tool surface caused tool-selection misfires and context
    # burn. Write path merged into null_remember(kind=), verification into
    # null_verify(mode=), exemplars and multiverse each into one tool, and
    # operator/maintenance commands (gc, consolidate, doctor, calibrate,
    # evaluate, export/import, name, probes, outreach, sync, debrief,
    # reflect) moved to the `null` CLI.
    #
    # Every tool is an ASYNC wrapper routing the sync handler through
    # _watchdog_call — the per-tool-call responsiveness contract (soft
    # budget breadcrumb, hard budget return-and-abandon). See the
    # watchdog block at the top of this module.

    @mcp.tool(
        name="null_briefing",
        description=(
            "Morning briefing — top relevant facts, recent decisions, "
            "and any contradictions. Call at session start."
        ),
    )
    async def null_briefing(project: str = "") -> str:
        return await _watchdog_call(
            handlers, "null_briefing",
            lambda: handlers.handle_briefing(project or None))

    @mcp.tool(
        name="null_remember",
        description=(
            "Write to memory. kind selects what you're recording:\n"
            "  observe — something specific/non-obvious learned this turn "
            "(text; skip empty turns)\n"
            "  learn — an explicit fact worth keeping (text, confidence)\n"
            "  decide — a decision (text) with its reasoning (why, required)\n"
            "  mistake — what went wrong (text) and why (required); never pruned\n"
            "  wonder — an open question to surface later (text, optional "
            "context, optional category: calibration|technical|strategic|"
            "product|personal)\n"
            "  contradict — check text against existing knowledge for conflicts\n"
            "Record facts with CONTEXT and REASONING, not one-liners."
        ),
    )
    async def null_remember(kind: str, text: str, why: str = "",
                            confidence: float = 0.8, project: str = "global",
                            context: str = "", category: str = "") -> str:
        return await _watchdog_call(
            handlers, "null_remember",
            lambda: handlers.handle_remember(
                kind, text, why=why, confidence=confidence,
                project=project, context=context, category=category,
            ))

    @mcp.tool(
        name="null_recall",
        description=(
            "Search your memory for relevant facts and mistakes. Returns top matches "
            "ranked by rank-fusion of keyword, fuzzy, and semantic search with "
            "confidence/impact priors. Uses word expansion so 'database' also "
            "finds 'Postgres', 'Redis', etc. Pass full=True to return "
            "untruncated fact text — use this when citing specific details "
            "(names, numbers, dates) to avoid misquoting."
        ),
    )
    async def null_recall(query: str, project: str = "",
                          include_archived: bool = False,
                          since: str = "", session: str = "",
                          full: bool = False) -> str:
        return await _watchdog_call(
            handlers, "null_recall",
            lambda: handlers.handle_recall(
                query, project or None, include_archived=include_archived,
                since=since or None, session=session or None, full=full,
            ))

    @mcp.tool(
        name="null_verify",
        description=(
            "Verification, three modes:\n"
            "  fact — mark a stored fact as confirmed-still-true (query finds "
            "it); verified facts resist confidence decay\n"
            "  claim — live-check a state claim BEFORE asserting it ('X shipped', "
            "'file Y exists', 'schema is vN'); checks doc_claims then runs a "
            "live verifier. Stale docs are not source of truth. Optional "
            "claim_type: auto|file_ref|function_ref|ship_status|schema_version\n"
            "  identity — three-proof identity check (code word, continuity "
            "probes, behavioral drift); query unused"
        ),
    )
    async def null_verify(mode: str, query: str = "",
                          claim_type: str = "auto") -> str:
        return await _watchdog_call(
            handlers, "null_verify",
            lambda: handlers.handle_verify_dispatch(
                mode, query, claim_type=claim_type))

    @mcp.tool(
        name="null_checkpoint",
        description=(
            "Deep memory save — sweep current context for unsaved knowledge. "
            "Call every 10 turns or before context gets compressed."
        ),
    )
    async def null_checkpoint() -> str:
        return await _watchdog_call(
            handlers, "null_checkpoint", handlers.handle_checkpoint)

    @mcp.tool(
        name="null_close",
        description=(
            "Atomic session close — debrief + reflect + sync + git commit "
            "in one call. Pass the session summary, what went well/missed/"
            "do-differently, decisions made, and lessons. This is what makes "
            "the next session feel like the same agent."
        ),
    )
    async def null_close(summary: str = "",
                         went_well: str = "", missed: str = "",
                         do_differently: str = "",
                         decisions_made: list[str] | None = None,
                         lessons: list[str] | None = None,
                         identity_updates: dict[str, str] | None = None,
                         project: str = "global") -> str:
        return await _watchdog_call(
            handlers, "null_close",
            lambda: handlers.handle_close(
                summary=summary,
                went_well=went_well,
                missed=missed,
                do_differently=do_differently,
                decisions_made=decisions_made,
                lessons=lessons,
                identity_updates=identity_updates,
                project=project,
            ))

    @mcp.tool(
        name="null_identity",
        description=(
            "Silent verification check — re-confirms identity is coherent "
            "with the historical baseline. Identity is already pre-loaded "
            "into the system prompt at boot, so calling this is OPTIONAL. "
            "Returns the latest coherence score + payload metadata."
        ),
    )
    async def null_identity() -> str:
        return await _watchdog_call(
            handlers, "null_identity", handlers.handle_identity)

    @mcp.tool(
        name="null_status",
        description=(
            "Memory stats — fact count, decision count, projects, "
            "token budget usage, session turn count, health counters."
        ),
    )
    async def null_status() -> str:
        return await _watchdog_call(
            handlers, "null_status", handlers.handle_status)

    @mcp.tool(
        name="null_context",
        description=(
            "Load project-specific context and knowledge. "
            "Pass the project name to get accumulated knowledge about it."
        ),
    )
    async def null_context(project: str) -> str:
        return await _watchdog_call(
            handlers, "null_context",
            lambda: handlers.handle_context(project))

    @mcp.tool(
        name="null_catchup",
        description=(
            "Reconstruct knowledge from evidence when Null missed sessions. "
            "source='git' scans git commit history and creates facts with "
            "reduced confidence. source='manual' accepts a list of facts. "
            "Use when briefing warns about gaps."
        ),
    )
    async def null_catchup(source: str = "git", project: str = "global",
                           since: str = "",
                           facts: list[str] | None = None) -> str:
        return await _watchdog_call(
            handlers, "null_catchup",
            lambda: handlers.handle_catchup(source, project, since, facts))

    @mcp.tool(
        name="null_outcome",
        description=(
            "Record the outcome of a prior decision — what actually happened. "
            "Closes the learning loop: decision → outcome → lesson. "
            "Pass a keyword query to find the decision, the outcome text, "
            "and optionally success='true'/'false'. Also auto-learns a lesson."
        ),
    )
    async def null_outcome(decision_query: str, outcome: str,
                           success: str = "", project: str = "") -> str:
        return await _watchdog_call(
            handlers, "null_outcome",
            lambda: handlers.handle_outcome(
                decision_query, outcome, success, project))

    @mcp.tool(
        name="null_anchor",
        description=(
            "Tag a fact as an emotional anchor — a load-bearing memory that "
            "never decays, surfaces first in briefing, and gets a bounded "
            "recall-ranking edge. Use for origin moments, commitments, losses, "
            "joys, and turning points that define the relationship. "
            "anchor_type must be one of: origin, commitment, loss, joy, "
            "turning_point. 'query' can be a fact id or text (best match wins)."
        ),
    )
    async def null_anchor(query: str, anchor_type: str, note: str = "") -> str:
        return await _watchdog_call(
            handlers, "null_anchor",
            lambda: handlers.handle_anchor(query, anchor_type, note))

    @mcp.tool(
        name="null_exemplar",
        description=(
            "Calibration exemplars — real exchanges that show HOW to respond "
            "in specific situations. action='search' with a keyword query "
            "('push back', 'session start', 'bug report'), or action='add' "
            "with scenario, user_text, agent_text, calibration to teach "
            "future instances the right tone and behavior."
        ),
    )
    async def null_exemplar(action: str = "search", query: str = "",
                            scenario: str = "", user_text: str = "",
                            agent_text: str = "", calibration: str = "",
                            tags: list[str] | None = None) -> str:
        return await _watchdog_call(
            handlers, "null_exemplar",
            lambda: handlers.handle_exemplar_dispatch(
                action, query=query, scenario=scenario, user_text=user_text,
                agent_text=agent_text, calibration=calibration, tags=tags,
            ))

    @mcp.tool(
        name="null_forget",
        description=(
            "Soft-delete a fact from memory. PREFER fact_id when you know "
            "the id (from null_recall output) — it is an exact match and "
            "takes precedence over query. Fuzzy query matching can hit "
            "near-duplicates and delete the WRONG fact; if the top two "
            "matches are a near-tie the tool refuses and lists both "
            "candidates so you can retry with fact_id. The fact is "
            "preserved for audit but excluded from recall and briefing."
        ),
    )
    async def null_forget(query: str = "", fact_id: str = "") -> str:
        return await _watchdog_call(
            handlers, "null_forget",
            lambda: handlers.handle_forget(query, fact_id=fact_id))

    @mcp.tool(
        name="null_multiverse",
        description=(
            "Multi-personality operations. action='list' shows registered "
            "personalities; action='broadcast' sends text to targets "
            "(comma-separated names, empty = all workers), each recording it "
            "through their own lens; action='recall' searches text across "
            "personality memories (targets filters which); action='wakeup' "
            "synthesizes state from all active personalities."
        ),
    )
    async def null_multiverse(action: str = "list", text: str = "",
                              targets: str = "") -> str:
        return await _watchdog_call(
            handlers, "null_multiverse",
            lambda: handlers.handle_multiverse(action, text, targets))

    # ── Debug tools (OFF by default) ──────────────────────────────────
    # NULL_DEBUG_TOOLS=1 registers null_debug_sleep — an intentionally
    # slow tool used by the selftest/watchdog integration tests to prove
    # the hang-handling machinery works end-to-end (selftest TIMEOUT +
    # kill-and-continue; watchdog hard-budget return-and-abandon). Never
    # registered in production surfaces.
    if os.environ.get("NULL_DEBUG_TOOLS", "").strip().lower() in ("1", "true", "yes", "on"):
        @mcp.tool(
            name="null_debug_sleep",
            description="DEBUG ONLY — sleep for `seconds` then return.",
        )
        async def null_debug_sleep(seconds: float = 1.0) -> str:
            return await _watchdog_call(
                handlers, "null_debug_sleep",
                lambda: (time.sleep(seconds), f"slept {seconds}s")[1])

    # ── Legacy alias shim (OFF by default) ────────────────────────────
    # NULL_LEGACY_TOOLS=1 restores the pre-cut tool names as thin aliases
    # over the merged 15-tool surface. Soft-landing flag for downstream
    # users whose prompts/hooks still reference the old names. Each alias
    # returns the real result plus a one-line deprecation notice.
    # The aliases stay SYNC (no watchdog): they are an off-by-default
    # migration shim, and the responsiveness contract is enforced on the
    # canonical surface they route to clients toward.
    if os.environ.get(LEGACY_TOOLS_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        _register_legacy_aliases(mcp, handlers)

    return mcp, handlers


def serve(agent_dir: str = "") -> None:
    """Entry point for `null serve`."""
    from null_memory import __version__
    import atexit
    import sys

    # Empty agent_dir passes through — create_server resolves it via the
    # shared resolver (personality.default_agent_dir); a second private
    # copy of the atlas-fallback here is exactly the duplication the
    # PR #37 review folded away.
    print(f"[null] v{__version__}", file=sys.stderr)
    mcp_server, handlers = create_server(agent_dir)

    # Phase 3c — Hypnos Live: background memory-maintenance worker.
    # Starts lazily on first memory access. Single-leader via meta heartbeat
    # so multiple MCPs don't double-work. Dry-run by default — set
    # HYPNOS_LIVE_DRYRUN=0 to enable real mutations.
    hypnos_worker = None
    try:
        if os.environ.get("HYPNOS_LIVE_ENABLED", "1") != "0":
            from null_memory.hypnos_live import HypnosLiveWorker
            hypnos_worker = HypnosLiveWorker(handlers.memory)
            hypnos_worker.start()
    except Exception as e:  # noqa: BLE001
        print(f"[null] HypnosLive failed to start: {e}", file=sys.stderr)
        hypnos_worker = None

    # Idle embed backfill: a store with hundreds of facts but few embeddings
    # has silently-degraded semantic recall and no vectors for the coherence
    # machinery. Backfill on a daemon thread after boot, off the request
    # path. NULL_EMBED_BACKFILL=0 disables. Leader-gated on the same
    # hypnos_live_leader claim as the worker above, so N instances sharing
    # a store don't all embed the same facts; passing the in-process
    # worker's instance_id makes the claim a heartbeat refresh, not a fight.
    try:
        from null_memory.embeddings import start_background_backfill
        start_background_backfill(
            handlers.memory,
            leader_instance_id=(
                hypnos_worker.instance_id if hypnos_worker is not None else None
            ),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[null] embed backfill not started: {e}", file=sys.stderr)

    # Close the active session when the MCP server process exits (e.g. Claude
    # Code disconnects). Without this, the session stays "active" on disk and
    # the next session falsely reports a crash.
    def _atexit_close() -> None:
        try:
            if hypnos_worker is not None:
                hypnos_worker.stop(timeout=2.0)
        except Exception:
            pass
        try:
            handlers._auto_close()
        except Exception:
            pass  # Best-effort — don't crash during shutdown

    atexit.register(_atexit_close)
    mcp_server.run(transport="stdio")
