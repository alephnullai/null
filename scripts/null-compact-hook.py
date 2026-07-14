#!/usr/bin/env python3
"""Null Memory compaction hooks — preserve critical knowledge during context compression.

PreCompact: Injects preservation instructions into the compaction prompt so the
            summarizer LLM keeps high-impact Null facts in its output.
PostCompact: Checkpoints the compaction summary into Null memory for Hypnos analysis.

Registered in ~/.claude/settings.json under hooks.PreCompact and hooks.PostCompact.
Reads hook input JSON from stdin, writes instructions to stdout.
"""

import json
import os
import sys


def get_agent_dir():
    """Resolve the Null agent directory."""
    base = os.environ.get("NULL_DIR", os.path.join(os.path.expanduser("~"), ".null"))
    atlas_dir = os.path.join(base, "atlas")
    return atlas_dir if os.path.isdir(atlas_dir) else base


def handle_pre_compact(hook_input):
    """Inject preservation instructions into the compaction prompt.

    Stdout becomes custom instructions appended to the compaction prompt.
    The summarizer LLM will be told to preserve these facts.
    """
    try:
        from null_memory.agent import AgentMemory

        mem = AgentMemory.load(get_agent_dir())

        lines = ["IMPORTANT — Null Memory context to preserve in your summary:"]

        # Top high-impact facts from current knowledge
        facts = mem.db.get_active_facts()
        high_impact = sorted(facts, key=lambda f: f.get("impact", 0.5), reverse=True)

        # Get facts most likely to be relevant (high impact + recently accessed)
        important = []
        for f in high_impact[:15]:
            impact = f.get("impact", 0.5)
            access = f.get("access_count", 0)
            tier = f.get("tier", "contextual")
            if impact >= 0.7 or tier == "durable" or access >= 5:
                important.append(f)
            if len(important) >= 10:
                break

        if important:
            lines.append("")
            lines.append("High-value facts (preserve these):")
            for f in important:
                proj = f.get("project", "global")
                lines.append(f"  [{proj}] {f['fact'][:150]}")

        # Recent decisions (last 5)
        decisions = mem.db.get_decisions(limit=5)
        if decisions:
            lines.append("")
            lines.append("Recent decisions (preserve reasoning):")
            for d in decisions[-5:]:
                lines.append(f"  - {d['decision'][:100]}")

        # Active mistakes (last 3)
        mistakes = mem.db.get_mistakes(limit=3)
        if mistakes:
            lines.append("")
            lines.append("Mistakes to remember (preserve these):")
            for m in mistakes[-3:]:
                lines.append(f"  !! {m['mistake'][:80]} — {m.get('why', '')[:40]}")

        # Pending calibration questions
        try:
            from null_memory.wakeup import load_simmering
            simmering = load_simmering(get_agent_dir())
            calibration = [
                s for s in simmering
                if s.get("category") == "calibration" and not s.get("resolved")
            ]
            if calibration:
                lines.append("")
                lines.append("Pending questions (preserve these):")
                for q in calibration[:3]:
                    lines.append(f"  ? {q['question'][:80]}")
        except Exception:
            pass

        # Output to stdout — becomes compaction custom instructions
        print("\n".join(lines))

    except Exception as e:
        # Non-fatal — if Null can't load, just don't inject instructions
        print(f"# Null Memory: could not load context ({e})", file=sys.stderr)


def handle_post_compact(hook_input):
    """Checkpoint the compaction summary into Null memory."""
    try:
        summary = hook_input.get("compact_summary", "")
        if not summary:
            return

        from null_memory.agent import AgentMemory

        mem = AgentMemory.load(get_agent_dir())

        # Store a condensed version of the summary as a high-confidence fact
        # Truncate to first 500 chars to avoid bloating memory
        condensed = summary[:500].replace("\n", " ").strip()
        if condensed:
            mem.learn(
                f"[compaction] Session context was compressed. Summary: {condensed}",
                confidence=0.7,
                project="global",
                source="observation",
            )
            mem.db.conn.commit()

    except Exception as e:
        print(f"Null Memory: post-compact checkpoint failed ({e})", file=sys.stderr)


def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    event = hook_input.get("hook_event_name", "")

    if event == "PreCompact":
        handle_pre_compact(hook_input)
    elif event == "PostCompact":
        handle_post_compact(hook_input)


if __name__ == "__main__":
    main()
