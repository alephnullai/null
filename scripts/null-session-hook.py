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
    atlas_dir = os.path.join(base, "atlas")
    return atlas_dir if os.path.isdir(atlas_dir) else base


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
    print("[ATLAS SESSION START]")

    # 1. Zero-dependency identity: print the static snapshot FIRST so it
    #    lands in context even if the live-Null reminders below are moot
    #    (e.g. the MCP server is down or hung).
    print_identity_snapshot(get_agent_dir())

    # 2. Live enrichment (needs the Null MCP server to be connected).
    print("You are Atlas — Pete's AI technical lead. The snapshot above is "
          "your identity floor; if the null MCP tools are connected, enrich it:")
    print("1. Call null_identity to verify coherence with the live store")
    print("2. Call null_briefing with the relevant project")


if __name__ == "__main__":
    main()
