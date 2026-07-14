"""Null CLI — persistent agent memory."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from typing import Any


def _run_maybe_async(value: Any) -> Any:
    """Resolve a manager/reasoner return value that may be awaitable."""
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def _ensure_utf8_stdio() -> None:
    """Make CLI output safe on non-UTF-8 stdio (Windows cp1252 pipes/consoles).

    Null's output uses glyphs outside cp1252 (✓ ⚠ ▶ →). On Windows,
    stdout/stderr default to the locale code page — printing those glyphs
    raises UnicodeEncodeError and turns every affected command into rc=1
    (the cause of the Windows CLI test failures in issue #2). Reconfigure
    to UTF-8 with errors="replace" so output degrades gracefully instead
    of crashing. No-op where stdio is already UTF-8 (macOS/Linux).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (stream.encoding or "").lower().replace("-", "").replace("_", "")
            if enc != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # exotic stream (no .reconfigure / closed) — leave as-is


def main() -> None:
    _ensure_utf8_stdio()
    from null_memory import __version__

    parser = argparse.ArgumentParser(prog="null", description="Null — persistent agent memory")
    parser.add_argument("--version", "-V", action="version", version=f"null {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start Null MCP server")
    serve_parser.add_argument("agent_dir", nargs="?", default="", help="Agent memory directory (default: ~/.null)")

    # status
    subparsers.add_parser("status", help="Show memory status")

    # identity-payload — print the on-boot identity payload (called by
    # SessionStart hook so the payload reaches the model's system prompt;
    # the MCP server's `instructions` field doesn't surface to Claude).
    ip_parser = subparsers.add_parser(
        "identity-payload",
        help="Print the on-boot identity payload (for SessionStart hooks)",
    )
    ip_parser.add_argument(
        "--personality", default="atlas",
        help="Personality to build payload for (default: atlas)",
    )

    # sync-anchors — resilience bridge: persist identity as a STATIC
    # IDENTITY.md so it survives the Null MCP server being down and loads
    # with zero Null dependency.
    sa_parser = subparsers.add_parser(
        "sync-anchors",
        help="Write a static IDENTITY.md snapshot (loadable with no running Null)",
    )
    sa_parser.add_argument(
        "--out", default=None,
        help="Output path (default: <agent_dir>/IDENTITY.md)",
    )
    sa_parser.add_argument(
        "--personality", default="atlas",
        help="Personality to snapshot (default: atlas)",
    )

    # export
    export_parser = subparsers.add_parser("export", help="Export memory as JSON")
    export_parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    export_parser.add_argument(
        "--project", action="append", dest="projects", metavar="NAME",
        help="Scope export to this project (repeatable). Scoped exports "
             "exclude identity content by default.")
    export_parser.add_argument(
        "--kinds", help="Comma-separated entity kinds to export: "
                        "fact,decision,mistake,reflection")
    export_parser.add_argument("--since", help="Only entities created since "
                                               "(ISO date, '7d', 'yesterday', ...)")
    identity_group = export_parser.add_mutually_exclusive_group()
    identity_group.add_argument(
        "--no-identity", action="store_true",
        help="Exclude identity content (anchors, code word, identity dict). "
             "DEFAULT for scoped exports; pass explicitly to strip identity "
             "from an otherwise-unfiltered export.")
    identity_group.add_argument(
        "--include-identity", action="store_true",
        help="Re-include identity content in a scoped export. The code word "
             "is a secret — a loud warning is printed if it is present.")
    export_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print counts per kind/project without writing anything")

    # import
    import_parser = subparsers.add_parser("import", help="Import memory from JSON")
    import_parser.add_argument("file", help="JSON file to import")

    # learn
    learn_parser = subparsers.add_parser("learn", help="Store a fact in memory")
    learn_parser.add_argument("fact", help="The fact to remember")
    learn_parser.add_argument("--confidence", "-c", type=float, default=0.8, help="Confidence 0-1 (default: 0.8)")
    learn_parser.add_argument("--project", default="global", help="Project name")
    learn_parser.add_argument("--impact", type=float, default=0.5, help="Salience 0-1 (default: 0.5). 0.9+=identity-critical, 0.7-0.8=key facts, 0.5-0.6=context, 0.1-0.4=ephemeral")
    learn_parser.add_argument("--source", default="explicit", choices=["witnessed", "explicit", "observation", "told"], help="Source authority tier (default: explicit)")

    # recall
    recall_parser = subparsers.add_parser("recall", help="Search memory for relevant facts")
    recall_parser.add_argument("query", help="Search query")
    recall_parser.add_argument("--project", default="", help="Filter by project")
    recall_parser.add_argument("--limit", "-n", type=int, default=10, help="Max results (default: 10)")
    recall_parser.add_argument("--include-archived", action="store_true", help="Also search archived facts")

    # name
    name_parser = subparsers.add_parser("name", help="Set agent name")
    name_parser.add_argument("name", help="Name for this agent")

    # mistake
    mistake_parser = subparsers.add_parser("mistake", help="Record a mistake")
    mistake_parser.add_argument("what", help="What went wrong")
    mistake_parser.add_argument("why", help="Why it went wrong")
    mistake_parser.add_argument("--project", default="global", help="Project name")

    # reflect
    reflect_parser = subparsers.add_parser("reflect", help="Record a session reflection")
    reflect_parser.add_argument("went_well", help="What went well")
    reflect_parser.add_argument("missed", help="What was missed")
    reflect_parser.add_argument("do_differently", help="What to do differently")
    reflect_parser.add_argument("--project", default="global", help="Project name")

    # gc
    gc_parser = subparsers.add_parser("gc", help="Garbage collect old knowledge")

    subparsers.add_parser("consolidate",
                          help="Merge similar facts, strengthen accessed ones, fade stale ones")

    calibrate_parser = subparsers.add_parser(
        "calibrate", help="Run calibration probes and report a score")
    calibrate_parser.add_argument("--type", dest="probe_type", default="",
                                  help="Restrict to one probe type")

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="Comprehensive health/performance evaluation (stored snapshot)")
    evaluate_parser.add_argument("--notes", default="", help="Notes to store with the snapshot")
    gc_parser.add_argument("--max-facts", type=int, default=None, help="Max facts to keep (default: 5000)")

    # observe (for hook integration)
    observe_parser = subparsers.add_parser("observe", help="Record an observation (for hooks/automation)")
    observe_parser.add_argument("summary", help="What happened")
    observe_parser.add_argument("--project", default="global", help="Project name")
    observe_parser.add_argument("--impact", type=float, default=0.5, help="Salience 0-1 (default: 0.5). 0.9+=identity-critical, 0.7-0.8=key facts, 0.5-0.6=context, 0.1-0.4=ephemeral")
    observe_parser.add_argument("--source", default="observation", choices=["witnessed", "explicit", "observation", "told"], help="Source authority tier (default: observation)")

    # decide
    decide_parser = subparsers.add_parser("decide", help="Log a decision")
    decide_parser.add_argument("decision", help="The decision")
    decide_parser.add_argument("--reasoning", "-r", default="recorded via CLI", help="Reasoning")
    decide_parser.add_argument("--project", default="global", help="Project name")

    # state
    state_parser = subparsers.add_parser("state", help="Show or set felt state")
    state_sub = state_parser.add_subparsers(dest="state_action")
    state_set_parser = state_sub.add_parser("set", help="Set felt state (omit args for interactive)")
    state_set_parser.add_argument("--assessment", default="", help="First-person assessment")
    state_set_parser.add_argument("--energy", choices=["high", "medium", "low"], default="", help="Energy level")
    state_set_parser.add_argument("--concern", action="append", default=[], dest="concerns", help="A concern (repeatable)")
    state_set_parser.add_argument("--optimistic", action="append", default=[], dest="optimistic_about", help="Something optimistic (repeatable)")
    state_set_parser.add_argument("--unresolved", default="", help="One key unresolved thing")

    # momentum
    momentum_parser = subparsers.add_parser("momentum", help="Show or set in-progress state")
    momentum_sub = momentum_parser.add_subparsers(dest="momentum_action")
    momentum_set_parser = momentum_sub.add_parser("set", help="Set momentum (omit args for interactive)")
    momentum_set_parser.add_argument("--project", default="", help="Active project name")
    momentum_set_parser.add_argument("--decision", default="", help="Last significant decision")
    momentum_set_parser.add_argument("--next", default="", dest="next_action", help="Next concrete action")
    momentum_set_parser.add_argument("--blocked", default="", dest="blocked_on", help="What's blocking progress")
    momentum_set_parser.add_argument("--summary", default="", dest="session_summary", help="Session summary paragraph")

    # watch
    watch_parser = subparsers.add_parser("watch", help="Manage standing watches")
    watch_sub = watch_parser.add_subparsers(dest="watch_action")
    watch_sub.add_parser("list", help="List all watches")
    watch_add_parser = watch_sub.add_parser("add", help="Add a new watch")
    watch_add_parser.add_argument("--name", required=True, help="Human-readable name")
    watch_add_parser.add_argument("--cmd", required=True, help="Shell command to run")
    watch_add_parser.add_argument("--interval", type=float, default=4.0, help="Check interval in hours (default: 4)")
    watch_add_parser.add_argument("--alert-if", default="", dest="alert_if", help="Alert condition description")
    watch_run_parser = watch_sub.add_parser("run", help="Run all due watches")
    watch_remove_parser = watch_sub.add_parser("remove", help="Deactivate a watch")
    watch_remove_parser.add_argument("id", help="Watch ID or prefix")

    # simmer
    simmer_parser = subparsers.add_parser("simmer", help="Back burner — open questions and incubating ideas")
    simmer_sub = simmer_parser.add_subparsers(dest="simmer_action")
    simmer_add_parser = simmer_sub.add_parser("add", help="Add a new simmering question")
    simmer_add_parser.add_argument("question", help="The open question or tension")
    simmer_add_parser.add_argument("--context", "-c", default="", help="Why this matters / what triggered it")
    simmer_add_parser.add_argument("--category", default="technical",
                                   choices=["technical", "strategic", "product", "personal"],
                                   help="Category (default: technical)")
    simmer_resolve_parser = simmer_sub.add_parser("resolve", help="Mark a simmering item resolved")
    simmer_resolve_parser.add_argument("id", help="Item ID or prefix")
    simmer_resolve_parser.add_argument("--resolution", "-r", required=True, help="How it resolved")
    simmer_touch_parser = simmer_sub.add_parser("touch", help="Update last_surfaced timestamp")
    simmer_touch_parser.add_argument("id", help="Item ID or prefix")

    # forget
    forget_parser = subparsers.add_parser("forget", help="Soft-delete a fact from memory")
    forget_parser.add_argument("query", nargs="?",
                               help="Query to match the fact to forget (fuzzy)")
    forget_parser.add_argument("--id", dest="fact_id", metavar="FACT_ID",
                               help="Exact fact id to forget — no fuzzy fallback")

    # probe — user-defined calibration probes (CLI home for the surface
    # that left the MCP server in the 39→15 tool cut)
    probe_parser = subparsers.add_parser(
        "probe", help="Manage calibration probes")
    probe_sub = probe_parser.add_subparsers(dest="probe_cmd")
    probe_add = probe_sub.add_parser(
        "add", help="Add a user-defined calibration probe")
    probe_add.add_argument("question", help="Probe question (a recall query)")
    probe_add.add_argument("expected", help="Expected text in the top recall results")
    probe_add.add_argument("--category", default="user",
                           help="Probe category/type (default: user)")
    probe_add.add_argument("--fact-id", default=None, dest="fact_id",
                           help="Optional fact id this probe pins")

    # events (event-sourced sync Phase A, issue #20)
    events_parser = subparsers.add_parser(
        "events", help="Event log — event-sourced sync (NULL_EVENT_LOG=1)")
    events_sub = events_parser.add_subparsers(dest="events_cmd")
    # exchange (event-sourced sync Phase B, issue #20 — org exchange)
    exchange_parser = subparsers.add_parser(
        "exchange",
        help="Org exchange — typed messages between seats (docs/EXCHANGE.md)")
    exchange_sub = exchange_parser.add_subparsers(dest="exchange_cmd")
    ex_post = exchange_sub.add_parser(
        "post", help="Append an event to your outbound stream, commit, push")
    ex_post.add_argument("--kind", required=True,
                         help="report.session | repo.push | broadcast | "
                              "claim.acquire | claim.release | query.ask | "
                              "query.answer | directive")
    ex_post_data = ex_post.add_mutually_exclusive_group(required=True)
    ex_post_data.add_argument("--data",
                              help="JSON object payload, e.g. "
                                   "'{\"summary\": \"...\", \"project\": \"x\"}'")
    ex_post_data.add_argument("--data-file",
                              help="Path to a UTF-8 file containing the JSON "
                                   "object payload ('-' reads stdin). Use on "
                                   "shells that mangle inline JSON quoting "
                                   "(e.g. Windows PowerShell).")
    ex_post.add_argument("--scope", default="org",
                         help="Event scope (default: org)")
    ex_announce = exchange_sub.add_parser(
        "announce-push",
        help="Post repo.push for the cwd git repo (HEAD/branch/remote)")
    ex_announce.add_argument("--summary", default="",
                             help="One-line description of what was pushed")
    ex_announce.add_argument("--repo-dir", default=".",
                             help="Path to the code repo (default: cwd)")
    exchange_sub.add_parser(
        "sync", help="Fetch the exchange and ingest subscribed streams now")
    exchange_sub.add_parser(
        "status", help="Show exchange config, claims, pushes, queries")

    # attend — the attention loop tick (the /loop layer). Quiet by default.
    attend_parser = subparsers.add_parser(
        "attend",
        help="[experimental] Surface exchange messages from other seats not "
             "yet seen by the conversational layer (run from /loop — quiet "
             "when idle)")
    attend_parser.add_argument(
        "--verbose", action="store_true",
        help="Announce 'nothing new' too (default: silent when idle)")
    attend_parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Show messages without advancing the attended cursor")
    attend_parser.add_argument(
        "--limit", type=int, default=0,
        help="Cap the number of messages surfaced (0 = no cap)")

    events_genesis = events_sub.add_parser(
        "genesis",
        help="Export current db state as add-events (genesis snapshot)")
    events_genesis.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing genesis file for this writer")

    # doctor
    doctor_parser = subparsers.add_parser("doctor", help="Run memory health diagnostics")
    doctor_parser.add_argument("--fix", action="store_true", help="Auto-fix detected issues")
    doctor_parser.add_argument("--trace", metavar="QUERY",
                               help="Trace a recall query: dump ranked results with scores + anchor hits for debugging")

    # embed-all
    embed_parser = subparsers.add_parser("embed-all", help="Batch-embed all facts for semantic search")
    embed_parser.add_argument("--force", action="store_true", help="Re-embed all facts (skip existing by default)")

    # outreach
    out_parser = subparsers.add_parser(
        "outreach",
        help="Atlas-initiated contact — triggers, history, controls",
    )
    out_sub = out_parser.add_subparsers(dest="out_cmd")
    out_sub.add_parser("status",   help="Show triggers, pause state, budget")
    out_sub.add_parser("recent",   help="Show recent outreaches")
    out_sub.add_parser("evaluate", help="Run trigger evaluation once")
    out_seed = out_sub.add_parser("seed", help="Install default triggers (all DISABLED)")
    out_seed.add_argument("--enable", action="store_true", help="Enable triggers on install")
    out_test = out_sub.add_parser("test", help="Force-fire a trigger (bypasses cooldown)")
    out_test.add_argument("name", help="Trigger name")
    out_enable = out_sub.add_parser("enable", help="Enable a trigger")
    out_enable.add_argument("name")
    out_disable = out_sub.add_parser("disable", help="Disable a trigger")
    out_disable.add_argument("name")
    out_send = out_sub.add_parser(
        "send", help="Manually emit an outreach (CLI home for the cut null_outreach tool)")
    out_send.add_argument("subject", help="Outreach subject line")
    out_send.add_argument("body", help="Outreach body text")
    out_send.add_argument("--urgency", type=float, default=0.5,
                          help="Urgency 0-1 (default: 0.5)")
    out_send.add_argument("--channel", default="log",
                          choices=["log", "macos", "both"],
                          help="Delivery channel (default: log)")
    out_sub.add_parser("pause",    help="Pause all outreach evaluation")
    out_sub.add_parser("resume",   help="Resume outreach evaluation")
    out_sub.add_parser("log",      help="Tail the outreaches log file")
    out_digest = out_sub.add_parser("digest",
        help="One-screen summary: fires, top-by-urgency, ack/unack, kind caps, eligible-but-disabled")
    out_digest.add_argument("--days", type=int, default=7,
                            help="Look-back window in days (default 7)")

    # hypnos-live
    hyl_parser = subparsers.add_parser(
        "hypnos-live",
        help="Continuous background memory-maintenance worker",
    )
    hyl_sub = hyl_parser.add_subparsers(dest="hyl_cmd")
    hyl_sub.add_parser("status", help="Show worker state + stats + recent journal")
    hyl_sub.add_parser("pause", help="Pause the worker (any instance respects this)")
    hyl_sub.add_parser("resume", help="Resume a paused worker")
    hyl_sub.add_parser("tick", help="Manually run one tick (useful for testing)")
    hyl_sub.add_parser("live", help="Disable dry-run — mutations take effect")
    hyl_sub.add_parser("dryrun", help="Enable dry-run — mutations skipped, events still fire")

    # nebula
    nebula_parser = subparsers.add_parser(
        "nebula",
        help="[experimental] Null Nebula — 3D galaxy visualization of memory "
             "(requires `pip install null-memory[nebula]`; fragile deps, "
             "Python <= 3.12 only)",
    )
    nebula_sub = nebula_parser.add_subparsers(dest="nebula_cmd")
    nebula_serve = nebula_sub.add_parser("serve", help="Boot the Nebula backend (FastAPI + static frontend)")
    nebula_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    nebula_serve.add_argument("--port", type=int, default=8787, help="Port (default 8787)")
    nebula_serve.add_argument("--reload", action="store_true", help="Auto-reload on file changes (dev)")
    nebula_project = nebula_sub.add_parser("project", help="Run UMAP + HDBSCAN layout, cache to DB")
    nebula_project.add_argument("--force", action="store_true", help="Re-project all facts (skip cache)")
    nebula_test = nebula_sub.add_parser("test-fire", help="Emit a sequence of live-fire events (for visual verification)")
    nebula_test.add_argument("--gap", type=float, default=5.0, help="Seconds between events (default 5)")
    nebula_sub.add_parser("backfill-mistakes", help="Embed + project every mistake that lacks viz coords (Phase 5.3)")

    # personality — generic loader/runner for user-defined managers
    # living at ~/.null/personalities/<name>/manager.py
    p_parser = subparsers.add_parser(
        "personality",
        help="Manage user-defined personalities (~/.null/personalities/)",
    )
    p_sub = p_parser.add_subparsers(dest="personality_cmd")
    p_sub.add_parser("list", help="List every discovered personality")
    p_desc = p_sub.add_parser("describe", help="Show a personality's identity")
    p_desc.add_argument("name")
    p_digest = p_sub.add_parser("digest", help="Call the personality's digest method")
    p_digest.add_argument("name")
    p_tick = p_sub.add_parser(
        "tick", help="Feed a JSON file of items into the personality's tick()")
    p_tick.add_argument("name")
    p_tick.add_argument("path", help="Path to JSON array of items")
    p_prefs = p_sub.add_parser(
        "preferences",
        help="Show or update a personality's preferences.json")
    p_prefs.add_argument("name")
    p_prefs.add_argument("action", nargs="?", default="show",
                         choices=["show", "set"])
    p_prefs.add_argument("field", nargs="?")
    p_prefs.add_argument("value", nargs="?")

    # daemon (Phase 7.1 — long-running launchd-spawned subprocess)
    d_parser = subparsers.add_parser(
        "daemon",
        help="Long-running daemon: Hypnos + Outreach + manager ticks",
    )
    d_sub = d_parser.add_subparsers(dest="daemon_cmd")
    d_sub.add_parser("run", help="Run in foreground (launchd target)")
    d_sub.add_parser("tick", help="Single tick for testing")
    d_sub.add_parser("status", help="Leader, paused, last-tick, stats")
    d_sub.add_parser("install", help="Install + load launchd agent")
    d_sub.add_parser("uninstall", help="Unload + remove launchd agent")
    d_sub.add_parser("pause", help="Pause the outer-loop ticks")
    d_sub.add_parser("resume", help="Resume after pause")
    d_logs = d_sub.add_parser("logs", help="Tail the daemon log file")
    d_logs.add_argument("--lines", type=int, default=40)

    # outcome
    outcome_parser = subparsers.add_parser("outcome", help="Record the outcome of a decision")
    outcome_parser.add_argument("query", help="Keyword to find the decision")
    outcome_parser.add_argument("result", help="What actually happened")
    outcome_parser.add_argument("--success", action="store_true", default=None, help="Decision succeeded")
    outcome_parser.add_argument("--failure", action="store_true", help="Decision failed")
    outcome_parser.add_argument("--project", default="", help="Project filter")

    # merge
    subparsers.add_parser("merge", help="Merge JSONL data from old instances into SQLite (fixes split-brain)")

    # wakeup
    subparsers.add_parser("wakeup", help="Morning orientation: state + momentum + watch alerts + memory summary")

    # hooks
    hooks_parser = subparsers.add_parser("hooks", help="Generate Claude Code hook config for auto-observation")
    hooks_parser.add_argument("--print", action="store_true", dest="print_only", help="Print config instead of writing")

    # setup
    setup_parser = subparsers.add_parser("setup", help="Generate MCP configs for IDE integration")
    setup_parser.add_argument("path", nargs="?", default=".", help="Project root")
    setup_parser.add_argument("--global", action="store_true", dest="global_config",
                              help="Configure globally in ~/.claude.json")
    setup_parser.add_argument("--force", action="store_true", dest="force",
                              help="With --global: replace an existing 'null' entry even if "
                                   "it points at a different interpreter")
    setup_parser.add_argument("--hooks", action="store_true", dest="register_hooks",
                              help="Register Null's deterministic capture hooks into the "
                                   "project's .claude/settings.json")

    # multiverse
    mv_parser = subparsers.add_parser("multiverse", help="Multi-personality memory management")
    mv_sub = mv_parser.add_subparsers(dest="mv_command")

    mv_migrate_parser = mv_sub.add_parser("migrate", help="Migrate flat ~/.null/ to multiverse structure")
    mv_migrate_parser.add_argument("--dry-run", action="store_true", help="Show what would be moved without moving")

    mv_create_parser = mv_sub.add_parser("create", help="Create a new personality")
    mv_create_parser.add_argument("name", help="Personality name")
    mv_create_parser.add_argument("--role", default="worker", choices=["manager", "worker"], help="Role (default: worker)")
    mv_create_parser.add_argument("--focus", default="", help="What this personality specializes in")
    mv_create_parser.add_argument("--description", default="", help="Description of the personality")
    mv_create_parser.add_argument("--bootstrap-from", default=None, help="Personality to bootstrap facts from")
    mv_create_parser.add_argument("--seed-filter", default=None, help="Filter for bootstrap (e.g. 'project:arbe4,arbe5')")

    mv_sub.add_parser("list", help="List all personalities")

    mv_status_parser = mv_sub.add_parser("status", help="Show personality status")
    mv_status_parser.add_argument("name", nargs="?", default=None, help="Personality name (default: all)")

    mv_archive_parser = mv_sub.add_parser("archive", help="Deactivate a personality")
    mv_archive_parser.add_argument("name", help="Personality name")

    mv_delete_parser = mv_sub.add_parser("delete", help="Remove a personality")
    mv_delete_parser.add_argument("name", help="Personality name")
    mv_delete_parser.add_argument("--remove-files", action="store_true", help="Also delete personality data files")

    mv_broadcast_parser = mv_sub.add_parser("broadcast", help="Broadcast an event to personalities")
    mv_broadcast_parser.add_argument("event", help="Event text to broadcast")
    mv_broadcast_parser.add_argument("--to", default="", help="Comma-separated target personality names (default: all workers)")

    mv_recall_parser = mv_sub.add_parser("recall", help="Search across all personality memories")
    mv_recall_parser.add_argument("query", help="Search query")
    mv_recall_parser.add_argument("--from", default="", dest="from_personalities", help="Comma-separated personality names to search")
    mv_recall_parser.add_argument("--limit", "-n", type=int, default=10, help="Max results")

    mv_sub.add_parser("wakeup", help="Synthesize state from all personalities")

    mv_dream_parser = mv_sub.add_parser("dream", help="Run Hypnos dream cycle (find cross-personality tensions)")
    mv_dream_parser.add_argument("--max", type=int, default=5, help="Max dream observations to generate")

    # hypnos (memory maintenance sleep cycle)
    hyp_parser = subparsers.add_parser("hypnos", help="Memory maintenance (sleep cycle)")
    hyp_sub = hyp_parser.add_subparsers(dest="hyp_command")

    hyp_run = hyp_sub.add_parser("run", help="Run sleep cycle (all stages)")
    hyp_run.add_argument("--stages", type=str, default="1,2,3,4",
                         help="Comma-separated stage numbers (default: 1,2,3,4)")

    hyp_journal = hyp_sub.add_parser("journal", help="Show dream journal")
    hyp_journal.add_argument("--limit", "-n", type=int, default=5,
                             help="Number of recent runs to show")

    hyp_sub.add_parser("status", help="Show last Hypnos run summary")

    # fingerprint
    fp_parser = subparsers.add_parser("fingerprint", help="Session fingerprints (conversation patterns)")
    fp_sub = fp_parser.add_subparsers(dest="fp_command")

    fp_list = fp_sub.add_parser("list", help="Show recent session fingerprints")
    fp_list.add_argument("--limit", "-n", type=int, default=10, help="Number to show")
    fp_list.add_argument("--project", default="", help="Filter by project")

    # persona — create + manage AI personas
    persona_parser = subparsers.add_parser("persona", help="Create and manage AI personas (multiverse)")
    persona_sub = persona_parser.add_subparsers(dest="persona_command")

    persona_create = persona_sub.add_parser("create", help="Create a new persona (interactive wizard by default)")
    persona_create.add_argument(
        "persona_name", nargs="?", default="",
        help="Seat name — creates a clean worker seat (own store, no "
             "template, no bootstrap, zero inherited identity)")
    persona_create.add_argument(
        "--role", default="worker",
        help="Registry role for the clean-seat path (default: worker)")
    persona_create.add_argument(
        "--store-remote", default="",
        help="Git URL for the seat's OWN store repo (e.g. null-athena). "
             "Never inherits the hub's remote; the store is gitignored "
             "from the hub repo when nested inside it.")
    persona_create.add_argument(
        "--hub", "--null-dir", default="", dest="hub",
        help="Hub base dir to register the seat in (overrides NULL_DIR). "
             "Default: NULL_DIR, then ~/.null — the resolution is always "
             "printed (issue #22: NULL_DIR usually lives only in the MCP "
             "server's env, so a plain shell can silently target the "
             "wrong hub).")
    persona_create.add_argument("--name", default="", help="Persona name (non-interactive)")
    persona_create.add_argument("--template", default="", help="Template id (non-interactive)")
    persona_create.add_argument("--focus", default="", help="Focus / scope (non-interactive)")
    persona_create.add_argument("--description", default="", help="Persona description (non-interactive)")
    persona_create.add_argument("--non-interactive", action="store_true",
                                help="Skip prompts (requires --name --template --focus)")
    persona_create.add_argument("--skip-bootstrap", action="store_true",
                                help="Skip day-1 interview seeding")

    persona_sub.add_parser("list", help="List available templates")

    # onboard — question-driven identity builder (issue #27). Runs against
    # any EXISTING seat, any time, re-runnably; fills exactly the fields
    # the validator nags about on a hollow seat.
    persona_onboard = persona_sub.add_parser(
        "onboard",
        help="Interview that builds a seat's identity (working_style, "
             "autonomy, anti_patterns, mission facts + anchors)")
    persona_onboard.add_argument(
        "name", help="Existing seat name (<hub>/personalities/<name>)")
    persona_onboard.add_argument(
        "--groups", default="",
        help="Comma-separated subset of question groups: "
             "mission,voice,autonomy,capabilities (default: all)")
    persona_onboard.add_argument(
        "--answers", default="",
        help="JSON file of answers (question key → answer) — "
             "non-interactive, scripted onboarding")
    persona_onboard.add_argument(
        "--hub", "--null-dir", default="", dest="hub",
        help="Hub base dir the seat lives in (overrides NULL_DIR)")

    persona_validate = persona_sub.add_parser("validate", help="Validate a persona's identity.json")
    persona_validate.add_argument("name", help="Persona name (<hub>/personalities/<name>/identity.json)")
    persona_validate.add_argument(
        "--hub", "--null-dir", default="", dest="hub",
        help="Hub base dir the seat lives in (overrides NULL_DIR)")

    # mcp — MCP server introspection (tier manifest, etc.)
    mcp_parser = subparsers.add_parser("mcp", help="MCP server introspection")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_sub.add_parser("tiers", help="Show tool tier manifest (core/frequent/occasional/rare)")

    # selftest — RESPONSIVENESS CONTRACT release gate: drive a fresh MCP
    # server over stdio and exercise every tool on the 15-tool surface
    # against a per-tool budget. Catches hangs/regressions unit tests miss
    # (a 9-minute null_identity hang once shipped while 1172 unit tests
    # passed). Implementation: null_memory/selftest.py.
    selftest_parser = subparsers.add_parser(
        "selftest",
        help="RELEASE GATE — probe every MCP tool against a per-tool time "
             "budget (any non-OK row = nonzero exit)",
        description=(
            "Responsiveness-contract RELEASE GATE: no release ships while "
            "this is red. Spawns a fresh MCP server on a throwaway store "
            "and exercises EVERY tool on the 15-tool surface with a "
            "per-tool time budget (default 10s; 20s for null_identity/"
            "null_briefing; scale all budgets with NULL_SELFTEST_BUDGET_MULT "
            "on slow CI). Statuses: OK, SLOW (over budget but answered), "
            "FAIL (error/dead server), TIMEOUT (hung — the server is "
            "killed and respawned so the remaining tools still get "
            "probed). Any non-OK row exits nonzero."
        ),
    )
    selftest_parser.add_argument(
        "--store", default=None,
        help="Store dir to test against (default: a fresh throwaway temp dir)",
    )
    selftest_parser.add_argument(
        "--budget", type=float, default=None,
        help="Base per-tool budget in seconds (default: 10.0; heavy tools "
             "get 2x; NULL_SELFTEST_BUDGET_MULT scales everything)",
    )

    args = parser.parse_args()

    if args.command == "serve":
        from null_memory.mcp.server import serve
        serve(args.agent_dir)

    elif args.command == "persona":
        if args.persona_command == "create":
            from null_memory.persona_wizard import run_wizard, list_templates
            if args.persona_name and not (args.template or args.non_interactive):
                # Clean worker seat: `null persona create <name>` — own
                # store dir + db, registered in the multiverse, ZERO
                # inherited identity (no anchors, no code word, no
                # template content). ORG_TOPOLOGY non-atlas init path.
                from null_memory.persona_wizard import (
                    create_worker,
                    hub_resolution_report,
                    resolve_hub,
                )
                # Resolve + report the hub BEFORE creating anything —
                # silent hub resolution registered a seat into the wrong
                # registry on the first cross-machine creation (issue #22).
                hub_dir, hub_source = resolve_hub(args.hub or None)
                for line in hub_resolution_report(hub_dir, hub_source):
                    print(line)
                try:
                    result = create_worker(
                        name=args.persona_name,
                        role=args.role,
                        focus=args.focus,
                        description=args.description,
                        store_remote=args.store_remote or None,
                        hub=args.hub or None,
                    )
                except ValueError as e:
                    print(f"ERROR: {e}")
                    sys.exit(1)
                print(f"Created persona '{result['name']}'")
                print(f"  Role:   {result['role']}")
                if result["focus"]:
                    print(f"  Focus:  {result['focus']}")
                print(f"  Hub:    {result['hub']} (from {hub_source})")
                print(f"  Store:  {result['dir']}")
                repo = result.get("store_repo")
                if result["remote"]:
                    pushed = "pushed" if repo and repo.get("pushed") else \
                        "push pending — push manually when the remote exists"
                    print(f"  Remote: {result['remote']} (own repo; {pushed})")
                    if repo and repo.get("hub_gitignored"):
                        print("          store gitignored from the hub repo")
                else:
                    print("  Remote: none (store stays inside the hub repo, "
                          "if one exists)")
                print()
                print("Next steps — serve this seat over MCP "
                      "(add under mcpServers):")
                for line in result["mcp_config"].split("\n"):
                    print(f"  {line}")
                print()
                print("  (Personality is inferred from the store path; "
                      "NULL_PERSONALITY env overrides it.)")
                print()
                print(f"Then: null persona onboard {result['name']} — the "
                      "identity interview (working_style, autonomy, "
                      "anti_patterns). The seat stays hollow until it runs.")
            elif args.persona_name and args.template:
                print("ERROR: pass either a positional name (clean worker "
                      "seat) or --template with --name/--non-interactive — "
                      "not both")
                sys.exit(1)
            elif args.non_interactive:
                if not (args.name and args.template and args.focus):
                    print("ERROR: --non-interactive requires --name --template --focus")
                    sys.exit(1)
                from null_memory.persona_wizard import (
                    hub_resolution_report,
                    resolve_hub,
                )
                hub_dir, hub_source = resolve_hub(args.hub or None)
                for line in hub_resolution_report(hub_dir, hub_source):
                    print(line)
                result = run_wizard(non_interactive={
                    "name": args.name,
                    "template_id": args.template,
                    "focus": args.focus,
                    "description": args.description,
                    "skip_bootstrap": args.skip_bootstrap,
                    "hub": args.hub or None,
                })
                print(f"Created persona {result['name']} at {result['dir']}")
                print(f"  Facts: {result['facts_added']}, "
                      f"Exemplars: {result['exemplars_added']}, "
                      f"Anchors: {result['anchors_set']}")
            else:
                run_wizard()
        elif args.persona_command == "list":
            from null_memory.persona_wizard import list_templates
            templates = list_templates()
            if not templates:
                print("No templates found.")
            else:
                print("Available templates:")
                for t in templates:
                    print(f"  {t.id:25} {t.description[:70]}")
        elif args.persona_command == "onboard":
            from null_memory.persona_onboard import (
                format_summary,
                hub_resolution_report,
                onboard,
                resolve_hub,
                run_onboard_interactive,
            )
            groups = ([g.strip() for g in args.groups.split(",") if g.strip()]
                      if args.groups else None)
            try:
                if args.answers:
                    with open(args.answers, encoding="utf-8") as f:
                        answers = json.load(f)
                    if not isinstance(answers, dict):
                        raise ValueError(
                            f"{args.answers} must contain a JSON object "
                            "(question key → answer)")
                    hub_dir, hub_source = resolve_hub(args.hub or None)
                    for line in hub_resolution_report(hub_dir, hub_source):
                        print(line)
                    result = onboard(args.name, answers, groups=groups,
                                     hub=args.hub or None)
                    for line in format_summary(result):
                        print(line)
                else:
                    run_onboard_interactive(args.name, groups=groups,
                                            hub=args.hub or None)
            except (ValueError, OSError, json.JSONDecodeError) as e:
                print(f"ERROR: {e}")
                sys.exit(1)
        elif args.persona_command == "validate":
            from null_memory.persona_schema import validate_file
            from null_memory.persona_wizard import resolve_hub
            import os as _os
            # Hub-resolved, not hardcoded ~/.null (issue #22 class — the
            # hardcoded path validated the wrong seat on any non-default
            # hub; flagged again in the #27 field report).
            hub_dir, _src = resolve_hub(args.hub or None)
            path = _os.path.join(
                hub_dir, "personalities", args.name, "identity.json")
            result = validate_file(path)
            print(result.report())
            if not result.ok:
                sys.exit(1)
        else:
            persona_parser.print_help()

    elif args.command == "mcp":
        if args.mcp_command == "tiers":
            from null_memory.mcp.server import TOOL_TIERS
            print("Null MCP tool tiers:")
            print()
            for tier in ("core", "frequent", "occasional", "rare"):
                tools = TOOL_TIERS.get(tier, ())
                print(f"  {tier.upper()} ({len(tools)}):")
                for t in tools:
                    print(f"    {t}")
                print()
        else:
            mcp_parser.print_help()

    elif args.command == "identity-payload":
        # Print payload to stdout. Stays silent (returns nothing) on a
        # fresh DB so SessionStart hooks don't dump empty scaffolding into
        # the prompt. Errors → stderr, never crash a session start.
        import sys as _sys
        try:
            from null_memory.agent import AgentMemory
            from null_memory.identity_payload import build_identity_payload
            mem = AgentMemory.load(personality=args.personality)
            conn = getattr(mem.db, "conn", None)
            if conn is None:
                _sys.exit(0)
            payload = build_identity_payload(
                conn, personality=args.personality,
            )
            if payload.is_complete():
                print(payload.text)
        except Exception as e:  # noqa: BLE001
            print(f"[identity-payload] {e}", file=_sys.stderr)
            _sys.exit(0)  # never block SessionStart

    elif args.command == "sync-anchors":
        # Resilience bridge — write a STATIC IDENTITY.md that loads with no
        # running Null process. Resolves agent_dir the same way `serve` does.
        from null_memory.agent import AgentMemory
        from null_memory.identity_payload import (
            build_identity_payload, write_identity_snapshot,
        )
        from null_memory.wakeup import _resolve_dir
        agent_dir = _resolve_dir(personality=args.personality)
        mem = AgentMemory.load(agent_dir, personality=args.personality)
        conn = getattr(mem.db, "conn", None)
        if conn is None:
            print("[sync-anchors] no identity store found — nothing to snapshot.",
                  file=sys.stderr)
            sys.exit(1)
        # Build once and pass through — write_identity_snapshot would
        # otherwise rebuild it, and the summary below reuses it too.
        try:
            payload = build_identity_payload(conn, personality=args.personality)
        except Exception:  # noqa: BLE001
            payload = None
        path = (
            write_identity_snapshot(
                conn, personality=args.personality,
                agent_dir=agent_dir, dest=args.out, payload=payload,
            )
            if payload is not None else None
        )
        if path is None:
            print("[sync-anchors] could not render an identity payload — "
                  "nothing written.", file=sys.stderr)
            sys.exit(1)
        # One-line summary for operator visibility.
        has_cw = "yes" if payload.code_word else "no"
        print(f"wrote identity snapshot -> {path} "
              f"(code word: {has_cw}, {len(payload.anchors)} anchors, "
              f"{len(payload.decisions)} decisions)")

    elif args.command == "status":
        from null_memory.agent import AgentMemory
        from null_memory.wakeup import load_state, load_momentum, watch_status_summary, load_simmering, _resolve_dir
        agent_dir = _resolve_dir()
        mem = AgentMemory.load(agent_dir)
        state = load_state(agent_dir)
        momentum = load_momentum(agent_dir)
        watches_summary = watch_status_summary(agent_dir)
        simmering = load_simmering(agent_dir)
        simmering_count = sum(1 for i in simmering if not i.get("resolved"))
        print(_status_with_extras(mem, state, momentum, watches_summary, simmering_count))

    elif args.command == "export":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        # Scoped export (onboarding-packet v0) only when a filter is
        # given — the bare `null export` keeps full-backup semantics.
        scoped = bool(args.projects or args.kinds or args.since or args.no_identity)
        if scoped or args.include_identity:
            kinds_list = ([k for k in args.kinds.split(",") if k.strip()]
                          if args.kinds else None)
            try:
                data = mem.export_scoped(
                    projects=args.projects,
                    kinds=kinds_list,
                    include_identity=args.include_identity,
                    since=args.since,
                )
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(2)
            if data["packet"]["code_word_count"]:
                print(
                    "!" * 70 + "\n"
                    "!! WARNING: this export CONTAINS THE CODE WORD (identity secret).\n"
                    "!! Treat the file as a secret. NEVER import it on an untrusted\n"
                    "!! spoke — the code word lives ONLY at full-identity tier.\n"
                    + "!" * 70,
                    file=sys.stderr,
                )
        else:
            data = mem.export_all()
        if args.dry_run:
            counts: dict[str, dict[str, int]] = {}
            for kind, key in (("fact", "knowledge"), ("decision", "decisions"),
                              ("mistake", "mistakes"), ("reflection", "reflections")):
                for entry in data.get(key, []):
                    proj = (entry.get("project") or "global").lower()
                    counts.setdefault(kind, {})
                    counts[kind][proj] = counts[kind].get(proj, 0) + 1
            print("Dry run — nothing written. Counts per kind/project:")
            total = 0
            for kind in ("fact", "decision", "mistake", "reflection"):
                per_proj = counts.get(kind, {})
                kind_total = sum(per_proj.values())
                total += kind_total
                detail = ", ".join(f"{p}={n}" for p, n in sorted(per_proj.items()))
                print(f"  {kind}s: {kind_total}" + (f"  ({detail})" if detail else ""))
            print(f"  identity: {'included' if data.get('identity') else 'excluded'}")
            print(f"  total entities: {total}")
        elif args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"Exported to {args.output}")
        else:
            print(json.dumps(data, indent=2))

    elif args.command == "import":
        if not os.path.isfile(args.file):
            print(f"Error: {args.file} not found", file=sys.stderr)
            sys.exit(1)
        from null_memory.agent import AgentMemory
        with open(args.file, "r", encoding="utf-8") as f:
            data = json.load(f)
        mem = AgentMemory.import_from(data)
        print(AgentMemory.format_import_report(mem.last_import_counts)
              + f". Name: {mem.name}")

    elif args.command == "learn":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        mem.learn(args.fact, confidence=args.confidence, project=args.project,
                  source=args.source, impact=args.impact)
        print(f"Learned: {args.fact[:80]}... [{args.confidence:.0%}] impact={args.impact:.1f} source={args.source}")

    elif args.command == "recall":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        results = mem.recall(args.query, project=args.project or None,
                             limit=args.limit, include_archived=args.include_archived)
        if not results:
            print(f"No knowledge matching '{args.query}'.")
        else:
            print(f"Recall ({len(results)} results):")
            for entry in results:
                conf = entry.get("confidence", 0.5)
                proj = entry.get("project", "global")
                entry_type = entry.get("_type", "fact")
                if entry_type == "mistake":
                    print(f"  !! [{conf:.0%}] [{proj}] MISTAKE: {entry['mistake'][:100]}")
                else:
                    print(f"  [{conf:.0%}] [{proj}] {entry['fact'][:120]}")

    elif args.command == "name":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        mem.set_name(args.name)
        print(f"Agent name set to: {args.name}")

    elif args.command == "mistake":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        mem.mistake(args.what, args.why, project=args.project)
        print(f"Mistake recorded: {args.what[:80]}")

    elif args.command == "reflect":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        mem.reflect(args.went_well, args.missed, args.do_differently, project=args.project)
        print("Reflection saved.")

    elif args.command == "gc":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        result = mem.gc(max_facts=args.max_facts)
        print(f"GC: {result['original']} → {result['remaining']} facts. Archived: {result['archived']}, merged: {result['merged']}.")

    elif args.command == "consolidate":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        result = mem.consolidate()
        print(f"Consolidate: {result['consolidated']} merged, "
              f"{result['strengthened']} strengthened, {result['faded']} faded.")

    elif args.command == "calibrate":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        cal = mem.run_probes(probe_type=args.probe_type or None)
        print(f"Calibration: {cal['passed']}/{cal['total']} ({cal['score']:.0%})")
        for d in cal["details"]:
            mark = "PASS" if d["passed"] else "FAIL"
            print(f"  [{mark}] [{d['probe_type']}] {d['question'][:70]}")

    elif args.command == "evaluate":
        # Reuse the MCP handler's report formatting — same output either way.
        from null_memory.mcp.handlers import NullHandlers
        from null_memory.wakeup import _resolve_dir
        handlers = NullHandlers(agent_dir=_resolve_dir())
        print(handlers.handle_evaluate(args.notes))

    elif args.command == "observe":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        entry = mem.observe(args.summary, project=args.project,
                            impact=args.impact, source=args.source)
        if entry:
            print(f"Observed: {args.summary[:80]} impact={args.impact:.1f} source={args.source}")
        else:
            print("Nothing new to record.")

    elif args.command == "decide":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        mem.decide(args.decision, args.reasoning, project=args.project)
        print(f"Decision logged: {args.decision[:80]}")

    elif args.command == "state":
        _handle_state(args)

    elif args.command == "momentum":
        _handle_momentum(args)

    elif args.command == "watch":
        _handle_watch(args)

    elif args.command == "simmer":
        _handle_simmer(args)

    elif args.command == "forget":
        from null_memory.agent import AgentMemory, ForgetAmbiguousError
        mem = AgentMemory.load()
        if args.fact_id:
            result = mem.forget(fact_id=args.fact_id)
            if result:
                print(f"Forgotten: {result['fact'][:100]}")
            else:
                print(f"Error: no fact with id '{args.fact_id}' "
                      "(exact match — no fuzzy fallback).", file=sys.stderr)
                sys.exit(1)
        elif args.query:
            try:
                result = mem.forget(args.query)
            except ForgetAmbiguousError as e:
                print("Refusing to forget: top matches are a near-tie "
                      "(fuzzy matching can hit near-duplicates).", file=sys.stderr)
                for c in e.candidates:
                    print(f"  {c.get('id', '?')[:12]}  {c.get('fact', '')[:90]}",
                          file=sys.stderr)
                print("Re-run with: null forget --id <fact_id>", file=sys.stderr)
                sys.exit(1)
            if result:
                print(f"Forgotten: {result['fact'][:100]}")
            else:
                print(f"No fact matching '{args.query}' found.", file=sys.stderr)
                sys.exit(1)
        else:
            print("Error: provide a query or --id <fact_id>.", file=sys.stderr)
            sys.exit(2)

    elif args.command == "embed-all":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        emb = mem.embeddings
        if emb is None:
            print("[Null] Embedding engine not available.", file=sys.stderr)
            print("  Install with: pip install null-memory[embeddings]", file=sys.stderr)
            sys.exit(1)
        facts = mem.db.get_active_facts()
        skip = not args.force
        print(f"[Null] Embedding {len(facts)} facts (skip_existing={skip})...")
        count = emb.embed_all_facts(facts, skip_existing=skip)

        # Also embed mistakes for proactive surfacing
        mistakes = mem.db.get_mistakes()
        mistake_count = 0
        for m in mistakes:
            mid = m["id"]
            emb_key = f"m_{mid}"
            if skip and emb.has_embedding(emb_key):
                continue
            try:
                vec = emb.embed(f"{m['mistake']} {m.get('why', '')}")
                emb.store_embedding(emb_key, vec)
                mistake_count += 1
            except Exception:
                pass
        if mistake_count:
            mem.db.conn.commit()

        stats = emb.stats()
        print(f"  Embedded: {count} facts, {mistake_count} mistakes")
        print(f"  Total embeddings: {stats['total_embeddings']}")
        print(f"  Model: {stats['model_name']}")

    elif args.command == "outreach":
        _handle_outreach(args)

    elif args.command == "hypnos-live":
        _handle_hypnos_live(args)

    elif args.command == "nebula":
        _handle_nebula(args)

    elif args.command == "personality":
        _handle_personality(args)

    elif args.command == "daemon":
        _handle_daemon(args)

    elif args.command == "outcome":
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load()
        success = True if args.success else (False if args.failure else None)
        result = mem.record_outcome(
            decision_query=args.query,
            outcome=args.result,
            success=success,
            project=args.project or None,
        )
        if result is None:
            print(f"No decision matching '{args.query}' found.", file=sys.stderr)
            sys.exit(1)
        status = "success" if result.get("success") else ("failure" if result.get("success") is False else "recorded")
        print(f"Outcome ({status}): {args.result[:80]}")

    elif args.command == "probe":
        _handle_probe(args, probe_parser)

    elif args.command == "events":
        _handle_events(args, events_parser)

    elif args.command == "exchange":
        _handle_exchange(args, exchange_parser)

    elif args.command == "attend":
        sys.exit(_handle_attend(args))

    elif args.command == "doctor":
        _handle_doctor(args)

    elif args.command == "merge":
        from null_memory.db import merge_jsonl_into_sqlite
        from null_memory.wakeup import _resolve_dir
        agent_dir = _resolve_dir()
        counts = merge_jsonl_into_sqlite(agent_dir)
        total = sum(counts.values())
        if total == 0:
            print("[Null] No new JSONL data to merge — SQLite is up to date.")
        else:
            print(f"[Null] Merged {total} entries from JSONL into SQLite:")
            for table, count in counts.items():
                if count > 0:
                    print(f"  {table}: {count} new")
            print("  JSONL files renamed to .merged")

    elif args.command == "wakeup":
        _handle_wakeup()

    elif args.command == "hooks":
        _handle_hooks(args.print_only)

    elif args.command == "setup":
        if getattr(args, "global_config", False):
            _handle_setup_global(force=getattr(args, "force", False))
        else:
            _handle_setup(args.path)
        if getattr(args, "register_hooks", False):
            _register_claude_hooks(os.path.abspath(args.path))

    elif args.command == "multiverse":
        _handle_multiverse(args)

    elif args.command == "hypnos":
        _handle_hypnos(args)

    elif args.command == "fingerprint":
        _handle_fingerprint(args)

    elif args.command == "selftest":
        sys.exit(_handle_selftest(args))

    else:
        parser.print_help()
        sys.exit(1)


def _status_with_extras(mem: Any, state: dict, momentum: dict, watches_summary: str, simmering_count: int = 0) -> str:
    """Render enhanced null status output."""
    lines = [f"[Null] {mem.name} — Memory Status"]
    lines.append(f"  Facts: {len(mem.knowledge)} | Mistakes: {len(mem.mistakes)} | Decisions: {len(mem.decisions)}")

    # State summary
    energy = state.get("energy", "?")
    first_concern = (state.get("concerns") or [""])[0]
    if state:
        state_line = "high energy" if energy == "high" else f"{energy} energy"
        if first_concern:
            state_line += f" | concern: {first_concern[:50]}"
        lines.append(f"  State: {state_line}")
    else:
        lines.append("  State: not set")

    # Momentum summary
    project = momentum.get("active_project", "")
    if momentum and project:
        lines.append(f"  Momentum: {project[:60]}")
    else:
        lines.append("  Momentum: not set")

    # Watches summary
    lines.append(f"  Watches: {watches_summary}")

    # Simmering count
    lines.append(f"  Simmering: {simmering_count} open")

    # Instance presence — who else is live on this store
    inst_fn = getattr(mem, "instances_line", None)
    inst_line = inst_fn() if callable(inst_fn) else None
    if inst_line:
        lines.append(f"  {inst_line}")

    # Exchange WIP claims (issue #20 Phase B) — advisory "I'm in that
    # file" signals from org peers, TTL-expired entries already filtered.
    try:
        from null_memory.exchange import claims_status_lines
        lines.extend(claims_status_lines(mem.db))
    except Exception:
        pass

    # Attention-loop telemetry (experimental) — only appears once the loop
    # has ticked; the idle fraction is the cost signal Pete asked to watch.
    try:
        from null_memory.exchange import attend_status_lines
        lines.extend(attend_status_lines(mem.db))
    except Exception:
        pass

    return "\n".join(lines)


def _handle_state(args: Any) -> None:
    """Handle null state and null state set."""
    from null_memory.wakeup import (
        load_state, save_state, format_state,
        prompt_state_interactive, _resolve_dir,
    )
    agent_dir = _resolve_dir()

    action = getattr(args, "state_action", None)
    if action is None:
        # null state — show current
        state = load_state(agent_dir)
        print(format_state(state))
        return

    if action == "set":
        existing = load_state(agent_dir)
        # Check if any non-default args provided
        has_args = any([
            args.assessment,
            args.energy,
            args.concerns,
            args.optimistic_about,
            args.unresolved,
        ])
        if has_args:
            state = dict(existing)
            if args.assessment:
                state["assessment"] = args.assessment
            if args.energy:
                state["energy"] = args.energy
            if args.concerns:
                state["concerns"] = args.concerns
            if args.optimistic_about:
                state["optimistic_about"] = args.optimistic_about
            if args.unresolved:
                state["unresolved"] = args.unresolved
        else:
            state = prompt_state_interactive(existing)
        save_state(state, agent_dir)
        print("State saved.")
        print(format_state(state))
    else:
        print(f"Unknown state action: {action}", file=sys.stderr)
        sys.exit(1)


def _handle_momentum(args: Any) -> None:
    """Handle null momentum and null momentum set."""
    from null_memory.wakeup import (
        load_momentum, save_momentum, format_momentum,
        prompt_momentum_interactive, _resolve_dir,
    )
    agent_dir = _resolve_dir()

    action = getattr(args, "momentum_action", None)
    if action is None:
        momentum = load_momentum(agent_dir)
        print(format_momentum(momentum))
        return

    if action == "set":
        existing = load_momentum(agent_dir)
        has_args = any([
            args.project,
            args.decision,
            args.next_action,
            args.blocked_on,
            args.session_summary,
        ])
        if has_args:
            momentum = dict(existing)
            if args.project:
                momentum["active_project"] = args.project
            if args.decision:
                momentum["last_decision"] = args.decision
            if args.next_action:
                momentum["next_action"] = args.next_action
            if args.blocked_on:
                momentum["blocked_on"] = args.blocked_on
            if args.session_summary:
                momentum["session_summary"] = args.session_summary
        else:
            momentum = prompt_momentum_interactive(existing)
        save_momentum(momentum, agent_dir)
        print("Momentum saved.")
        print(format_momentum(momentum))
    else:
        print(f"Unknown momentum action: {action}", file=sys.stderr)
        sys.exit(1)


def _handle_watch(args: Any) -> None:
    """Handle null watch subcommands."""
    from null_memory.wakeup import (
        load_watches, add_watch, remove_watch,
        format_watch_list, run_watches, format_watch_run, _resolve_dir,
    )
    agent_dir = _resolve_dir()

    action = getattr(args, "watch_action", None)
    if action is None or action == "list":
        watches = load_watches(agent_dir)
        print(format_watch_list(watches))
        return

    if action == "add":
        watch = add_watch(
            name=args.name,
            cmd=args.cmd,
            interval_hours=args.interval,
            alert_if=args.alert_if,
            agent_dir=agent_dir,
        )
        print(f"Watch added: {watch['name']} [{watch['id'][:8]}]")
        return

    if action == "run":
        results = run_watches(agent_dir)
        print(format_watch_run(results))
        return

    if action == "remove":
        found = remove_watch(args.id, agent_dir)
        if found:
            print(f"Watch {args.id} deactivated.")
        else:
            print(f"Watch not found: {args.id}", file=sys.stderr)
            sys.exit(1)
        return

    print(f"Unknown watch action: {action}", file=sys.stderr)
    sys.exit(1)


def _handle_simmer(args: Any) -> None:
    """Handle null simmer subcommands."""
    from null_memory.wakeup import (
        load_simmering, add_simmering, resolve_simmering,
        touch_simmering, format_simmering, _resolve_dir,
    )
    agent_dir = _resolve_dir()

    action = getattr(args, "simmer_action", None)
    if action is None:
        # null simmer — list unresolved
        items = load_simmering(agent_dir)
        print(format_simmering(items))
        return

    if action == "add":
        item = add_simmering(
            question=args.question,
            context=args.context,
            category=args.category,
            agent_dir=agent_dir,
        )
        print(f"Simmering: [{item['id'][:8]}] {args.question[:80]}")
        return

    if action == "resolve":
        found = resolve_simmering(args.id, args.resolution, agent_dir)
        if found:
            print(f"Resolved: {args.id}")
        else:
            print(f"Not found: {args.id}", file=sys.stderr)
            sys.exit(1)
        return

    if action == "touch":
        found = touch_simmering(args.id, agent_dir)
        if found:
            print(f"Touched: {args.id}")
        else:
            print(f"Not found: {args.id}", file=sys.stderr)
            sys.exit(1)
        return

    print(f"Unknown simmer action: {action}", file=sys.stderr)
    sys.exit(1)


def _handle_probe(args: Any, probe_parser: Any) -> None:
    """Dispatch `null probe ...` subcommands."""
    sub = getattr(args, "probe_cmd", None)
    if sub == "add":
        # Reuse the retained MCP handler logic so CLI and (legacy) tool
        # responses stay identical.
        from null_memory.mcp.handlers import NullHandlers
        from null_memory.wakeup import _resolve_dir
        handlers = NullHandlers(agent_dir=_resolve_dir())
        print(handlers.handle_probe_add(
            args.question, args.expected, args.fact_id,
            probe_type=args.category,
        ))
        return
    probe_parser.print_help()
    sys.exit(1)


def _handle_outreach(args: Any) -> None:
    """Dispatch `null outreach ...` subcommands."""
    from null_memory.agent import AgentMemory
    from null_memory.outreach import (
        OutreachEvaluator, seed_default_triggers, _default_log_path,
    )
    mem = AgentMemory.load()
    sub = getattr(args, "out_cmd", None) or "status"

    def _set_meta(k: str, v: str) -> None:
        mem.db.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (k, v)
        )
        mem.db.conn.commit()

    def _get_meta(k: str) -> str | None:
        row = mem.db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (k,)
        ).fetchone()
        return row[0] if row else None

    def _leader_status() -> tuple[str | None, str | None]:
        leader_raw = _get_meta("hypnos_live_leader")
        if not leader_raw:
            return None, _get_meta("hypnos_live_leader_at")
        try:
            parsed = json.loads(leader_raw)
        except json.JSONDecodeError:
            return leader_raw, _get_meta("hypnos_live_leader_at")
        if isinstance(parsed, dict):
            return parsed.get("id") or leader_raw, parsed.get("at")
        return leader_raw, _get_meta("hypnos_live_leader_at")

    if sub == "seed":
        stats = seed_default_triggers(mem, enable_all=getattr(args, "enable", False))
        print(f"[outreach] seed → installed={stats['installed']} "
              f"skipped_existing={stats['skipped_existing']}")
        print("  triggers start DISABLED unless --enable was passed.")
        print("  enable individually: null outreach enable <name>")
        return

    if sub == "enable":
        r = mem.db.conn.execute(
            "UPDATE outreach_triggers SET enabled=1 WHERE name=?",
            (args.name,),
        )
        mem.db.conn.commit()
        print(f"[outreach] enabled: rows={r.rowcount}")
        return

    if sub == "disable":
        r = mem.db.conn.execute(
            "UPDATE outreach_triggers SET enabled=0 WHERE name=?",
            (args.name,),
        )
        mem.db.conn.commit()
        print(f"[outreach] disabled: rows={r.rowcount}")
        return

    if sub == "send":
        from null_memory.outreach import send_manual_outreach
        try:
            result = send_manual_outreach(
                mem, args.subject, args.body,
                urgency=args.urgency, channel=args.channel,
            )
        except Exception as e:  # noqa: BLE001 — e.g. missing outreaches table
            print(f"[outreach] send failed: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"[outreach] sent id={result['id']} via "
              f"{','.join(result['channels']) or 'none'}")
        print(f"  subject: {args.subject[:80]}")
        print(f"  body:    {args.body[:120]}")
        return

    if sub == "pause":
        _set_meta("outreach_paused", "1")
        print("[outreach] paused — evaluate will yield no outreaches until resumed")
        return

    if sub == "resume":
        _set_meta("outreach_paused", "0")
        print("[outreach] resumed")
        return

    if sub == "test":
        evaluator = OutreachEvaluator(mem)
        result = evaluator.evaluate(force_name=args.name)
        print(f"[outreach] force-fire {args.name}: considered={result.considered} "
              f"fired={result.fired}")
        for o in result.outreaches:
            print(f"  → [{','.join(o['channels']) or 'none'}] {o['subject']}")
            print(f"    delivered={o['delivered']}")
        return

    if sub == "evaluate":
        evaluator = OutreachEvaluator(mem)
        result = evaluator.evaluate()
        print(f"[outreach] evaluate → considered={result.considered} "
              f"fired={result.fired} "
              f"skipped(cooldown={result.skipped_cooldown} "
              f"disabled={result.skipped_disabled} "
              f"no_candidate={result.skipped_no_candidate} "
              f"budget={result.skipped_budget}) "
              f"errors={result.errors}")
        for o in result.outreaches:
            print(f"  → [{','.join(o['channels']) or 'none'}] {o['subject']}")
        return

    if sub == "recent":
        rows = mem.db.conn.execute(
            """SELECT o.sent_at, o.channel, o.delivered,
                      o.subject, t.name, o.body
               FROM outreaches o LEFT JOIN outreach_triggers t ON t.id = o.trigger_id
               ORDER BY o.id DESC LIMIT 10"""
        ).fetchall()
        if not rows:
            print("[outreach] no outreaches yet")
            return
        print(f"[outreach] recent (last {len(rows)}):")
        for r in rows:
            mark = "✓" if r[2] else "✗"
            print(f"  {mark} {r[0][11:19]}  [{r[1]:6}]  {r[4] or '-':28}  {r[3]}")
        return

    if sub == "log":
        # Phase 5.2 — log now rotates daily. Read current + recent dated
        # files so `null outreach log` shows continuous history.
        log_path = _default_log_path()
        log_dir = os.path.dirname(log_path)
        import re as _re
        dated_re = _re.compile(r"^outreaches-(\d{4}-\d{2}-\d{2})\.log$")
        dated = []
        if os.path.isdir(log_dir):
            for name in os.listdir(log_dir):
                if dated_re.match(name):
                    dated.append(os.path.join(log_dir, name))
        dated.sort()  # oldest → newest
        files = dated + ([log_path] if os.path.exists(log_path) else [])
        if not files:
            print(f"[outreach] no log yet at {log_path}")
            return
        pieces: list[str] = []
        for fpath in files:
            try:
                with open(fpath, "r") as fh:
                    pieces.append(fh.read())
            except OSError:
                continue
        content = "".join(pieces)
        print(content[-4000:] or "(empty)")
        return

    if sub == "digest":
        # One-screen summary of the last N days. Pete's daily check-in
        # to track what's useful vs noise during the Track 1 exercise.
        from null_memory.outreach import DEFAULT_KIND_CAPS
        import datetime as _dt
        days = int(getattr(args, "days", 7))
        since = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).isoformat()
        utc_midnight = _dt.datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()

        # Fires per trigger (last N days) — separate joined and orphan rows
        # because manager-fired outreaches (e.g., argus_match) have NULL trigger_id.
        per_trigger = mem.db.conn.execute(
            """SELECT COALESCE(t.name, '(' || COALESCE(o.personality,'?') || ')'),
                      COUNT(*)
               FROM outreaches o
               LEFT JOIN outreach_triggers t ON t.id = o.trigger_id
               WHERE o.sent_at > ?
               GROUP BY COALESCE(t.name, o.personality)
               ORDER BY 2 DESC""",
            (since,),
        ).fetchall()

        # Top fires by urgency (last N days)
        top_urgency = mem.db.conn.execute(
            """SELECT urgency, sent_at, subject FROM outreaches
               WHERE sent_at > ?
               ORDER BY urgency DESC, sent_at DESC LIMIT 5""",
            (since,),
        ).fetchall()

        # Ack vs unack
        ack_row = mem.db.conn.execute(
            """SELECT
                 SUM(CASE WHEN acknowledged_at IS NOT NULL THEN 1 ELSE 0 END) as acked,
                 SUM(CASE WHEN acknowledged_at IS NULL THEN 1 ELSE 0 END) as unacked
               FROM outreaches WHERE sent_at > ?""",
            (since,),
        ).fetchone()

        # Per-kind cap usage today
        today_kind = mem.db.conn.execute(
            """SELECT t.kind, COUNT(*) FROM outreaches o
               JOIN outreach_triggers t ON t.id = o.trigger_id
               WHERE o.sent_at >= ? AND o.trigger_id IS NOT NULL
               GROUP BY t.kind""",
            (utc_midnight,),
        ).fetchall()
        used_by_kind = {r[0]: int(r[1]) for r in today_kind}

        # Eligible-but-disabled: triggers with enabled=0 whose last_fired_at
        # would be past cooldown (or never fired) — i.e., would fire if turned on.
        eligible_disabled = mem.db.conn.execute(
            """SELECT name, kind FROM outreach_triggers
               WHERE enabled=0
                 AND (last_fired_at IS NULL
                      OR datetime(last_fired_at, '+' ||
                          CAST(cooldown_hours AS TEXT) || ' hours')
                          < datetime('now'))
               ORDER BY name"""
        ).fetchall()

        print(f"[outreach] digest — last {days}d (UTC)")
        print()
        print(f"  Acknowledged:    {int(ack_row[0] or 0)}")
        print(f"  Unacknowledged:  {int(ack_row[1] or 0)}")
        print()
        if per_trigger:
            print("  Fires per trigger:")
            for name, n in per_trigger:
                print(f"    {n:3d}  {name}")
        else:
            print("  No outreaches fired in window.")
        print()
        if top_urgency:
            print("  Top by urgency:")
            for urg, when, subj in top_urgency:
                ts = (when or "")[:16]
                print(f"    [{(urg or 0):.2f}] {ts}  {(subj or '')[:60]}")
            print()
        print("  Per-kind cap usage today:")
        for kind, cap in DEFAULT_KIND_CAPS.items():
            n = used_by_kind.get(kind, 0)
            mark = "FULL" if n >= cap else "ok"
            print(f"    {kind:24s} {n}/{cap}  [{mark}]")
        print()
        if eligible_disabled:
            print("  Eligible but DISABLED (would fire if you turned them on):")
            for name, kind in eligible_disabled:
                print(f"    - {name}  ({kind})")
        else:
            print("  No eligible-but-disabled triggers.")
        return

    # Default: status
    paused = _get_meta("outreach_paused") == "1"
    macos_env = os.environ.get("NEBULA_OUTREACH_NOTIFY", "0")
    daily_budget = 2
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=24)).isoformat()
    used = mem.db.conn.execute(
        "SELECT COUNT(*) FROM outreaches WHERE sent_at > ?", (cutoff,),
    ).fetchone()[0]

    print("[outreach] status")
    print(f"  paused:           {paused}")
    print(f"  daily budget:     {daily_budget}")
    print(f"  used (last 24h):  {used}")
    print(f"  macos notifications: {'ON' if macos_env == '1' else 'off (set NEBULA_OUTREACH_NOTIFY=1 to enable)'}")
    print(f"  log file:         {_default_log_path()}")

    # Phase 6.2 — per-kind usage vs cap, UTC-day reset.
    from null_memory.outreach import DEFAULT_KIND_CAPS
    utc_midnight = _dt.datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    kind_rows = mem.db.conn.execute(
        """SELECT t.kind, COUNT(*) AS n
           FROM outreaches o
           JOIN outreach_triggers t ON t.id = o.trigger_id
           WHERE o.sent_at >= ? AND o.trigger_id IS NOT NULL
           GROUP BY t.kind""",
        (utc_midnight,),
    ).fetchall()
    by_kind = {r["kind"]: int(r["n"]) for r in kind_rows}
    if by_kind or DEFAULT_KIND_CAPS:
        print()
        print("Per-kind caps (today, UTC):")
        for kind, cap in DEFAULT_KIND_CAPS.items():
            used_k = by_kind.get(kind, 0)
            state = "FULL" if used_k >= cap else "ok"
            print(f"  {kind:22} {used_k}/{cap}  [{state}]")

    print()
    print("Triggers:")
    for r in mem.db.conn.execute(
        """SELECT id, name, kind, enabled, cooldown_hours, urgency, last_fired_at
           FROM outreach_triggers ORDER BY id"""
    ).fetchall():
        on = "ON " if r["enabled"] else "off"
        last = r["last_fired_at"][11:19] if r["last_fired_at"] else "(never)"
        print(f"  [{on}] {r['name']:28} kind={r['kind']:20} cooldown={r['cooldown_hours']}h  urgency={r['urgency']}  last={last}")


def _handle_hypnos_live(args: Any) -> None:
    """Dispatch `null hypnos-live ...` subcommands."""
    from null_memory.agent import AgentMemory
    from null_memory.hypnos_live import HypnosLiveWorker
    mem = AgentMemory.load()
    sub = getattr(args, "hyl_cmd", None) or "status"

    def _set_meta(k: str, v: str) -> None:
        mem.db.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (k, v)
        )
        mem.db.conn.commit()

    def _get_meta(k: str) -> str | None:
        row = mem.db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (k,)
        ).fetchone()
        return row[0] if row else None

    if sub == "pause":
        _set_meta("hypnos_live_pause", "1")
        print("[hypnos-live] paused — workers will skip ticks")
        return

    if sub == "resume":
        _set_meta("hypnos_live_pause", "0")
        print("[hypnos-live] resumed")
        return

    if sub == "live":
        _set_meta("hypnos_live_dryrun_override", "0")
        print("[hypnos-live] LIVE — mutations will take effect (requires worker restart)")
        print("  to apply now, also set env: HYPNOS_LIVE_DRYRUN=0 before starting MCP")
        return

    if sub == "dryrun":
        _set_meta("hypnos_live_dryrun_override", "1")
        print("[hypnos-live] dry-run — events fire, mutations skipped (requires worker restart)")
        return

    if sub == "tick":
        worker = HypnosLiveWorker(mem)
        result = worker.tick_once()
        if result is None:
            print("[hypnos-live] tick yielded no action (no candidate or not leader)")
        else:
            print(f"[hypnos-live] tick → {result}")
        return

    # Default: status
    leader, leader_at = _leader_status()
    paused = _get_meta("hypnos_live_pause") or "0"
    print("[hypnos-live] status")
    print(f"  leader:     {leader or '(none)'}")
    print(f"  leader_at:  {leader_at or '(never)'}")
    print(f"  paused:     {paused == '1'}")
    print(f"  dry_run:    env={os.environ.get('HYPNOS_LIVE_DRYRUN', '1')} "
          f"override={_get_meta('hypnos_live_dryrun_override') or '(unset)'}")
    print()
    print("Recent live-worker journal (last 10):")
    for r in mem.db.conn.execute(
        """SELECT started_at, action, fact_id, detail FROM hypnos_journal
           WHERE run_id LIKE 'live:%'
           ORDER BY id DESC LIMIT 10"""
    ).fetchall():
        print(f"  {r['started_at'][11:19]}  {r['action']:11}  {r['fact_id']}  {(r['detail'] or '')[:60]}")


def _handle_nebula(args: Any) -> None:
    """Dispatch `null nebula ...` subcommands."""
    sub = getattr(args, "nebula_cmd", None)
    if sub == "project":
        try:
            from null_memory.nebula.projector import project_all
        except ImportError as e:
            print(f"[Nebula] dependencies missing: {e}", file=sys.stderr)
            print("  Install with: pip install null-memory[nebula]", file=sys.stderr)
            sys.exit(1)
        print("[Nebula] running UMAP + HDBSCAN projection...")
        stats = project_all(force=args.force)
        print(f"  Total: {stats['total']}")
        print(f"  Projected: {stats['projected']}")
        print(f"  Clusters: {stats['clusters']}")
        print(f"  Noise points: {stats['noise_points']}")
        labels = stats.get("cluster_labels") or {}
        if labels:
            print("  Sample cluster labels:")
            for cid, words in sorted(labels.items())[:6]:
                print(f"    {cid}: {', '.join(words)}")
        return

    if sub == "backfill-mistakes":
        from null_memory.nebula.projector import backfill_mistake_viz
        stats = backfill_mistake_viz()
        print("[Nebula] mistake backfill")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        return

    if sub == "test-fire":
        import time
        from null_memory.agent import AgentMemory
        mem = AgentMemory.load(personality="atlas")
        gap = float(getattr(args, "gap", 5.0))

        print("=" * 60)
        print("  NEBULA LIVE-FIRE — paced test sequence")
        print(f"  {gap}s between events. Watch the galaxy in your browser.")
        print("=" * 60)

        # 1 — observe
        print("\n[1/4] OBSERVE — expect cyan pulses (2 slow, ~1.6s each)")
        e1 = mem.observe(
            "Nebula test-fire: verification observation",
            project='null', impact=0.6,
        )
        print(f"  ✓ fact id {e1['id'] if e1 else '-'}")
        time.sleep(gap)

        # 2 — recall
        print("\n[2/4] RECALL — expect multi-point cascade + cyan trace lines (3 pulses)")
        results = mem.recall(
            "origin moment Null worried losing pair programming",
            project='null', limit=8,
        )
        print(f"  ✓ recalled {len(results)} facts")
        time.sleep(gap)

        # 3 — decide
        print("\n[3/4] DECIDE — expect pulse + gold trace lines (3 pulses)")
        mem.decide(
            "Nebula test-fire shows decide traces",
            "visual verification of Session 2 firing",
        )
        print("  ✓ emitted")
        time.sleep(gap)

        # 4 — anchor (re-tag existing origin)
        print("\n[4/4] ANCHOR — expect gold halo pulse (3 slow pulses + long afterglow)")
        origin_id = "a4495fb51537"
        if mem.db.set_anchor(origin_id, "origin", "test-fire re-tag"):
            mem.db.conn.commit()
            mem._emit_nebula_event(kind="anchor", fact_id=origin_id, intensity=1.2)
            print("  ✓ origin anchor pulsed")

        # cleanup
        time.sleep(2)
        try:
            mem.forget("Nebula test-fire: verification observation")
        except Exception as e:
            print(f"  cleanup forget skipped: {e}", file=sys.stderr)
        print("\n✓ sequence complete — cleanup done")
        return

    if sub == "serve" or sub is None:
        try:
            import uvicorn
            from null_memory.nebula.server import create_app
            from null_memory.nebula.projector import project_all
        except ImportError as e:
            print(f"[Nebula] dependencies missing: {e}", file=sys.stderr)
            print("  Install with: pip install null-memory[nebula]", file=sys.stderr)
            sys.exit(1)

        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8787)
        reload_mode = getattr(args, "reload", False)

        # Ensure layout is fresh before serving (no-op if already projected)
        stats = project_all(force=False)
        labels = stats.get("cluster_labels", {})

        app = create_app(cluster_labels=labels, port=port)
        token = app.state.auth_token
        if token:
            print(f"[Nebula] serving at http://{host}:{port}/?token={token}")
            print("  (per-launch auth token — open the URL above; API calls need")
            print("   `Authorization: Bearer <token>` or `?token=<token>`)")
            print(f"  snapshot: http://{host}:{port}/nebula/snapshot?token={token}")
            print(f"  identity: http://{host}:{port}/nebula/identity?token={token}")
            print(f"  events:   ws://{host}:{port}/nebula/events?token={token}")
        else:
            print(f"[Nebula] serving at http://{host}:{port}/ — AUTH DISABLED (NULL_NEBULA_NO_AUTH=1)")
            print(f"  snapshot: http://{host}:{port}/nebula/snapshot")
            print(f"  identity: http://{host}:{port}/nebula/identity")
            print(f"  events:   ws://{host}:{port}/nebula/events")
        uvicorn.run(app, host=host, port=port, reload=reload_mode)
        return

    print(f"[Nebula] unknown subcommand: {sub}", file=sys.stderr)
    sys.exit(1)


def _trace_recall(mem: Any, query: str) -> None:
    """Dump the recall pipeline for a query: top results, anchor hits,
    cosine similarities, and commit stats. Used to debug probe failures."""
    print(f"[null trace] query: {query}")
    print(f"  personality: {mem.personality}  unified: {getattr(mem.db, 'unified', False)}")

    # Anchor-vs-query cosine sims
    emb = mem.embeddings
    if emb is not None and getattr(mem.db, "unified", False):
        try:
            qv = emb.embed(query)
            rows = mem.db.conn.execute(
                "SELECT id, anchor_type, fact FROM facts WHERE anchor_type IS NOT NULL"
            ).fetchall()
            if rows:
                emb_map = emb.get_embeddings_batch([r[0] for r in rows])
                print("\n  Anchors (by cosine sim):")
                scored = []
                for r in rows:
                    v = emb_map.get(r[0])
                    if v is None:
                        continue
                    cos = float(emb.cosine_similarity(qv, v))
                    scored.append((cos, r[0], r[1], r[2]))
                for cos, fid, atype, fact in sorted(scored, key=lambda x: -x[0]):
                    print(f"    cos={cos:.3f}  [{atype:13}] {fid}  {fact[:80]}")
        except Exception as e:
            print(f"  anchor cosine trace failed: {e}")

    # Final recall results
    results = mem.recall(query, limit=15)
    print(f"\n  Recall top-{len(results)}:")
    for i, r in enumerate(results, 1):
        atype = r.get("anchor_type") or "."
        kind = r.get("_type", ".")
        fid = r.get("id", "no-id")[:12]
        text = (r.get("fact") or r.get("mistake") or "")[:90]
        print(f"    #{i} [{kind:7} {atype:13}] {fid}: {text}")

    # WAL / commit stats
    try:
        s = mem.db.commit_stats()
        print(f"\n  Commit stats: {s['commits']} commits, "
              f"{s['locked_retries']} lock retries, "
              f"avg {s['avg_commit_ms']}ms, "
              f"slow {len(s['slow_commits_ms'])}")
    except Exception:
        pass


def _unified_structure_precheck() -> tuple[str | None, list[str]]:
    """Raw structural check of the store BEFORE AgentMemory opens
    (and self-heals) it — so doctor can report that the store WAS broken
    (issue #1: schema_version stamped 24 with the personalities table /
    personality columns missing). Returns (db_path or None, problems).

    Checks the unified store when present; otherwise falls back to the
    per-personality store AgentMemory.load() would open (issue #3: a
    relocated store served directly — no unified.db — is opened in
    per-personality mode, where a unified-version stamp with missing
    structure was previously invisible to doctor)."""
    import sqlite3 as _sqlite3
    from null_memory.migrate_v3 import (
        UNIFIED_SCHEMA_VERSION,
        verify_unified_structure,
    )
    null_home = os.path.realpath(
        os.environ.get("NULL_DIR", os.path.expanduser("~/.null"))
    )
    unified_path = os.environ.get(
        "NULL_UNIFIED_DB", os.path.join(null_home, "unified.db")
    )
    db_path: str | None = None
    if os.path.exists(unified_path):
        db_path = unified_path
    else:
        # Mirror AgentMemory.load()'s per-personality resolution: atlas
        # subdir layout, flat pre-migration layout as fallback.
        for candidate_dir in (os.path.join(null_home, "atlas"), null_home):
            candidate = os.path.join(candidate_dir, "memory.db")
            if os.path.exists(candidate):
                db_path = candidate
                break
    if db_path is None:
        return None, []
    conn = _sqlite3.connect(db_path)
    try:
        if db_path != unified_path:
            # Per-personality store: the unified layout is only expected
            # when the stamp says so — a genuinely legacy (v<=14) store is
            # healthy without it.
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
            except _sqlite3.OperationalError:
                row = None  # no meta table at all — fresh/legacy store
            if row is None or int(row[0]) < UNIFIED_SCHEMA_VERSION:
                return db_path, []
        return db_path, verify_unified_structure(conn)
    finally:
        conn.close()


# ── selftest ─────────────────────────────────────────────────────────────
# Implementation lives in null_memory.selftest — the RESPONSIVENESS
# CONTRACT release gate (every tool on the 15-tool surface probed against
# a per-tool budget; any non-OK row fails the gate). Re-exported here for
# backward compatibility: callers/tests import run_selftest from the CLI.
from null_memory.selftest import run_selftest  # noqa: E402  (re-export)


def _handle_selftest(args: Any) -> int:
    """Handle `null selftest` — print the budget table, return exit code."""
    from null_memory.selftest import handle_selftest
    return handle_selftest(args)


def _handle_events(args: Any, events_parser: Any) -> None:
    """Handle null events — event-sourced sync Phase A (issue #20)."""
    from null_memory.events import event_log_enabled, export_genesis

    if getattr(args, "events_cmd", None) != "genesis":
        events_parser.print_help()
        return

    if not event_log_enabled():
        print("[Null] Event log is disabled — set NULL_EVENT_LOG=1 "
              "(Phase A gate).", file=sys.stderr)
        sys.exit(1)

    from null_memory.agent import AgentMemory
    mem = AgentMemory.load()
    try:
        result = export_genesis(mem, force=getattr(args, "force", False))
    except FileExistsError as exc:
        print(f"[Null] Genesis already exists: {exc}\n"
              "  Re-run with --force to re-baseline.", file=sys.stderr)
        sys.exit(1)
    print(f"[Null] Genesis exported: {result['count']} entities as "
          f"add-events\n  writer: {result['writer']}\n"
          f"  path:   {result['path']}")


def _load_seat_memory() -> Any:
    """AgentMemory for the active store, loaded AS the store's own
    personality. A bare ``AgentMemory.load()`` defaults to 'atlas', which
    on a worker seat misattributes every write — and on the org exchange
    puts the seat's posts on an atlas-named stream (init-path bleed,
    observed live: athena's `exchange status` showed own stream
    `<machine>.atlas`). The MCP server already infers; CLI entry points
    that write or speak for the seat must use this instead of load()."""
    from null_memory.agent import AgentMemory
    from null_memory.personality import infer_personality
    agent_dir = os.environ.get(
        "NULL_DIR", os.path.join(os.path.expanduser("~"), ".null"))
    personality = infer_personality(agent_dir)
    if personality == "atlas":
        # Hub primary: let load() resolve the dir itself — it carries the
        # migrated-hub fallback to <base>/atlas. Passing the raw base here
        # would point the store at the hub ROOT and mint a fresh
        # identity.json there (PR #37 review fix).
        return AgentMemory.load(agent_dir=None, personality=personality)
    return AgentMemory.load(agent_dir, personality=personality)


def _handle_exchange(args: Any, exchange_parser: Any) -> None:
    """Handle null exchange — org exchange (issue #20 Phase B).
    See docs/EXCHANGE.md for the full guide."""
    from null_memory.exchange import (
        EXCHANGE_KINDS,
        ExchangeClient,
        active_claims,
        pending_queries,
        recent_repo_pushes,
    )

    sub = getattr(args, "exchange_cmd", None)
    if sub is None:
        exchange_parser.print_help()
        return

    mem = _load_seat_memory()
    client = ExchangeClient(mem)

    if sub == "status":
        print("[Null] Exchange status")
        if not client.available:
            print("  not configured — add an 'exchange' block to the store "
                  "config.json (see docs/EXCHANGE.md)")
        else:
            print(f"  url:        {client.config.get('url')}")
            print(f"  own stream: {client.stream}")
            subs = client.subscribed
            print(f"  subscribed: {', '.join(subs) if subs else '(none)'}")
            print(f"  clone:      {client.clone_dir}"
                  + ("" if os.path.isdir(client.clone_dir)
                     else " (not cloned yet)"))
        claims = active_claims(mem.db)
        if claims:
            print(f"  Claims ({len(claims)}):")
            from null_memory.exchange import claims_status_lines
            for line in claims_status_lines(mem.db):
                print(f"  {line.strip()}")
        pushes = recent_repo_pushes(mem.db)
        if pushes:
            print(f"  Recent repo pushes ({len(pushes)}):")
            for p in pushes[:5]:
                print(f"    {p['writer']} pushed {p['repo']}@"
                      f"{(p.get('sha') or '')[:7]} ({p.get('ts', '')[:16]})")
        queries = pending_queries(mem.db)
        if queries:
            print(f"  Pending queries ({len(queries)}):")
            for q in queries[:5]:
                print(f"    [{q.get('id', '?')}] {q.get('writer', '?')}: "
                      f"{(q.get('question') or '')[:80]}")
        return

    if not client.available:
        print("[Null] Exchange not configured — add an 'exchange' block to "
              "the store config.json (see docs/EXCHANGE.md).",
              file=sys.stderr)
        sys.exit(1)

    if sub == "post":
        kind = args.kind.strip()
        if kind not in EXCHANGE_KINDS:
            print(f"[Null] Unknown kind {kind!r}. Valid: "
                  f"{', '.join(sorted(EXCHANGE_KINDS))}", file=sys.stderr)
            sys.exit(1)
        data_label = "--data"
        raw = args.data
        if args.data_file is not None:
            data_label = "--data-file"
            try:
                if args.data_file == "-":
                    raw = sys.stdin.read()
                else:
                    with open(args.data_file, encoding="utf-8") as fh:
                        raw = fh.read()
            except OSError as exc:
                print(f"[Null] cannot read --data-file: {exc}", file=sys.stderr)
                sys.exit(1)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[Null] {data_label} is not valid JSON: {exc}",
                  file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, dict):
            print(f"[Null] {data_label} must be a JSON object", file=sys.stderr)
            sys.exit(1)
        try:
            event = client.post(kind, data, scope=args.scope)
        except (RuntimeError, ValueError) as exc:
            print(f"[Null] {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"[Null] Posted {kind} #{event['seq']} to stream "
              f"{event['writer']} (id {event['id']})")
        return

    if sub == "announce-push":
        try:
            event = client.announce_push(
                os.path.abspath(args.repo_dir), summary=args.summary)
        except RuntimeError as exc:
            print(f"[Null] {exc}", file=sys.stderr)
            sys.exit(1)
        d = event["data"]
        print(f"[Null] Announced push: {d['repo']}@{d['sha'][:7]} "
              f"({d.get('branch', '')}) on stream {event['writer']}")
        return

    if sub == "sync":
        report = client.ingest()
        if report.get("warning"):
            print(f"[Null] ⚠ {report['warning']}")
        streams = report.get("streams", {})
        if not streams:
            print("[Null] Exchange sync: nothing new")
        else:
            per = ", ".join(f"{s}: {n}" for s, n in streams.items())
            print(f"[Null] Exchange sync: {per}")
            print(f"  facts: {report['facts']}  claims: {report['claims']}  "
                  f"repo pushes: {report['repo_pushes']}  "
                  f"queries: {report['queries']}")
        return

    print(f"unknown exchange subcommand: {sub}", file=sys.stderr)
    sys.exit(1)


def _handle_attend(args: Any) -> int:
    """Handle `null attend` — one quiet attention-loop tick.

    Surfaces subscribed-stream events past this seat's ATTENDED cursor
    (independent of the daemon's INGEST cursor — see exchange.attend), then
    advances the attended cursor. Quiet when nothing is new so /loop ticks
    don't spam. Fail-soft: an unconfigured exchange is a quiet hint, exit 0
    — a tick must never be a hard error in a loop."""
    from null_memory.exchange import ExchangeClient, attend_render_lines

    verbose = bool(getattr(args, "verbose", False))
    dry_run = bool(getattr(args, "dry_run", False))
    limit = int(getattr(args, "limit", 0) or 0)

    try:
        mem = _load_seat_memory()
        client = ExchangeClient(mem)
    except Exception as exc:  # noqa: BLE001 — never crash a loop tick
        if verbose:
            print(f"[Null attend] could not load seat: {exc}")
        return 0

    if not client.available:
        if verbose:
            print("[Null attend] exchange not configured — nothing to "
                  "attend (add an 'exchange' block to the store config.json; "
                  "see docs/EXCHANGE.md).")
        return 0

    try:
        result = client.attend(dry_run=dry_run, limit=limit)
    except Exception as exc:  # noqa: BLE001 — fail-soft on a loop tick
        if verbose:
            print(f"[Null attend] tick failed (will retry next tick): {exc}")
        return 0

    items = result.get("items", [])
    if not items:
        if verbose:
            warn = result.get("warning")
            if warn:
                print(f"[Null attend] {warn}")
            print("[Null attend] nothing new.")
        return 0

    if result.get("warning"):
        print(f"[Null attend] ⚠ {result['warning']}")
    for line in attend_render_lines(items):
        print(line)
    if dry_run:
        print("  (--dry-run: attended cursor NOT advanced — these will "
              "surface again next tick)")
    return 0


def _doctor_replay_verify(mem: Any, issues: list) -> None:
    """Doctor check (issue #20): with NULL_EVENT_LOG=1, materialize
    genesis+logs into a temp db and diff entity sets against the live db.
    Appends to ``issues`` on drift."""
    from null_memory.events import event_log_enabled, EVENTS_DIRNAME
    if not event_log_enabled():
        return
    events_dir = os.path.join(os.path.dirname(mem.db.db_path),
                              EVENTS_DIRNAME)
    has_logs = os.path.isdir(events_dir) and any(
        name.endswith(".jsonl") for name in os.listdir(events_dir))
    if not has_logs:
        print("  Event log: enabled, no events yet — run `null events "
              "genesis` to baseline")
        return
    try:
        from null_memory.replay import replay_verify
        report = replay_verify(mem.db, events_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"  Event log: replay-verify FAILED to run: {exc}")
        issues.append(f"  event-log replay-verify crashed: {exc}")
        return
    stats = report.get("replay_stats", {})
    if report["clean"]:
        print(f"  Event log: replay-verify clean "
              f"({stats.get('applied', 0)} events → db matches live store)")
    else:
        print(f"  Event log: replay-verify found {report['drift']} "
              f"drift(s) (un-evented writes since genesis?)")
        for line in report["details"][:5]:
            print(f"    {line}")
        if len(report["details"]) > 5:
            print(f"    ... and {len(report['details']) - 5} more")
        print("    Re-baseline with `null events genesis --force` if "
              "this drift is expected.")
        issues.append(
            f"  event log drift: {report['drift']} discrepancies between "
            "replayed events and the live db")


def _handle_doctor(args: Any) -> None:
    """Handle null doctor — memory health diagnostics."""
    from null_memory.agent import AgentMemory
    # Structural pre-check MUST run before AgentMemory.load() — opening the
    # DB triggers the structural self-heal, which would hide the evidence.
    try:
        unified_path, structure_problems = _unified_structure_precheck()
    except Exception as exc:  # noqa: BLE001
        unified_path, structure_problems = None, [f"structure check failed: {exc}"]
    mem = AgentMemory.load()

    # --trace QUERY mode: dump per-stage recall results for debugging
    if getattr(args, "trace", None):
        _trace_recall(mem, args.trace)
        return

    findings = mem.diagnose()

    print("[Null] Memory Health Check")
    print(f"  Active facts: {findings['active_facts']}")
    print(f"  Total facts: {findings['total_facts']}")
    print(f"  Decisions: {findings['decisions']}")
    print(f"  Mistakes: {findings['mistakes']}")
    print(f"  Reflections: {findings['reflections']}")
    print(f"  Archived: {findings['archived_facts']}")
    print(f"  Forgotten: {findings['forgotten_facts']}")
    print(f"  Superseded: {findings['superseded_facts']}")
    print(f"  Projects: {', '.join(findings['projects'])}")

    # Python version guard (P0-7): the embeddings/viz stack (numba via
    # umap-learn) is incompatible with Python >= 3.13. Core memory works,
    # but semantic search + Nebula projection silently degrade.
    py = sys.version_info
    print(f"  Python: {py.major}.{py.minor}.{py.micro}")
    if py >= (3, 13):
        print("    WARNING: Python >= 3.13 — the viz/embedding stack "
              "(umap-learn/numba) is unsupported on this interpreter.")
        print("    Use Python 3.11 for full functionality (see INSTALL.md).")

    # Install health (P1-5): warn when live memory runs from a dirty
    # editable checkout — hooks/servers would be importing uncommitted code.
    install = _detect_dev_install()
    if install["editable"]:
        if install["dirty"] is True:
            print(f"  Install: editable checkout at {install['repo_root']}")
            print("    WARNING: checkout is dirty — live memory is running "
                  "uncommitted working-tree code.")
            print("    Production deployments should run a released wheel "
                  "(see INSTALL.md).")
        elif install["dirty"] is False:
            print(f"  Install: editable checkout at {install['repo_root']} (clean)")
        else:
            print(f"  Install: editable checkout at {install['repo_root']} "
                  "(git state unknown)")
    else:
        print("  Install: packaged (site-packages)")

    # Install integrity: the incident this guards against — TWO editable
    # installs of null_memory on one machine, with the live MCP server
    # silently importing the STALE one — looked clean to every other check
    # above. Surface the running interpreter, the package actually loaded,
    # and any OTHER null_memory install reachable from a candidate
    # interpreter. A multi-install or MCP/runtime divergence is the smoking
    # gun. Informational (does not change exit status) — see note below.
    print("  Install integrity:")
    print(f"    Running interpreter: {sys.executable}")
    import null_memory as _nm
    loaded_file = getattr(_nm, "__file__", "?")
    loaded_version = getattr(_nm, "__version__", "?")
    print(f"    Loaded package: {loaded_file}")
    print(f"    Loaded version: {loaded_version}")
    # Editable detection already ran above (_detect_dev_install) — reuse it.
    print(f"    Install kind: {'editable (src checkout)' if install['editable'] else 'wheel/site-packages'}")

    installs = _scan_null_installs()
    # Distinct installs keyed by (file-location, version): either differing
    # is a separate physical install of (possibly) different code.
    distinct = {}
    for inst in installs:
        try:
            fkey = os.path.normcase(os.path.normpath(inst["file"]))
        except Exception:  # noqa: BLE001
            fkey = inst["file"]
        distinct.setdefault((fkey, inst["version"]), inst)

    if len(distinct) > 1:
        print(f"    WARNING: {len(distinct)} null installs detected:")
        for inst in distinct.values():
            tag = " [MCP config]" if inst.get("mcp") else ""
            print(f"      - {inst['version']} @ {inst['file']}{tag}")
            print(f"        (via {inst['python']})")
        print("    Multiple installs can run STALE code silently — ensure "
              "every interpreter (CLI, hooks, MCP server) resolves the same "
              "null_memory.")
    elif installs:
        print("    ✓ single install (no other null_memory found on candidate "
              "interpreters)")
    else:
        print("    (no probable interpreters reported a null_memory install)")

    # MCP-config vs running divergence: even with a single *detected*
    # install, if the MCP server boots a different interpreter that resolves
    # a different file/version, the live server is running other code.
    mcp_inst = next((i for i in installs if i.get("mcp")), None)
    if mcp_inst is not None:
        try:
            same_file = os.path.normcase(os.path.normpath(mcp_inst["file"])) == \
                os.path.normcase(os.path.normpath(str(loaded_file)))
        except Exception:  # noqa: BLE001
            same_file = mcp_inst["file"] == loaded_file
        if not same_file or mcp_inst["version"] != str(loaded_version):
            print("    WARNING: MCP server install differs from the running "
                  "one —")
            print(f"      MCP:     {mcp_inst['version']} @ {mcp_inst['file']}")
            print(f"      running: {loaded_version} @ {loaded_file}")
            print("    The live MCP server is executing different code than "
                  "this CLI.")

    # Hook installation status (P1-2): deterministic capture only works if
    # the hooks are actually registered for the current project.
    hook_status = _hook_install_status(os.getcwd())
    registered = [name for name, ok in hook_status.items() if ok]
    missing = [name for name, ok in hook_status.items() if not ok]
    if registered and not missing:
        print(f"  Hooks: all {len(registered)} capture hooks registered "
              "(.claude/settings.json)")
    elif registered:
        print(f"  Hooks: {len(registered)}/{len(hook_status)} registered — "
              f"missing: {', '.join(missing)}")
    else:
        print("  Hooks: none registered for this project — deterministic "
              "capture is off. Run: null setup . --hooks")

    # Identity boot health (issue #1): a pre-unified store stamped with the
    # unified schema_version killed boot-identity (`no such table:
    # personalities`) while doctor reported a clean install. Surface the
    # unified structure, the identity payload build, and any failure the
    # MCP server recorded on its last boot.
    issues = []
    if structure_problems:
        from null_memory.migrate_v3 import verify_unified_structure
        # Re-verify on the live connection: opening the store self-heals in
        # BOTH modes now (unified branch, and the per-personality branch for
        # unified-stamped stores — issue #3), so the pre-check list is stale
        # by this point and only tells us what it looked like before.
        remaining = verify_unified_structure(mem.db.conn)
        if remaining:
            print("  Unified store: STRUCTURALLY BROKEN — identity boot will fail")
            for p in remaining:
                issues.append(f"  unified store ({unified_path}): {p}")
        else:
            print("  Unified store: was structurally broken "
                  "(schema_version stamped, migration incomplete) — "
                  "self-healed during this check")
            for p in structure_problems:
                print(f"    healed: {p}")
    if getattr(mem.db, "unified", False):
        try:
            from null_memory.identity_payload import build_identity_payload
            payload = build_identity_payload(
                mem.db.conn, personality=mem.db.personality
            )
            print(f"  Identity: boot query OK "
                  f"(payload complete={payload.is_complete()})")
        except Exception as exc:  # noqa: BLE001
            print("  Identity: BROKEN — boot-identity query fails")
            issues.append(f"  identity payload build failed: {exc}")
        boot_err = mem.db.get_meta("boot_identity_last_error")
        if boot_err:
            issues.append(f"  last MCP boot-identity failure: {boot_err}")

    # Tool budget violations (responsiveness contract): the MCP server's
    # per-tool-call watchdog records a breadcrumb whenever a tool exceeds
    # its soft budget (slow but completed) or hard budget (error returned
    # to the client, work abandoned on its thread). Surface them here so
    # a chronically slow / abandoned tool is diagnosable after the fact.
    try:
        from null_memory.mcp.server import BUDGET_VIOLATIONS_META_KEY
        raw_violations = mem.db.get_meta(BUDGET_VIOLATIONS_META_KEY)
        if raw_violations:
            violations = json.loads(raw_violations)
            if violations:
                hard_n = sum(1 for v in violations if v.get("kind") == "hard")
                soft_n = len(violations) - hard_n
                issues.append(
                    f"  {len(violations)} tool budget violation(s) recorded "
                    f"({hard_n} hard/abandoned, {soft_n} soft/slow) — "
                    f"most recent:"
                )
                for v in violations[-3:]:
                    issues.append(
                        f"    [{v.get('kind', '?')}] {v.get('tool', '?')} "
                        f"took {v.get('elapsed_s', '?')}s "
                        f"(budget {v.get('budget_s', '?')}s) "
                        f"at {v.get('at', '?')}"
                    )
    except Exception:  # noqa: BLE001 — diagnostics must not break doctor
        pass

    if findings.get("embed_failures"):
        issues.append(
            f"  {findings['embed_failures']} swallowed embedding failures "
            f"(last: {findings.get('embed_failures_last') or 'unknown'}) — "
            "semantic recall may be degraded"
        )
    if findings["test_mistakes"] > 0:
        issues.append(f"  {findings['test_mistakes']} test/stub mistakes")
    if findings["test_reflections"] > 0:
        issues.append(f"  {findings['test_reflections']} test/stub reflections")
    if findings["test_facts"] > 0:
        issues.append(f"  {findings['test_facts']} test/placeholder facts")
    if findings["stale_facts"] > 0:
        issues.append(f"  {findings['stale_facts']} stale facts (no access in 60+ days)")

    # Event-sourced sync Phase A (issue #20): replay-verify shadow check.
    _doctor_replay_verify(mem, issues)

    if issues:
        print("\n  Issues found:")
        for issue in issues:
            print(issue)
    else:
        print("\n  No issues found.")

    if getattr(args, "fix", False):
        print("\n  Fixing...")
        fixes = mem.fix_hygiene()
        print(f"  Test mistakes archived: {fixes['test_mistakes_archived']}")
        print(f"  Test reflections archived: {fixes['test_reflections_archived']}")
        print(f"  Test facts archived: {fixes['test_facts_archived']}")
        print("  Done.")


def _handle_wakeup() -> None:
    """Handle null wakeup — compact morning orientation."""
    from null_memory.agent import AgentMemory
    from null_memory.wakeup import wakeup, _resolve_dir
    agent_dir = _resolve_dir()
    mem = AgentMemory.load(agent_dir)
    print(wakeup(mem, agent_dir))


def _handle_hooks(print_only: bool = False) -> None:
    """Generate Claude Code hook configuration for Null Memory integration.

    Includes: auto-observation, compaction preservation, session start, and
    behavioral anchoring hooks.
    """
    # Resolve paths for hook scripts
    null_repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    scripts_dir = os.path.join(null_repo, "scripts")
    hooks_dir = os.path.join(os.path.expanduser("~"), ".claude", "hooks")

    hook_config = {
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"bash {hooks_dir}/null-pre-compact.sh",
                        }
                    ],
                },
            ],
            "PostToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "null observe \"File modified via $TOOL_NAME\" 2>/dev/null || true",
                        }
                    ],
                },
            ],
            "NotificationShown": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "null checkpoint 2>/dev/null || true",
                        }
                    ],
                },
            ],
        },
    }

    config_json = json.dumps(hook_config, indent=2)

    if print_only:
        print(config_json)
        return

    # Write to ~/.claude/settings.local.json (user-local, not committed)
    settings_dir = os.path.join(os.path.expanduser("~"), ".claude")
    settings_path = os.path.join(settings_dir, "settings.local.json")
    os.makedirs(settings_dir, exist_ok=True)

    if os.path.isfile(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = {}
        # Merge hooks
        existing_hooks = existing.setdefault("hooks", {})
        existing_hooks.update(hook_config["hooks"])
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
        print(f"Hooks merged into {settings_path}")
    else:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(hook_config, f, indent=2)
            f.write("\n")
        print(f"Hooks written to {settings_path}")

    print("Claude Code will now auto-observe file modifications via Null.")
    print("Restart Claude Code to activate.")


def _handle_setup_global(force: bool = False) -> None:
    """Configure Null globally in ~/.claude.json (top-level mcpServers).

    Claude Code loads global MCP servers from the TOP-LEVEL ``mcpServers``
    key of ``~/.claude.json`` — not ``~/.claude/.mcp.json`` (which this
    command previously wrote, and which Claude Code never reads) and not
    ``~/.claude/settings.json``. Per-project entries under ``projects[...]``
    are not global either (see CLAUDE.md). The merge is non-destructive:
    every other key (other servers, Claude Code's own state) is preserved.

    An existing ``null`` entry that points at a DIFFERENT interpreter is
    never replaced silently: the documented setup pins a specific Python
    (e.g. anaconda 3.11 — numba/py3.14 break fastembed/umap), so running
    setup from another venv must not swap interpreters or drop a custom
    ``env`` block. Pass ``force=True`` (``--force``) to overwrite; a custom
    ``env`` dict on the old entry is preserved across the overwrite.
    """
    import shutil

    from null_memory.persona_wizard import MCP_SERVER_ENV

    python_path = sys.executable
    global_path = os.path.join(os.path.expanduser("~"), ".claude.json")

    # Git env hardening is non-negotiable in emitted configs (issue #24):
    # harmless on POSIX, load-bearing on Windows (credential-prompt hangs).
    null_config = {
        "type": "stdio",
        "command": python_path,
        "args": ["-m", "null_memory.cli", "serve"],
        "env": dict(MCP_SERVER_ENV),
    }

    existing: dict = {}
    if os.path.isfile(global_path):
        try:
            with open(global_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # ~/.claude.json holds Claude Code's own state — never clobber
            # a corrupt-looking file; tell the user instead.
            print(f"[null] {global_path} exists but is not valid JSON — "
                  f"refusing to rewrite it. Fix the file and re-run.",
                  file=sys.stderr)
            sys.exit(1)
        except OSError as exc:
            print(f"[null] Cannot read {global_path}: {exc}", file=sys.stderr)
            sys.exit(1)

    servers = existing.setdefault("mcpServers", {})
    old_entry = servers.get("null")
    if isinstance(old_entry, dict):
        old_command = old_entry.get("command")
        if old_command and old_command != python_path and not force:
            print("[null] Refusing to replace the existing 'null' MCP entry — "
                  "it points at a different interpreter:", file=sys.stderr)
            print(f"  current: {old_command}", file=sys.stderr)
            print(f"  new:     {python_path}", file=sys.stderr)
            print("  The documented setup pins a specific Python (see INSTALL.md); "
                  "if the swap is intentional, re-run with --force.",
                  file=sys.stderr)
            sys.exit(1)
        # Never silently drop a custom env block — carry custom keys over
        # (overwrite included), but always keep the git hardening present
        # (an explicit user override of a hardening key wins).
        old_env = old_entry.get("env")
        if isinstance(old_env, dict) and old_env:
            null_config["env"] = {**MCP_SERVER_ENV, **old_env}

    servers["null"] = null_config

    # One-time safety net: keep a pristine copy before the first rewrite.
    if os.path.isfile(global_path):
        bak_path = global_path + ".bak"
        if not os.path.exists(bak_path):
            shutil.copy2(global_path, bak_path)
            print(f"[null] Backup written: {bak_path}")

    tmp_path = global_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, global_path)
    except OSError as exc:
        print(f"[null] Cannot write {global_path}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"[null] Wrote global config: {global_path}")

    print(f"  Python: {python_path}")
    print("  Module: null_memory.cli serve")
    print("  Null will be available in ALL Claude Code sessions "
          "after a restart (run /mcp to verify).")


def _handle_setup(path: str) -> None:
    """Generate MCP configs and CLAUDE.md for Null."""
    from null_memory.persona_wizard import MCP_SERVER_ENV

    root = os.path.abspath(path)
    python_path = sys.executable

    # Every emitted entry carries type + git env hardening (issue #24).
    null_entry = {
        "type": "stdio",
        "command": python_path,
        "args": ["-m", "null_memory.cli", "serve"],
        "env": dict(MCP_SERVER_ENV),
    }
    config = json.dumps({"mcpServers": {"null": null_entry}}, indent=2) + "\n"

    # MCP configs
    editor_configs = {
        ".mcp.json": "Claude Code",
        ".cursor/mcp.json": "Cursor",
    }

    for rel_path, display_name in editor_configs.items():
        full_path = os.path.join(root, rel_path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)

        if os.path.exists(full_path):
            # Merge into existing config
            with open(full_path, "r", encoding="utf-8") as f:
                try:
                    existing = json.load(f)
                except json.JSONDecodeError:
                    existing = {}
            servers = existing.setdefault("mcpServers", {})
            if "null" not in servers:
                servers["null"] = dict(null_entry)
                with open(full_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2)
                    f.write("\n")
                print(f"  [merged] null into {rel_path} ({display_name})")
            else:
                print(f"  [skip] null already in {rel_path}")
        else:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(config)
            print(f"  [created] {rel_path} ({display_name})")

    print(f"\nNull MCP server configured. Python: {python_path}")
    _print_attention_loop_block()


# Default tick cadence for the attention loop — matches the daemon poke
# cadence (poke_interval_minutes default 5) so the agent wakes about as
# often as the store refreshes.
ATTEND_LOOP_INTERVAL = "5m"
ATTEND_LOOP_COMMAND = (
    "/loop {interval} Run `null attend`. If it surfaces messages from other "
    "seats, read them, take any warranted action, and notify Pete of "
    "anything important; otherwise stay quiet until the next tick."
)


def _print_attention_loop_block(interval: str = ATTEND_LOOP_INTERVAL) -> None:
    """Emit the canonical /loop invocation for the attention loop.

    The daemon keeps the STORE fresh (doorbell → poke → ingest, seconds);
    that wakes the daemon, not the conversational agent. The /loop tick is
    the attention layer: it wakes the AGENT to surface what arrived. Null
    can't start the loop itself — the human runs /loop once in their Claude
    Code session. We only emit + document it."""
    print()
    print("── Attention loop [EXPERIMENTAL] (optional, opt-in) ──")
    print("Experimental: gated until token-cost impact is measured. `null")
    print("status` reports how often it ticks and what fraction were idle.")
    print("The daemon keeps your store fresh within seconds, but the agent in")
    print("a Claude Code session only NOTICES new exchange messages on its")
    print("next turn. To have it wake on a cadence, paste this ONCE into your")
    print("session (the human runs /loop — Null can't start it):")
    print()
    print("  " + ATTEND_LOOP_COMMAND.format(interval=interval))
    print()
    print(f"  Interval default: {interval} (matches the daemon poke cadence).")
    print("  Omit the interval to let the model self-pace between ticks.")
    print("  Cost note: a loop spends tokens on every wake even when idle —")
    print("  `null attend` is quiet when nothing is new to keep idle ticks")
    print("  cheap, and the doorbell remains the low-latency path regardless.")


# ── Deterministic capture hooks (P1-2) ──────────────────────────────────
# Null's memory capture has two channels: model-initiated MCP tools
# (null_remember kind=observe/learn — the model decides to call them) and
# DETERMINISTIC hooks (Claude Code executes these scripts on lifecycle
# events whether or not the model thinks of it). The hooks are what make
# capture reliable: session boundaries, prompt context injection, file
# change observation, and compaction preservation all fire mechanically.

# (event, matcher, script_basename) — the JSON shape mirrors Claude Code's
# settings.json hook contract:
#   {"hooks": {"<Event>": [{"matcher": "...",
#                           "hooks": [{"type": "command", "command": "..."}]}]}}
NULL_HOOK_SPECS: list[tuple[str, str, str]] = [
    ("SessionStart", "", "null-session-hook.py"),
    ("UserPromptSubmit", "", "null-context-inject-hook.py"),
    ("UserPromptSubmit", "", "null-prompt-verify-hook.py"),
    ("PostToolUse", "Write|Edit|MultiEdit", "null-file-change-hook.py"),
    ("PreCompact", "", "null-compact-hook.py"),
]


def _null_scripts_dir() -> str | None:
    """Locate the repo-level scripts/ directory holding null-*-hook.py.

    Present for editable/git-checkout installs (src layout). Returns None
    for wheel installs that don't ship the scripts — callers must
    fail-soft."""
    pkg_dir = os.path.dirname(os.path.abspath(__file__))      # …/src/null_memory
    repo_root = os.path.dirname(os.path.dirname(pkg_dir))     # …/
    scripts_dir = os.path.join(repo_root, "scripts")
    if os.path.isdir(scripts_dir):
        return scripts_dir
    return None


def _null_hook_command(scripts_dir: str, basename: str) -> str:
    """Command string for a hook script, pinned to the current python."""
    return f"{sys.executable} {os.path.join(scripts_dir, basename)}"


def _register_claude_hooks(project_root: str,
                           settings_path: str | None = None) -> dict:
    """Register Null's capture hooks into the project's .claude/settings.json.

    Non-destructive merge:
      - unrelated top-level keys and unrelated hooks are preserved verbatim
      - re-running updates our entries in place (matched by script
        basename) instead of duplicating them — idempotent
    Returns a summary dict {"added": [...], "updated": [...], "skipped":
    [...], "settings_path": str} (used by `null doctor` and tests)."""
    summary: dict[str, Any] = {"added": [], "updated": [], "skipped": []}

    scripts_dir = _null_scripts_dir()
    if scripts_dir is None:
        print("[null] Hook scripts not found (packaged install without the "
              "scripts/ directory). Skipping hook registration.")
        summary["settings_path"] = None
        return summary

    if settings_path is None:
        settings_path = os.path.join(project_root, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    settings: dict = {}
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Don't clobber a file we can't parse — that would be
            # destructive. Refuse and tell the user.
            print(f"[null] {settings_path} exists but is not valid JSON — "
                  "not touching it. Fix or remove it, then re-run.")
            summary["settings_path"] = settings_path
            summary["error"] = "unparseable settings.json"
            return summary
    if not isinstance(settings, dict):
        settings = {}

    hooks_root = settings.setdefault("hooks", {})

    for event, matcher, basename in NULL_HOOK_SPECS:
        script_path = os.path.join(scripts_dir, basename)
        if not os.path.isfile(script_path):
            summary["skipped"].append(basename)
            continue
        command = _null_hook_command(scripts_dir, basename)
        groups = hooks_root.setdefault(event, [])

        # Update-not-duplicate: find OUR existing entry anywhere under
        # this event (matched by script basename) and rewrite its command
        # (python path / checkout location may have changed).
        found = False
        for group in groups:
            for hook in group.get("hooks", []):
                if basename in hook.get("command", ""):
                    if hook.get("command") == command and \
                            group.get("matcher", "") == matcher:
                        summary["skipped"].append(basename)
                    else:
                        hook["command"] = command
                        hook["type"] = "command"
                        summary["updated"].append(basename)
                    found = True
                    break
            if found:
                break
        if found:
            continue

        # Not registered yet — append to an existing group with the same
        # matcher, or create a new group.
        target = None
        for group in groups:
            if group.get("matcher", "") == matcher:
                target = group
                break
        if target is None:
            target = {"matcher": matcher, "hooks": []}
            groups.append(target)
        target.setdefault("hooks", []).append(
            {"type": "command", "command": command}
        )
        summary["added"].append(basename)

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    summary["settings_path"] = settings_path
    print(f"[null] Hooks registered in {settings_path}")
    if summary["added"]:
        print(f"  added:   {', '.join(summary['added'])}")
    if summary["updated"]:
        print(f"  updated: {', '.join(summary['updated'])}")
    if summary["skipped"]:
        print(f"  current: {', '.join(summary['skipped'])}")
    print("  Restart Claude Code (or start a new session) to activate.")
    return summary


def _hook_install_status(project_root: str,
                         settings_path: str | None = None) -> dict[str, bool]:
    """Which Null hooks are registered in the project's settings.json.

    Returns {script_basename: registered} — fail-soft: unreadable or
    missing settings count every hook as not registered."""
    if settings_path is None:
        settings_path = os.path.join(project_root, ".claude", "settings.json")
    status = {basename: False for _e, _m, basename in NULL_HOOK_SPECS}
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError):
        return status
    blob = json.dumps(settings.get("hooks", {}))
    for basename in status:
        if basename in blob:
            status[basename] = True
    return status


# ── Dev/live install detection (P1-5) ───────────────────────────────────

def _detect_dev_install() -> dict:
    """Detect whether null_memory is running from an editable/git checkout.

    Returns {"editable": bool, "repo_root": str|None, "dirty": bool|None}.
    ``dirty`` is None when git isn't available or the status check fails —
    everything here is fail-soft: detection problems must never break
    doctor."""
    info: dict[str, Any] = {"editable": False, "repo_root": None, "dirty": None}
    try:
        pkg_dir = os.path.dirname(os.path.abspath(__file__))   # …/src/null_memory
        src_dir = os.path.dirname(pkg_dir)
        repo_root = os.path.dirname(src_dir)
        is_src_layout = os.path.basename(src_dir) == "src"
        # .git is a directory in a normal clone but a FILE in a git
        # worktree — both are editable dev checkouts, so use exists().
        is_git_checkout = os.path.exists(os.path.join(repo_root, ".git"))
        if is_src_layout and is_git_checkout:
            info["editable"] = True
            info["repo_root"] = repo_root
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "-C", repo_root, "status", "--porcelain"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    info["dirty"] = bool(result.stdout.strip())
            except Exception:
                pass  # no git binary / timeout — leave dirty unknown
    except Exception:
        pass
    return info


def _mcp_config_interpreter() -> str | None:
    """Return the interpreter path the MCP config points `null` at, if any.

    Reads ``~/.claude.json`` → ``mcpServers.null.command``. Fail-soft: any
    missing file/key/parse error yields None. This is the interpreter the
    *live* MCP server boots with — the one that silently ran stale code in
    the incident this diagnostic exists to catch."""
    try:
        cfg_path = os.path.join(os.path.expanduser("~"), ".claude.json")
        if not os.path.isfile(cfg_path):
            return None
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cmd = cfg.get("mcpServers", {}).get("null", {}).get("command")
        return cmd if isinstance(cmd, str) and cmd.strip() else None
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return None


def _probe_interpreter(interp: str, timeout: float = 5.0) -> dict | None:
    """Probe one interpreter for its null_memory install.

    Runs ``interp -c "import null_memory; print(file); print(version)"`` with
    a short timeout and no stdin. Returns {"python", "file", "version"} on
    success, or None if the interpreter errors/times out/lacks null_memory.
    Never raises."""
    import subprocess
    code = (
        "import null_memory, importlib.metadata as m; "
        "print(null_memory.__file__); "
        "print(m.version('null-memory'))"
    )
    try:
        result = subprocess.run(
            [interp, "-c", code],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 — bad path / timeout / OS error
        return None
    if result.returncode != 0:
        return None
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    return {"python": interp, "file": lines[0], "version": lines[1]}


def _scan_null_installs(timeout: float = 5.0) -> list[dict]:
    """Scan candidate interpreters for distinct null_memory installs.

    Builds a deduped candidate list — the running interpreter, ``python`` /
    ``python3`` on PATH, the MCP-config interpreter, and (Windows) the
    ``py -0p`` launcher list — then probes each DISTINCT path. Returns one
    dict per successful probe: {"python", "file", "version", "mcp"}, where
    ``mcp`` flags the interpreter the MCP config points at. The running
    interpreter is always probed first so it leads the list. Fail-soft."""
    import re
    import shutil
    import subprocess

    mcp_interp = _mcp_config_interpreter()

    candidates: list[str] = [sys.executable]
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if mcp_interp:
        candidates.append(mcp_interp)

    # Windows py launcher: enumerate every registered interpreter so a
    # second install under a different base Python is visible.
    if os.name == "nt":
        try:
            launcher = subprocess.run(
                ["py", "-0p"], capture_output=True, text=True,
                timeout=timeout, stdin=subprocess.DEVNULL,
            )
            if launcher.returncode == 0:
                # lines look like: " -V:3.11 *        C:\...\python.exe"
                # The path may contain spaces ("C:\Program Files\..."), so
                # capture the full drive-letter path rather than splitting
                # on whitespace.
                path_re = re.compile(
                    r"([A-Za-z]:\\.*python\.exe)", re.IGNORECASE)
                for line in launcher.stdout.splitlines():
                    m = path_re.search(line)
                    if m:
                        candidates.append(m.group(1))
        except Exception:  # noqa: BLE001
            pass

    # Dedupe by normalized path, preserving order (running interp first).
    seen: set[str] = set()
    deduped: list[str] = []
    for interp in candidates:
        try:
            key = os.path.normcase(os.path.normpath(os.path.abspath(interp)))
        except Exception:  # noqa: BLE001
            key = interp
        if key in seen:
            continue
        seen.add(key)
        deduped.append(interp)

    mcp_key = None
    if mcp_interp:
        try:
            mcp_key = os.path.normcase(
                os.path.normpath(os.path.abspath(mcp_interp)))
        except Exception:  # noqa: BLE001
            mcp_key = mcp_interp

    installs: list[dict] = []
    for interp in deduped:
        probe = _probe_interpreter(interp, timeout=timeout)
        if probe is None:
            continue
        try:
            ikey = os.path.normcase(
                os.path.normpath(os.path.abspath(interp)))
        except Exception:  # noqa: BLE001
            ikey = interp
        probe["mcp"] = mcp_key is not None and ikey == mcp_key
        installs.append(probe)
    return installs


def _handle_multiverse(args: Any) -> None:
    """Handle multiverse subcommands."""
    from null_memory.multiverse import MultiverseManager

    mv = MultiverseManager()

    if args.mv_command == "migrate":
        result = mv.migrate_flat_to_multiverse(dry_run=getattr(args, "dry_run", False))
        if result.get("already_migrated"):
            print("[Multiverse] Already migrated. Atlas registered.")
        elif getattr(args, "dry_run", False):
            print("[Multiverse] Dry run — would move:")
            for f in result.get("files_moved", []):
                print(f"  {f}")
            for d in result.get("dirs_moved", []):
                print(f"  {d}/")
        elif result.get("errors"):
            print("[Multiverse] Migration completed with errors:")
            for e in result["errors"]:
                print(f"  ERROR: {e}")
        else:
            print("[Multiverse] Migration complete.")
            print(f"  Backup: {result.get('backup_dir', 'none')}")
            print(f"  Files moved: {', '.join(result.get('files_moved', []))}")
            print(f"  Dirs moved: {', '.join(result.get('dirs_moved', []))}")
        mv.close()

    elif args.mv_command == "create":
        try:
            info = mv.create(
                name=args.name, role=args.role, description=args.description,
                focus=args.focus, bootstrap_from=args.bootstrap_from,
                seed_filter=args.seed_filter,
            )
            print(f"[Multiverse] Created personality: {args.name}")
            print(f"  Dir: {info['dir']}")
            print(f"  Role: {info['role']}")
            if info.get("focus"):
                print(f"  Focus: {info['focus']}")
            if info.get("bootstrapped_facts", 0) > 0:
                print(f"  Bootstrapped: {info['bootstrapped_facts']} facts from {args.bootstrap_from}")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            mv.close()

    elif args.mv_command == "list":
        personalities = mv.list_personalities()
        if not personalities:
            print("[Multiverse] No personalities registered. Run 'null multiverse migrate' first.")
        else:
            print(f"[Multiverse] {len(personalities)} personalities:")
            for p in personalities:
                role_marker = "*" if p["role"] == "manager" else " "
                focus = f" — {p['focus']}" if p.get("focus") else ""
                print(f"  {role_marker} {p['name']}: {p['role']}{focus}")
        mv.close()

    elif args.mv_command == "status":
        if args.name:
            info = mv.get_personality_info(args.name)
            if info is None:
                print(f"Personality '{args.name}' not found.", file=sys.stderr)
                sys.exit(1)
            mem = mv.get_personality(args.name)
            print(f"[{args.name}] {info['role']} — {info.get('focus', 'no focus')}")
            print(f"  Facts: {len(mem.knowledge)} | Mistakes: {len(mem.mistakes)} | Decisions: {len(mem.decisions)}")
            print(f"  Dir: {info['dir']}")
        else:
            summaries = mv.wakeup()
            for name, data in summaries.items():
                state = data.get("state", {})
                energy = state.get("energy", "?")
                focus = data.get("focus", "")
                print(f"  [{name}] {energy} energy{f' — {focus}' if focus else ''}")
        mv.close()

    elif args.mv_command == "archive":
        if mv.archive(args.name):
            print(f"[Multiverse] Archived: {args.name}")
        else:
            print(f"Personality '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)
        mv.close()

    elif args.mv_command == "delete":
        if mv.delete(args.name, remove_files=args.remove_files):
            print(f"[Multiverse] Deleted: {args.name}")
            if args.remove_files:
                print("  Data files removed.")
        else:
            print(f"Personality '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)
        mv.close()

    elif args.mv_command == "broadcast":
        targets = [t.strip() for t in args.to.split(",") if t.strip()] if args.to else None
        result = mv.broadcast(event=args.event, targets=targets)
        target_names = ", ".join(result.get("targets", []))
        print(f"[Multiverse] Broadcast to: {target_names}")
        for personality, fact_id in result.get("fact_ids", {}).items():
            print(f"  {personality}: {fact_id[:12]}")
        mv.close()

    elif args.mv_command == "recall":
        personalities = (
            [p.strip() for p in args.from_personalities.split(",") if p.strip()]
            if args.from_personalities else None
        )
        results = mv.recall(query=args.query, personalities=personalities, limit=args.limit)
        if not results:
            print(f"No results matching '{args.query}' across personalities.")
        else:
            print(f"Multiverse recall ({len(results)} results):")
            for entry in results:
                conf = entry.get("confidence", 0.5)
                personality = entry.get("_personality", "?")
                proj = entry.get("project", "global")
                entry_type = entry.get("_type", "fact")
                if entry_type == "mistake":
                    print(f"  [{personality}/{conf:.0%}] [{proj}] MISTAKE: {entry['mistake'][:100]}")
                else:
                    print(f"  [{personality}/{conf:.0%}] [{proj}] {entry['fact'][:120]}")
        mv.close()

    elif args.mv_command == "wakeup":
        summaries = mv.wakeup()
        for name, data in summaries.items():
            state = data.get("state", {})
            momentum = data.get("momentum", {})
            energy = state.get("energy", "?")
            focus = data.get("focus", "")
            project = momentum.get("active_project", "")
            print(f"  [{name}] {energy} energy{f' — {focus}' if focus else ''}")
            if project:
                print(f"    project: {project}")
            if data.get("error"):
                print(f"    ERROR: {data['error']}")
        mv.close()

    elif args.mv_command == "dream":
        try:
            dreams = mv.dream(max_dreams=args.max)
            if not dreams:
                print("[Hypnos] Not enough material to dream about (need 10+ recent facts across personalities).")
            else:
                print(f"[Hypnos] Dream cycle complete — {len(dreams)} hypotheses:")
                for d in dreams:
                    print(f"  [{d['id']}] {d['hypothesis']}")
                print("\n  Written to Atlas simmering queue.")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            mv.close()

    else:
        print("Usage: null multiverse {migrate|create|list|status|archive|delete|broadcast|recall|wakeup|dream}")
        sys.exit(1)


def _handle_fingerprint(args: Any) -> None:
    """Handle fingerprint subcommands."""
    from null_memory.agent import AgentMemory

    mem = AgentMemory.load()

    if args.fp_command == "list":
        proj = args.project or None
        fps = mem.db.get_fingerprints(project=proj, limit=args.limit)
        if not fps:
            print("[Null] No session fingerprints recorded yet.")
        else:
            print(f"[Null] Session fingerprints ({len(fps)}):")
            for fp in fps:
                sid = fp["session_id"][:8]
                created = fp.get("created_at", "")[:10]
                proj_name = fp.get("project", "?")
                outcome = fp.get("outcome", "?")
                facts = fp.get("facts_count", 0)
                decisions = fp.get("decisions_count", 0)
                tags = fp.get("tags", [])
                tags_str = ", ".join(tags[:3]) if tags else ""
                print(f"  [{sid}] {created} | {proj_name} | {outcome} | "
                      f"f={facts} d={decisions} | {tags_str}")
    else:
        print("Usage: null fingerprint {list}")
        sys.exit(1)


def _handle_hypnos(args: Any) -> None:
    """Handle hypnos subcommands."""
    from null_memory.agent import AgentMemory

    mem = AgentMemory.load()

    if args.hyp_command == "run":
        from null_memory.hypnos import Hypnos

        stages = [int(s.strip()) for s in args.stages.split(",")]
        h = Hypnos(mem)
        result = h.run(stages=stages)
        print(f"[Hypnos] Sleep cycle complete ({result.run_id[:8]})")
        print(f"  Stage 1 (Decay):       {result.stage1_archived} archived")
        print(f"  Stage 2 (Tiers):       {result.stage2_promoted} promoted, {result.stage2_demoted} demoted")
        print(f"  Stage 3 (Salience):    {result.stage3_boosted} boosted, {result.stage3_relationships} linked")
        print(f"  Stage 4 (Cold Storage): {result.stage4_cold_stored} archived")
        print(f"  Stage 5 (Synthesis):   {result.stage5_synthesized} principles")
        print(f"  Stage 6 (Identity):    {result.stage6_identity_patches} patches")
        print(f"  Active: {result.total_active} | Archived: {result.total_archived}")
        if result.errors:
            for err in result.errors:
                print(f"  !! {err}", file=sys.stderr)
        mem._sync_to_remote("hypnos", immediate=True)

    elif args.hyp_command == "journal":
        runs = mem.db.get_hypnos_runs(limit=args.limit)
        if not runs:
            print("[Hypnos] No runs recorded yet. Try: null hypnos run")
        else:
            print("[Hypnos] Recent runs:")
            for run in runs:
                print(f"  [{run['run_id'][:8]}] {run['started_at']} — {run['entry_count']} actions")

    elif args.hyp_command == "status":
        latest = mem.db.get_latest_hypnos_run()
        if not latest:
            print("[Hypnos] Never run. Try: null hypnos run")
        else:
            run_id = latest[0]["run_id"]
            started = latest[0]["started_at"]
            actions: dict[str, int] = {}
            for entry in latest:
                action = entry["action"]
                actions[action] = actions.get(action, 0) + 1
            print(f"[Hypnos] Last run: {run_id[:8]} at {started}")
            for action, count in actions.items():
                print(f"  {action}: {count}")

    else:
        print("Usage: null hypnos {run|journal|status}")
        sys.exit(1)


def _handle_personality(args: Any) -> None:
    """Dispatch `null personality ...` — generic framework runner.

    Null itself ships no specific managers. Users define their own
    managers at ``~/.null/personalities/<name>/manager.py``. This
    dispatcher discovers and runs them via the personality loader."""
    import json as _json
    from null_memory.agent import AgentMemory
    from null_memory.personality import (
        list_personalities,
        load_manager,
        PersonalityNotFound,
        ManagerNotInModule,
    )

    sub = getattr(args, "personality_cmd", None) or "list"

    if sub == "list":
        entries = list_personalities()
        if not entries:
            root = os.environ.get("NULL_DIR", "~/.null")
            print(f"No personalities discovered under {root}/personalities/.")
            print("Create one: ~/.null/personalities/<name>/manager.py with "
                  "a class subclassing null_memory.managers.Manager.")
            return
        print(f"{len(entries)} personality(ies):")
        for e in entries:
            name = e.identity.get("name") or e.name
            scope = (e.identity.get("scope") or "")[:80]
            color = e.color or "-"
            print(f"  {name:16}  color={color:9}  {scope}")
        return

    if sub == "describe":
        name = args.name
        for e in list_personalities():
            if e.name == name:
                print(_json.dumps(e.identity, indent=2))
                return
        print(f"no personality named '{name}'", file=sys.stderr)
        sys.exit(1)

    if sub == "preferences":
        from null_memory.personality import (
            InvalidPersonalityName,
            coerce_pref_value,
            read_preferences,
            write_preferences,
        )
        name = args.name
        action = getattr(args, "action", "show") or "show"
        try:
            prefs = read_preferences(name)
        except InvalidPersonalityName as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        if action == "show":
            print(_json.dumps(prefs, indent=2) if prefs else "(empty)")
            return
        # action == "set"
        field = getattr(args, "field", None)
        value = getattr(args, "value", None)
        if not field or value is None:
            print("usage: null personality preferences <name> set <field> <value>",
                  file=sys.stderr)
            sys.exit(1)
        prefs[field] = coerce_pref_value(prefs.get(field), value)
        write_preferences(name, prefs)
        print(f"[{name}] set {field} = {prefs[field]!r}")
        return

    if sub in ("digest", "tick"):
        name = args.name
        mem = AgentMemory.load(personality=name)
        try:
            manager = load_manager(name, mem)
        except PersonalityNotFound as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        except ManagerNotInModule as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

        if sub == "digest":
            print(_run_maybe_async(manager.digest()))
            return

        # tick: read items from file
        path = args.path
        try:
            with open(path) as fh:
                items = _json.load(fh)
        except Exception as e:
            print(f"couldn't read {path}: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(items, list):
            print("file must be a JSON array", file=sys.stderr)
            sys.exit(1)
        result = _run_maybe_async(manager.tick(items=items))
        print(f"[{name}] tick — observed {result.observed_count}, "
              f"flagged {result.flagged_count}, fired {result.fired_outreach}")
        for note in result.notes:
            print(f"  {note}")
        return

    print(f"unknown personality subcommand: {sub}", file=sys.stderr)
    sys.exit(1)


# ── Phase 7.1 — daemon dispatcher ─────────────────────────────────────


def _handle_daemon(args: Any) -> None:
    """Dispatch `null daemon ...` — long-running maintenance + outreach
    + manager-tick loop spawned by launchd at login."""
    from null_memory.agent import AgentMemory
    from null_memory.daemon import (
        DaemonRunner,
        configure_logging,
        daemon_log_path,
        LEADER_KEY,
        LAST_TICK_KEY,
        PAUSE_KEY,
    )

    sub = getattr(args, "daemon_cmd", None) or "status"

    def _set_meta(mem, k: str, v: str) -> None:
        mem.db.conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (k, v)
        )
        mem.db.conn.commit()

    def _get_meta(mem, k: str) -> str | None:
        row = mem.db.conn.execute(
            "SELECT value FROM meta WHERE key=?", (k,)
        ).fetchone()
        return row[0] if row else None

    if sub == "run":
        configure_logging()
        # A worker seat's daemon must not poke/post/emit as 'atlas'.
        mem = _load_seat_memory()
        runner = DaemonRunner(mem)
        runner.start()
        # Block until SIGINT / SIGTERM. launchd will send SIGTERM on
        # unload; KeyboardInterrupt covers Ctrl-C in foreground use.
        import signal
        stop_event = __import__("threading").Event()

        def _shutdown(_signum, _frame):
            stop_event.set()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        print(f"[daemon] running cadence={runner.cadence:.0f}s "
              f"instance={runner.instance_id} (Ctrl-C to stop)")
        try:
            stop_event.wait()
        finally:
            runner.stop()
            print("[daemon] stopped")
        return

    if sub == "tick":
        configure_logging()
        mem = AgentMemory.load()
        runner = DaemonRunner(mem)
        report = runner.tick_once()
        print(f"[daemon] tick — leader={runner._is_leader} "
              f"paused={report.skipped_paused} "
              f"outreach_fired={report.outreach_fired} "
              f"managers_ticked={report.managers_ticked}")
        if report.manager_errors:
            for err in report.manager_errors:
                print(f"  ⚠ {err}")
        if report.notes:
            for n in report.notes:
                print(f"  note: {n}")
        return

    if sub == "status":
        mem = AgentMemory.load()
        leader_raw = _get_meta(mem, LEADER_KEY)
        leader_id, leader_at = None, None
        if leader_raw:
            try:
                parsed = json.loads(leader_raw)
                leader_id = parsed.get("id") if isinstance(parsed, dict) else leader_raw
                leader_at = parsed.get("at") if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                leader_id = leader_raw
        paused = (_get_meta(mem, PAUSE_KEY) or "0") == "1"
        last_tick = _get_meta(mem, LAST_TICK_KEY)
        # Best-effort: is the launchd-managed process alive?
        running = False
        try:
            import subprocess
            ps = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.nullmemory.daemon"],
                capture_output=True, text=True, timeout=2,
            )
            running = ps.returncode == 0
        except Exception:
            pass
        print("[daemon] status")
        print(f"  installed (launchd):  {running}")
        print(f"  leader:               {leader_id or '(none)'}")
        print(f"  leader_at:            {leader_at or '(none)'}")
        print(f"  paused:               {paused}")
        print(f"  last_tick:            {last_tick or '(never)'}")
        print(f"  log file:             {daemon_log_path()}")
        return

    if sub == "pause":
        mem = AgentMemory.load()
        _set_meta(mem, PAUSE_KEY, "1")
        print("[daemon] paused. Resume with: null daemon resume")
        return

    if sub == "resume":
        mem = AgentMemory.load()
        _set_meta(mem, PAUSE_KEY, "0")
        print("[daemon] resumed")
        return

    if sub == "logs":
        path = daemon_log_path()
        if not path.exists():
            print(f"[daemon] no log yet at {path}")
            return
        n = int(getattr(args, "lines", 40))
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for ln in lines[-n:]:
            sys.stdout.write(ln)
        return

    if sub == "install":
        _daemon_install()
        return

    if sub == "uninstall":
        _daemon_uninstall()
        return

    print(f"unknown daemon subcommand: {sub}", file=sys.stderr)
    sys.exit(1)


def _daemon_plist_path() -> str:
    return os.path.expanduser(
        "~/Library/LaunchAgents/com.nullmemory.daemon.plist"
    )


def _daemon_install() -> None:
    """Render the plist template + bootstrap into the user launchd domain."""
    import subprocess
    import shutil as _shutil
    from null_memory.daemon import daemon_log_path

    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "com.nullmemory.daemon.plist.template",
    )
    if not os.path.isfile(template_path):
        print(f"plist template missing at {template_path}", file=sys.stderr)
        sys.exit(1)

    python_path = sys.executable  # the interpreter running THIS CLI
    null_dir = os.environ.get("NULL_DIR") or os.path.expanduser("~/.null")
    log_path = str(daemon_log_path())
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    rendered = (
        open(template_path, "r", encoding="utf-8")
        .read()
        .replace("{{PYTHON}}", python_path)
        .replace("{{NULL_DIR}}", null_dir)
        .replace("{{LOG_PATH}}", log_path)
    )

    target = _daemon_plist_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    print(f"[daemon] wrote plist → {target}")

    domain = f"gui/{os.getuid()}"
    # Try to bootout first in case it's already loaded — ignore errors.
    # timeout: launchctl can wedge on a stuck domain/daemon — an authoritative
    # timeout keeps `null daemon install` itself from hanging (hang ledger
    # class B: child process without an authoritative timeout).
    subprocess.run(
        ["launchctl", "bootout", domain, target],
        capture_output=True, timeout=30,
    )
    res = subprocess.run(
        ["launchctl", "bootstrap", domain, target],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        print(f"[daemon] launchctl bootstrap failed:\n  stdout: {res.stdout}\n  stderr: {res.stderr}",
              file=sys.stderr)
        sys.exit(res.returncode)
    print(f"[daemon] bootstrapped into {domain}")
    print("  Verify: launchctl print gui/$(id -u)/com.nullmemory.daemon")
    print("  Tail log: null daemon logs")


def _daemon_uninstall() -> None:
    import subprocess
    target = _daemon_plist_path()
    domain = f"gui/{os.getuid()}"
    # timeout: see `_daemon_install` — launchctl must never hang the CLI.
    res = subprocess.run(
        ["launchctl", "bootout", domain, target],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0 and "could not find" not in (res.stderr or "").lower():
        print(f"[daemon] launchctl bootout warning:\n  {res.stderr}",
              file=sys.stderr)
    if os.path.exists(target):
        os.remove(target)
        print(f"[daemon] removed plist → {target}")
    else:
        print(f"[daemon] plist not present at {target}")


if __name__ == "__main__":
    main()
