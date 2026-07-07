"""Null v0.4.0 — State, Momentum, Watches, Wakeup."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any


# ── Helpers ──

def _resolve_dir(agent_dir: str | None = None, personality: str = "atlas") -> str:
    if agent_dir is not None:
        return agent_dir
    base = os.environ.get("NULL_DIR", os.path.join(os.path.expanduser("~"), ".null"))
    if personality == "atlas":
        atlas_dir = os.path.join(base, "atlas")
        # Fall back to flat layout if not yet migrated
        return atlas_dir if os.path.isdir(atlas_dir) else base
    return os.path.join(base, "personalities", personality)


def _state_path(agent_dir: str | None = None) -> str:
    return os.path.join(_resolve_dir(agent_dir), "state.json")


def _momentum_path(agent_dir: str | None = None) -> str:
    return os.path.join(_resolve_dir(agent_dir), "momentum.json")


def _watching_path(agent_dir: str | None = None) -> str:
    return os.path.join(_resolve_dir(agent_dir), "watching.jsonl")


def _simmering_path(agent_dir: str | None = None) -> str:
    return os.path.join(_resolve_dir(agent_dir), "simmering.jsonl")


def _age_str(ts: str) -> str:
    """Return human-readable age string for an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except (ValueError, TypeError):
        return ts[:10]


# ── State ──

def load_state(agent_dir: str | None = None) -> dict:
    """Load state.json. Returns empty dict if not found."""
    path = _state_path(agent_dir)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_state(state: dict, agent_dir: str | None = None) -> None:
    """Write state.json, stamping written timestamp."""
    d = _resolve_dir(agent_dir)
    os.makedirs(d, exist_ok=True)
    state = dict(state)
    state["written"] = datetime.now(timezone.utc).isoformat()
    with open(_state_path(agent_dir), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def format_state(state: dict) -> str:
    """Format state for display."""
    if not state:
        return "[State] No state recorded yet. Run: null state set"

    written = state.get("written", "")
    age = _age_str(written) if written else "unknown"

    lines = [f"[State] Written {age}"]
    energy = state.get("energy", "unknown")
    lines.append(f"  Energy: {energy}")

    assessment = state.get("assessment", "")
    if assessment:
        lines.append(f"  Assessment: {assessment}")

    for c in state.get("concerns", []):
        lines.append(f"  ⚠ {c}")

    for o in state.get("optimistic_about", []):
        lines.append(f"  ✓ {o}")

    unresolved = state.get("unresolved", "")
    if unresolved:
        lines.append(f"  Open: {unresolved}")

    return "\n".join(lines)


def prompt_state_interactive(existing: dict) -> dict:
    """Prompt interactively for each state field. Returns merged state dict."""
    print("State update (press Enter to keep current value, blank line to stop lists):")

    assessment = input(f"  Assessment [{existing.get('assessment', '')}]: ").strip()
    energy_raw = input(f"  Energy (high/medium/low) [{existing.get('energy', 'medium')}]: ").strip()

    print("  Concerns (one per line, blank to stop):")
    concerns: list[str] = []
    while True:
        c = input("    + ").strip()
        if not c:
            break
        concerns.append(c)

    print("  Optimistic about (one per line, blank to stop):")
    optimistic: list[str] = []
    while True:
        o = input("    + ").strip()
        if not o:
            break
        optimistic.append(o)

    unresolved = input(f"  Unresolved [{existing.get('unresolved', '')}]: ").strip()

    state = dict(existing)
    if assessment:
        state["assessment"] = assessment
    if energy_raw and energy_raw in ("high", "medium", "low"):
        state["energy"] = energy_raw
    if concerns:
        state["concerns"] = concerns
    if optimistic:
        state["optimistic_about"] = optimistic
    if unresolved:
        state["unresolved"] = unresolved

    return state


# ── Momentum ──

def load_momentum(agent_dir: str | None = None) -> dict:
    """Load momentum.json. Returns empty dict if not found."""
    path = _momentum_path(agent_dir)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_momentum(momentum: dict, agent_dir: str | None = None) -> None:
    """Write momentum.json, stamping updated timestamp."""
    d = _resolve_dir(agent_dir)
    os.makedirs(d, exist_ok=True)
    momentum = dict(momentum)
    momentum["updated"] = datetime.now(timezone.utc).isoformat()
    with open(_momentum_path(agent_dir), "w", encoding="utf-8") as f:
        json.dump(momentum, f, indent=2)


def format_momentum(momentum: dict) -> str:
    """Format momentum for display."""
    if not momentum:
        return "[Momentum] No momentum recorded yet. Run: null momentum set"

    updated = momentum.get("updated", "")
    age = _age_str(updated) if updated else "unknown"

    lines = [f"[Momentum] Updated {age}"]

    project = momentum.get("active_project", "")
    if project:
        lines.append(f"  Project: {project}")

    decision = momentum.get("last_decision", "")
    if decision:
        lines.append(f"  Decision: {decision}")

    next_action = momentum.get("next_action", "")
    if next_action:
        lines.append(f"  Next: {next_action}")

    blocked = momentum.get("blocked_on", "")
    if blocked:
        lines.append(f"  Blocked: {blocked}")

    summary = momentum.get("session_summary", "")
    if summary:
        lines.append(f"  Summary: {summary[:120]}")

    return "\n".join(lines)


def prompt_momentum_interactive(existing: dict) -> dict:
    """Prompt interactively for each momentum field. Returns merged dict."""
    print("Momentum update (press Enter to keep current value):")

    project = input(f"  Active project [{existing.get('active_project', '')}]: ").strip()
    decision = input(f"  Last decision [{existing.get('last_decision', '')}]: ").strip()
    next_action = input(f"  Next action [{existing.get('next_action', '')}]: ").strip()
    blocked = input(f"  Blocked on [{existing.get('blocked_on', '')}]: ").strip()
    summary = input(f"  Session summary [{existing.get('session_summary', '')[:50]}]: ").strip()

    momentum = dict(existing)
    if project:
        momentum["active_project"] = project
    if decision:
        momentum["last_decision"] = decision
    if next_action:
        momentum["next_action"] = next_action
    if blocked:
        momentum["blocked_on"] = blocked
    if summary:
        momentum["session_summary"] = summary

    return momentum


# ── Watches ──

def load_watches(agent_dir: str | None = None) -> list[dict]:
    """Load watching.jsonl. Returns list of active watches (last write per ID wins)."""
    path = _watching_path(agent_dir)
    if not os.path.isfile(path):
        return []

    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Deduplicate by ID — last written entry wins
    seen: dict[str, dict] = {}
    for w in entries:
        wid = w.get("id")
        if wid:
            seen[wid] = w

    return list(seen.values())


def _append_watch(watch: dict, agent_dir: str | None = None) -> None:
    """Append a watch entry to watching.jsonl."""
    d = _resolve_dir(agent_dir)
    os.makedirs(d, exist_ok=True)
    with open(_watching_path(agent_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(watch) + "\n")


def add_watch(
    name: str,
    cmd: str,
    interval_hours: float,
    alert_if: str,
    agent_dir: str | None = None,
) -> dict:
    """Create and persist a new watch. Returns the watch dict."""
    watch: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "check_cmd": cmd,
        "interval_hours": interval_hours,
        "alert_if": alert_if,
        "last_checked": None,
        "last_output": None,
        "active": True,
    }
    _append_watch(watch, agent_dir)
    return watch


def remove_watch(watch_id: str, agent_dir: str | None = None) -> bool:
    """Deactivate a watch by ID (prefix match ok). Returns True if found."""
    watches = load_watches(agent_dir)
    for w in watches:
        wid = w.get("id", "")
        if wid == watch_id or wid.startswith(watch_id):
            w["active"] = False
            _append_watch(w, agent_dir)
            return True
    return False


def format_watch_list(watches: list[dict]) -> str:
    """Format watch list for display."""
    active = [w for w in watches if w.get("active", True)]
    inactive = [w for w in watches if not w.get("active", True)]

    if not watches:
        return "[Watches] No watches configured.\n  Add: null watch add --name NAME --cmd CMD --interval 4 --alert-if CONDITION"

    now = datetime.now(timezone.utc)
    lines = [f"[Watches] {len(active)} active, {len(inactive)} inactive"]

    for w in active:
        last = w.get("last_checked")
        if last:
            try:
                dt = datetime.fromisoformat(last)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_h = (now - dt).total_seconds() / 3600
                interval = float(w.get("interval_hours", 4))
                if age_h >= interval:
                    tag = f"  ⚠ {w['name']} [{w.get('id','?')[:8]}] (due {age_h - interval:.1f}h ago)"
                else:
                    tag = f"  ✓ {w['name']} [{w.get('id','?')[:8]}] (checked {age_h:.1f}h ago)"
            except (ValueError, TypeError):
                tag = f"  ? {w['name']} [{w.get('id','?')[:8]}]"
        else:
            tag = f"  - {w['name']} [{w.get('id','?')[:8]}] (never run)"

        lines.append(tag)
        alert_if = w.get("alert_if", "")
        if alert_if:
            lines.append(f"    alert if: {alert_if}")

    if inactive:
        lines.append(f"\nInactive ({len(inactive)}):")
        for w in inactive:
            lines.append(f"  ✗ {w['name']} [{w.get('id','?')[:8]}]")

    return "\n".join(lines)


def run_watches(agent_dir: str | None = None) -> list[dict]:
    """Run all due watches. Returns list of result dicts.

    Each result: {"watch": w, "output": str, "error": str | None}
    Never raises — all subprocess errors are caught.

    Shell semantics (deliberate): watch ``check_cmd`` values are
    user-authored shell command strings, so they run with shell=True
    through the platform shell — /bin/sh on POSIX, cmd.exe on Windows.
    Shell builtins like ``echo`` therefore work on both platforms.
    timeout=30s prevents hangs; stdout+stderr are captured and truncated.
    """
    watches = load_watches(agent_dir)
    active = [w for w in watches if w.get("active", True)]

    now = datetime.now(timezone.utc)
    results: list[dict] = []

    for w in active:
        # Check if due
        last = w.get("last_checked")
        due = True
        if last:
            try:
                dt = datetime.fromisoformat(last)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_h = (now - dt).total_seconds() / 3600
                due = age_h >= float(w.get("interval_hours", 4))
            except (ValueError, TypeError):
                due = True

        if not due:
            continue

        # Run safely — never raise
        output = ""
        error: str | None = None
        try:
            proc = subprocess.run(
                w["check_cmd"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            combined = (proc.stdout + proc.stderr).strip()
            output = combined[:500] + ("..." if len(combined) > 500 else "")
        except subprocess.TimeoutExpired:
            error = "TIMEOUT after 30s"
        except Exception as exc:
            error = f"ERROR: {exc}"

        result: dict = {
            "watch": w,
            "output": output,
            "error": error,
        }
        results.append(result)

        # Persist updated state (append new entry — last write wins on load)
        updated_watch = dict(w)
        updated_watch["last_checked"] = now.isoformat()
        updated_watch["last_output"] = error if error else output
        _append_watch(updated_watch, agent_dir)

    return results


def format_watch_run(results: list[dict]) -> str:
    """Format watch run results for display."""
    if not results:
        return "[Watches] No watches due."

    lines = [f"[Watches] Ran {len(results)} watch(es):"]
    for r in results:
        w = r["watch"]
        lines.append(f"\n  ▶ {w['name']} [{w.get('id','?')[:8]}]")
        alert_if = w.get("alert_if", "")
        if alert_if:
            lines.append(f"    Alert if: {alert_if}")
        if r.get("error"):
            lines.append(f"    !! {r['error']}")
        else:
            out = r.get("output", "").strip()
            if out:
                for line in out.splitlines()[:5]:
                    lines.append(f"    {line}")
            else:
                lines.append("    (no output)")

    return "\n".join(lines)


def watch_status_summary(agent_dir: str | None = None) -> str:
    """One-line watch status for null status output."""
    watches = load_watches(agent_dir)
    active = [w for w in watches if w.get("active", True)]
    if not active:
        return "0 active"

    now = datetime.now(timezone.utc)
    due_count = 0
    for w in active:
        last = w.get("last_checked")
        if last is None:
            due_count += 1
        else:
            try:
                dt = datetime.fromisoformat(last)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_h = (now - dt).total_seconds() / 3600
                if age_h >= float(w.get("interval_hours", 4)):
                    due_count += 1
            except (ValueError, TypeError):
                due_count += 1

    return f"{len(active)} active ({due_count} due)"


# ── Simmering ──

def load_simmering(agent_dir: str | None = None) -> list[dict]:
    """Load simmering.jsonl. Returns list of all entries (last write per ID wins)."""
    path = _simmering_path(agent_dir)
    if not os.path.isfile(path):
        return []

    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Deduplicate by ID — last written entry wins
    seen: dict[str, dict] = {}
    for e in entries:
        eid = e.get("id")
        if eid:
            seen[eid] = e

    return list(seen.values())


def _append_simmering(entry: dict, agent_dir: str | None = None) -> None:
    """Append an entry to simmering.jsonl."""
    d = _resolve_dir(agent_dir)
    os.makedirs(d, exist_ok=True)
    with open(_simmering_path(agent_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def add_simmering(
    question: str,
    context: str = "",
    category: str = "technical",
    agent_dir: str | None = None,
) -> dict:
    """Add a new simmering item. Returns the created entry."""
    now = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "question": question,
        "context": context,
        "category": category,
        "added": now,
        "last_surfaced": None,
        "resolved": False,
        "resolution": None,
    }
    _append_simmering(entry, agent_dir)
    return entry


def resolve_simmering(
    item_id: str,
    resolution: str,
    agent_dir: str | None = None,
) -> bool:
    """Mark a simmering item resolved. Returns True if found."""
    items = load_simmering(agent_dir)
    for item in items:
        iid = item.get("id", "")
        if iid == item_id or iid.startswith(item_id):
            item = dict(item)
            item["resolved"] = True
            item["resolution"] = resolution
            _append_simmering(item, agent_dir)
            return True
    return False


def touch_simmering(item_id: str, agent_dir: str | None = None) -> bool:
    """Update last_surfaced timestamp on a simmering item. Returns True if found."""
    items = load_simmering(agent_dir)
    for item in items:
        iid = item.get("id", "")
        if iid == item_id or iid.startswith(item_id):
            item = dict(item)
            item["last_surfaced"] = datetime.now(timezone.utc).isoformat()
            _append_simmering(item, agent_dir)
            return True
    return False


def format_simmering(items: list[dict], unresolved_only: bool = True) -> str:
    """Format simmering items for display."""
    if unresolved_only:
        items = [i for i in items if not i.get("resolved")]

    if not items:
        return "[Simmering] No open questions. Add: null simmer add \"question\" --context \"why\""

    lines = [f"[Simmering] {len(items)} open question(s)"]
    for item in items:
        cat = item.get("category", "?")
        short_id = item.get("id", "?")[:8]
        added = item.get("added", "")
        age = _age_str(added) if added else "?"
        lines.append(f"\n  [{short_id}] ({cat}) — added {age}")
        lines.append(f"  {item['question']}")
        ctx = item.get("context", "")
        if ctx:
            lines.append(f"    Context: {ctx}")

    return "\n".join(lines)


def simmering_wakeup_section(agent_dir: str | None = None, limit: int = 3) -> list[str]:
    """Return lines for the wakeup 'Simmering' section (oldest last_surfaced first)."""
    items = load_simmering(agent_dir)
    unresolved = [i for i in items if not i.get("resolved")]

    if not unresolved:
        return []

    # Sort: None last_surfaced first (never surfaced), then oldest first
    def sort_key(item: dict) -> str:
        ls = item.get("last_surfaced")
        return ls if ls else "0000-00-00T00:00:00+00:00"

    unresolved.sort(key=sort_key)
    top = unresolved[:limit]

    lines = [f"Simmering: {len(unresolved)} open — surfacing {len(top)}"]
    for item in top:
        short_id = item.get("id", "?")[:8]
        cat = item.get("category", "?")
        lines.append(f"  [{short_id}] ({cat}) {item['question'][:90]}")
    return lines


# ── Wakeup ──

def wakeup(mem: Any, agent_dir: str | None = None) -> str:
    """Compact morning orientation: state + momentum + watch run + recent facts.

    Designed to be pasted into a session start — not a wall of text.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"[Null] {mem.name} — Wakeup {now_str}", ""]

    # State
    state = load_state(agent_dir)
    if state:
        written = state.get("written", "")
        age = _age_str(written) if written else "unknown"
        energy = state.get("energy", "?")
        lines.append(f"State ({age}): {energy} energy")
        assessment = state.get("assessment", "")
        if assessment:
            lines.append(f"  {assessment}")
        for c in (state.get("concerns") or [])[:2]:
            lines.append(f"  ⚠ {c}")
        for o in (state.get("optimistic_about") or [])[:2]:
            lines.append(f"  ✓ {o}")
        if state.get("unresolved"):
            lines.append(f"  Open: {state['unresolved']}")
    else:
        lines.append("State: not set (run: null state set)")

    lines.append("")

    # Momentum
    momentum = load_momentum(agent_dir)
    if momentum:
        project = momentum.get("active_project", "?")
        lines.append(f"Momentum: {project}")
        if momentum.get("next_action"):
            lines.append(f"  Next: {momentum['next_action']}")
        if momentum.get("blocked_on"):
            lines.append(f"  Blocked: {momentum['blocked_on']}")
        if momentum.get("last_decision"):
            lines.append(f"  Decision: {momentum['last_decision']}")
    else:
        lines.append("Momentum: not set (run: null momentum set)")

    lines.append("")

    # Watches — run due ones
    watch_results = run_watches(agent_dir)
    watches = load_watches(agent_dir)
    active_watches = [w for w in watches if w.get("active", True)]

    if active_watches:
        lines.append(f"Watches: {len(active_watches)} active, {len(watch_results)} ran this wakeup")
        for r in watch_results[:5]:
            w = r["watch"]
            lines.append(f"  ▶ {w['name']} — alert if: {w.get('alert_if', '?')}")
            if r.get("error"):
                lines.append(f"    !! {r['error']}")
            else:
                out_lines = r.get("output", "").splitlines()
                for ol in out_lines[:3]:
                    lines.append(f"    {ol}")
    else:
        lines.append("Watches: none configured")

    lines.append("")

    # Simmering
    simmer_lines = simmering_wakeup_section(agent_dir)
    if simmer_lines:
        lines.extend(simmer_lines)
        lines.append("")

    # Hypnos (last sleep cycle)
    try:
        from null_memory.hypnos import hypnos_wakeup_section
        hyp_lines = hypnos_wakeup_section(mem.db)
        if hyp_lines:
            lines.extend(hyp_lines)
            lines.append("")
    except Exception:
        pass  # Hypnos not available or no runs yet

    # Memory summary
    lines.append(
        f"Memory: {len(mem.knowledge)} facts | "
        f"{len(mem.mistakes)} mistakes | "
        f"{len(mem.decisions)} decisions"
    )

    if mem.knowledge:
        # Sort all non-superseded facts by impact descending, show top 8
        # High-impact facts always surface at wakeup regardless of age
        active_facts = [e for e in mem.knowledge if not e.get("superseded_by")]
        sorted_facts = sorted(active_facts, key=lambda e: e.get("impact", 0.5), reverse=True)
        recent = sorted_facts[:8]
        lines.append("Recent context:")
        for e in recent:
            conf = e.get("confidence", 0.5)
            lines.append(f"  [{conf:.0%}] {e['fact'][:100]}")

    return "\n".join(lines)
