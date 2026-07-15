#!/usr/bin/env python3
"""Null Memory session hooks — auto-load identity and context at session boundaries.

SessionStart: Loads the static IDENTITY.md snapshot (written by
              `null sync-anchors` and auto-refreshed by the MCP server on
              boot) directly into context — pure stdlib, zero dependency on
              a running Null process. This is the resilience bridge: even if
              the Null MCP server is down or hung, Atlas's identity
              (verification fingerprint, anchors, recent decisions) still
              reaches the session.
              Fires on startup, resume, clear, AND after compaction.

Registered in ~/.claude/settings.json under hooks.SessionStart.
"""

import json
import os
import sys
import time

# Cap the snapshot we inject so a huge identity card can't bloat context.
SNAPSHOT_MAX_LINES = 120
SNAPSHOT_MAX_BYTES = 6 * 1024
STALE_AFTER_DAYS = 30


def get_agent_dir():
    """Resolve the Null agent directory."""
    base = os.environ.get("NULL_DIR", os.path.join(os.path.expanduser("~"), ".null"))
    # A personality sub-store (e.g. ~/.null/atlas/) if one exists, else the
    # root. We check for identity.json rather than hardcoding a persona name —
    # a fresh user is NOT "atlas".
    for sub in sorted(_persona_subdirs(base)):
        if os.path.isfile(os.path.join(base, sub, "identity.json")):
            return os.path.join(base, sub)
    return base


def _persona_subdirs(base):
    try:
        return [d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d)) and not d.startswith(".")]
    except OSError:
        return []


def get_agent_name(agent_dir):
    """The agent's own name from identity.json — never a hardcoded persona.

    Stdlib only (this hook is the works-when-everything-else-is-down path).
    Falls back to a neutral label so we NEVER assert an identity that isn't
    the user's own (a fresh install must not claim to be "Atlas")."""
    try:
        with open(os.path.join(agent_dir, "identity.json"), "r",
                  encoding="utf-8") as f:
            name = (json.load(f).get("name") or "").strip()
            if name:
                return name
    except (OSError, ValueError):
        pass
    return "your agent"


def print_identity_snapshot(agent_dir):
    """Print <agent_dir>/IDENTITY.md if present — stdlib only, never raises.

    This MUST stay dependency-free (no null_memory import, no subprocess):
    it is the path that works when everything else is broken.
    """
    path = os.path.join(agent_dir, "IDENTITY.md")
    try:
        if not os.path.isfile(path):
            print("(no identity snapshot found — run `null sync-anchors` "
                  "to create one so identity survives Null being down)")
            return False
        age_days = max(0.0, (time.time() - os.path.getmtime(path)) / 86400.0)
        if age_days < 1:
            age_str = f"{age_days * 24:.0f}h"
        else:
            age_str = f"{age_days:.0f}d"
        stale = " (stale — run `null sync-anchors`)" if age_days > STALE_AFTER_DAYS else ""
        print(f"--- identity snapshot (age: {age_str}){stale} ---")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(SNAPSHOT_MAX_BYTES)
            # If anything remains past the cap, content was dropped — the
            # truncation marker below must appear even when the capped read
            # ends mid-line with fewer than SNAPSHOT_MAX_LINES lines.
            byte_truncated = bool(f.read(1))
        lines = text.splitlines()
        truncated = byte_truncated or len(lines) > SNAPSHOT_MAX_LINES
        for line in lines[:SNAPSHOT_MAX_LINES]:
            print(line)
        if truncated:
            print("... (snapshot truncated)")
        print("--- end identity snapshot ---")
        return True
    except Exception as exc:  # noqa: BLE001 — the hook must never fail
        print(f"(identity snapshot unreadable: {exc})")
        return False


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    # The SessionStart hook fires on: startup, resume, clear, compact.
    agent_dir = get_agent_dir()
    name = get_agent_name(agent_dir)
    print(f"[{name.upper()} SESSION START]")

    # 1. Zero-dependency identity: print the static snapshot FIRST so it
    #    lands in context even if the live-Null reminders below are moot
    #    (e.g. the MCP server is down or hung).
    print_identity_snapshot(agent_dir)

    # 2. Live enrichment (needs the Null MCP server to be connected). The
    #    identity is whatever the store says — never a hardcoded persona, so a
    #    fresh install is told to be ITS agent, not someone else's.
    print(f"You are {name}. The snapshot above is your identity floor; if the "
          "null MCP tools are connected, enrich it:")
    print("1. Call null_identity to verify coherence with the live store")
    print("2. Call null_briefing with the relevant project")


if __name__ == "__main__":
    main()
