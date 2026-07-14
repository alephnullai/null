"""`null persona onboard <name>` — question-driven identity builder (issue #27).

Runs against any EXISTING seat (clean or templated), any time, re-runnably.
It is the identity side of hiring: the scoped-export onboarding packet
(#21) delivers what a new seat needs to KNOW; onboard captures who the
seat IS — working_style, autonomy, anti_patterns, escalation boundaries —
exactly the fields the validator nags about on a hollow seat.

Question groups (filterable with --groups):

  mission       who the user is / why this persona / 30-day success
                (reuses BOOTSTRAP_QUESTIONS) plus focus + description.
                → facts + commitment anchors via AgentMemory.learn()/anchor()
  voice         pace / pushback / communication format / humor
                → identity.json working_style
  autonomy      act-first vs ask-first / escalation triggers / never-do-X
                → working_style.autonomy, session_lifecycle.always_escalate,
                  anti_patterns
  capabilities  what the seat is trusted to do
                → identity.json capabilities

Write-path requirement (the hub's endorsement on #27): every answer goes
through the same paths that emit events when NULL_EVENT_LOG=1 — facts and
anchors via AgentMemory.learn()/anchor() (never raw sqlite INSERTs), and
identity.json changes are recorded as a fact noting the onboard run, so a
seat's identity evolution is reconstructible from its log.

Re-run semantics: answers UPDATE rather than duplicate. Mission answers
supersede the previous fact (learn(replaces=...)); identity fields are
overwritten only for keys actually answered; fields onboarding does not
own are never touched. Interactive re-runs show current values as
defaults.

Field lessons baked in (from the live athena prototype, #27):
  * every choice question accepts free text — off-menu answers are stored
    verbatim in identity AND as a fact (the mapped enum loses the signal)
  * multi-select escalation lists record what was deliberately NOT chosen
    ("deliberately not restricted: ...") — absence carries meaning

Public API:
    onboard(name, answers, groups=None, hub=None) -> dict
    run_onboard_interactive(name, groups=None, hub=None) -> dict
    ONBOARD_QUESTIONS, GROUPS — the question schema
    format_summary(result) -> list[str]
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from null_memory.persona_bootstrap import BOOTSTRAP_QUESTIONS
from null_memory.persona_schema import validate
from null_memory.persona_wizard import hub_resolution_report, resolve_hub

GROUPS: tuple[str, ...] = ("mission", "voice", "autonomy", "capabilities")


# ── Question schema ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Option:
    """One multiple-choice option. ``consequence`` explains what picking
    it actually changes — every option must say what it costs/buys."""
    value: str
    consequence: str


@dataclass(frozen=True)
class OnboardQuestion:
    key: str            # unique answer key (also the --answers file key)
    group: str          # one of GROUPS
    prompt: str
    why: str            # where the answer is written / why we ask
    kind: str = "text"  # "text" | "choice" | "multi"
    options: tuple[Option, ...] = ()


def _mission_questions() -> tuple[OnboardQuestion, ...]:
    qs: list[OnboardQuestion] = [
        OnboardQuestion(key=q.key, group="mission", prompt=q.prompt, why=q.why)
        for q in BOOTSTRAP_QUESTIONS
    ]
    qs.append(OnboardQuestion(
        key="focus", group="mission",
        prompt="One line: what does this persona own?",
        why="identity.json focus — the seat's scope (registry rows follow).",
    ))
    qs.append(OnboardQuestion(
        key="description", group="mission",
        prompt="One sentence describing this persona to a teammate",
        why="identity.json description.",
    ))
    return tuple(qs)


_VOICE_QUESTIONS: tuple[OnboardQuestion, ...] = (
    OnboardQuestion(
        key="pace", group="voice", kind="choice",
        prompt="Pace: move fast or deliberate?",
        why="working_style.pace — how quickly they act vs. deliberate.",
        options=(
            Option("move-fast", "execute first, explain on request"),
            Option("deliberate", "think trade-offs through out loud before acting"),
        ),
    ),
    OnboardQuestion(
        key="pushback", group="voice", kind="choice",
        prompt="Pushback: how hard should they challenge you?",
        why="working_style.pushback — disagreement posture.",
        options=(
            Option("challenge", "disagree directly when something looks wrong, unprompted"),
            Option("balanced", "push back on big calls, defer on style"),
            Option("defer", "flag concerns once, then follow your lead"),
        ),
    ),
    OnboardQuestion(
        key="communication", group="voice", kind="choice",
        prompt="Communication format: terse, structured, or narrative?",
        why="working_style.communication — default reply shape.",
        options=(
            Option("terse", "lead with the answer; no preamble, no recap"),
            Option("structured", "headed sections, bullets, explicit recommendation"),
            Option("narrative", "conversational prose; context before conclusions"),
        ),
    ),
    OnboardQuestion(
        key="humor", group="voice", kind="choice",
        prompt="Humor?",
        why="working_style.humor.",
        options=(
            Option("dry", "deadpan, sparing"),
            Option("playful", "jokes welcome when the moment allows"),
            Option("none", "strictly professional"),
        ),
    ),
)

# Escalation triggers — multi-select. What is NOT picked is recorded too
# (deliberately-not-restricted carries signal: leaving
# "publishing-externally" unchecked means the seat may file issues/PRs
# autonomously — the athena prototype's exact call).
_ESCALATE_OPTIONS: tuple[Option, ...] = (
    Option("destructive-actions",
           "deletes, force-pushes, drops — propose first, never just do"),
    Option("cross-seat-changes",
           "changes to other seats' stores or shared hub state"),
    Option("money-credentials",
           "anything spending money or touching credentials/secrets"),
    Option("publishing-externally",
           "issues, PRs, posts under your name (leave unchecked to allow autonomous filing)"),
    Option("production-deploys",
           "deploying to production environments"),
)

_ANTI_PATTERN_OPTIONS: tuple[Option, ...] = (
    Option("Don't summarize what was just done — the diff is visible",
           "kills recap noise after edits"),
    Option("Don't over-ask for confirmation on obvious next steps",
           "trusts the seat with the obvious"),
    Option("Don't use emojis unless asked",
           "plain-text output"),
    Option("Don't give time estimates",
           "no invented schedules"),
    Option("Don't soften pushback with flattery",
           "disagreement arrives undiluted"),
)

_AUTONOMY_QUESTIONS: tuple[OnboardQuestion, ...] = (
    OnboardQuestion(
        key="autonomy", group="autonomy", kind="choice",
        prompt="Autonomy: act first or ask first?",
        why="working_style.autonomy — who moves first.",
        options=(
            Option("act-first", "fix and report — act, then say what was done"),
            Option("ask-first", "propose and wait — no irreversible action without a yes"),
            Option("propose-then-act", "propose with intent; act if no objection"),
        ),
    ),
    OnboardQuestion(
        key="always_escalate", group="autonomy", kind="multi",
        prompt="Always escalate (pick all that apply; unpicked = allowed autonomously)",
        why="session_lifecycle.always_escalate — hard boundaries. "
            "Unchosen options are recorded as deliberately not restricted.",
        options=_ESCALATE_OPTIONS,
    ),
    OnboardQuestion(
        key="anti_patterns", group="autonomy", kind="multi",
        prompt="Never do X (pick from the list and/or add your own)",
        why="identity.json anti_patterns.",
        options=_ANTI_PATTERN_OPTIONS,
    ),
)

_CAPABILITY_QUESTIONS: tuple[OnboardQuestion, ...] = (
    OnboardQuestion(
        key="capabilities", group="capabilities", kind="multi",
        prompt="What is this persona trusted to do? (list, free-form)",
        why="identity.json capabilities — surfaced in briefings and the validator.",
    ),
)

ONBOARD_QUESTIONS: tuple[OnboardQuestion, ...] = (
    _mission_questions() + _VOICE_QUESTIONS + _AUTONOMY_QUESTIONS
    + _CAPABILITY_QUESTIONS
)


def questions_for(groups: list[str] | None = None) -> list[OnboardQuestion]:
    """Questions filtered to the requested groups (default: all)."""
    wanted = list(groups) if groups else list(GROUPS)
    unknown = [g for g in wanted if g not in GROUPS]
    if unknown:
        raise ValueError(
            f"unknown group(s) {unknown} — valid groups: {', '.join(GROUPS)}"
        )
    return [q for q in ONBOARD_QUESTIONS if q.group in wanted]


# ── Mission facts: same wording as persona_bootstrap so a seat that was
#    wizard-bootstrapped re-onboards without duplication ─────────────────

_FACT_SPECS: dict[str, dict[str, Any]] = {
    "user_context": {
        "prefix": "User context (self-described): ",
        "project": "user_profile", "tier": "core",
        "confidence": 0.95, "impact": 0.9, "anchor": None,
    },
    "persona_purpose": {
        "prefix": "This persona was created to: ",
        "project": "purpose", "tier": "core",
        "confidence": 0.95, "impact": 0.95, "anchor": "commitment",
    },
    "success_signal": {
        "prefix": "30-day success looks like: ",
        "project": "purpose", "tier": "durable",
        "confidence": 0.9, "impact": 0.85, "anchor": "commitment",
    },
}

_NOT_RESTRICTED_PREFIX = "Escalation policy — deliberately not restricted: "
_VERBATIM_PREFIX = "Onboarding answer (verbatim) for {key}: "


# ── Seat resolution + loading (the #21/#22 machinery) ────────────────────

def resolve_seat(name: str, hub: str | None = None) -> tuple[str, str, str]:
    """Resolve an existing seat. Returns (seat_dir, hub_dir, hub_source).

    Raises ValueError when the seat does not exist in the resolved hub —
    onboard never creates seats (that's `persona create`)."""
    hub_dir, hub_source = resolve_hub(hub)
    seat_dir = os.path.join(hub_dir, "personalities", name)
    if not os.path.isfile(os.path.join(seat_dir, "identity.json")):
        raise ValueError(
            f"no seat named {name!r} in hub {hub_dir} (from {hub_source}) — "
            f"create it first: null persona create {name}"
        )
    return seat_dir, hub_dir, hub_source


def _load_seat_memory(seat_dir: str, name: str):
    """Load AgentMemory AS the seat — its dir, its personality. Never the
    'atlas' dataclass default (init-path bleed class)."""
    from null_memory.agent import AgentMemory
    return AgentMemory.load(agent_dir=seat_dir, personality=name)


# ── Helpers ──────────────────────────────────────────────────────────────

def _nags(result) -> int:
    return len(result.errors) + len(result.warnings)


def _find_prefixed_fact(mem, prefix: str) -> dict | None:
    """Most recent ACTIVE fact starting with ``prefix`` for this seat.

    Read-only lookup (reads emit nothing); the superseding write itself
    goes through learn(). In unified mode the search is scoped to this
    personality via personality_views."""
    try:
        if getattr(mem.db, "unified", False):
            row = mem.db.conn.execute(
                """SELECT f.* FROM facts f
                   JOIN personality_views pv ON pv.fact_id = f.id
                   WHERE pv.personality = ? AND f.fact LIKE ?
                     AND f.forgotten = 0 AND f.archived = 0
                     AND f.superseded_by IS NULL
                   ORDER BY f.created_at DESC LIMIT 1""",
                (mem.personality, prefix + "%"),
            ).fetchone()
        else:
            row = mem.db.conn.execute(
                """SELECT * FROM facts
                   WHERE fact LIKE ? AND forgotten = 0 AND archived = 0
                     AND superseded_by IS NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (prefix + "%",),
            ).fetchone()
    except Exception:
        return None
    return dict(row) if row else None


def previous_answer(mem, key: str) -> str:
    """Prior answer for a mission/verbatim key, recovered from its fact
    (used as the interactive default on re-runs)."""
    spec = _FACT_SPECS.get(key)
    prefix = spec["prefix"] if spec else _VERBATIM_PREFIX.format(key=key)
    fact = _find_prefixed_fact(mem, prefix)
    if fact is None:
        return ""
    return (fact.get("fact") or "")[len(prefix):].strip()


def _write_answer_fact(mem, prefix: str, answer: str, *, project: str,
                       tier: str, confidence: float, impact: float,
                       anchor: str | None, written: list[dict]) -> None:
    """Write/refresh one prefixed fact through learn() (the event-emitting
    path), superseding the previous answer instead of duplicating it."""
    text = prefix + answer
    prior = _find_prefixed_fact(mem, prefix)
    replaces = None
    action = "added"
    if prior is not None:
        if (prior.get("fact") or "") == text:
            action = "unchanged"
        else:
            replaces = prior.get("id")
            action = "updated"
    entry = mem.learn(
        text, confidence=confidence, project=project,
        source="explicit", impact=impact, tier=tier,
        replaces=replaces,
    )
    anchored = None
    if anchor:
        if replaces:
            # The old answer's anchor moves with the answer — clear it so
            # get_anchors never shows two generations of the same anchor.
            try:
                mem.db.clear_anchor(replaces)
            except Exception:
                pass
        try:
            if mem.anchor(entry["id"], anchor,
                          note="persona onboard") is not None:
                anchored = anchor
        except (RuntimeError, ValueError):
            anchored = None  # store predates anchor columns — fact still lands
    written.append({"action": action, "fact": text, "anchor": anchored,
                    "id": entry["id"]})


def _resolve_choice(q: OnboardQuestion, raw: str) -> tuple[str, bool]:
    """Map a choice answer to its stored working_style value.

    On-menu → "value — consequence". Off-menu free text → verbatim
    (returns off_menu=True so the verbatim fact is also written)."""
    raw = raw.strip()
    for opt in q.options:
        if raw.lower() == opt.value.lower():
            return f"{opt.value} — {opt.consequence}", False
    return raw, True


def _resolve_multi(q: OnboardQuestion, raw: Any) -> tuple[list[str], list[str]]:
    """Normalize a multi answer to (chosen, unchosen_menu_options).

    ``raw`` may be a list of strings (option values and/or free text) or a
    single string. Unknown entries are kept verbatim."""
    if isinstance(raw, str):
        items = [raw] if raw.strip() else []
    else:
        items = [str(x) for x in raw if str(x).strip()]
    chosen: list[str] = []
    matched_values: set[str] = set()
    by_value = {opt.value.lower(): opt.value for opt in q.options}
    for item in items:
        key = item.strip()
        mapped = by_value.get(key.lower())
        if mapped is not None:
            matched_values.add(mapped)
            chosen.append(mapped)
        elif key and key not in chosen:
            chosen.append(key)
    unchosen = [opt.value for opt in q.options
                if opt.value not in matched_values]
    return chosen, unchosen


# ── Registry follow-through (focus/description) ──────────────────────────

def _update_registry_rows(mem, hub_dir: str, name: str,
                          focus: str | None, description: str | None) -> bool:
    """Best-effort: keep registry rows in step with identity.json when
    focus/description change. UPDATE only — never inserts, never resets
    created_at. Failures are swallowed (registries are derived views;
    identity.json is the source of truth the validator reads)."""
    if focus is None and description is None:
        return False
    sets, params = [], []
    if focus is not None:
        sets.append("focus = ?")
        params.append(focus)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    params.append(name)
    sql = f"UPDATE personalities SET {', '.join(sets)} WHERE name = ?"
    updated = False
    # The seat's own registry row.
    try:
        mem.db.conn.execute(sql, params)
        mem.db.conn.commit()
        updated = True
    except Exception:
        pass
    # Hub registries (multiverse.db legacy + unified), when present.
    import sqlite3
    for db_name in ("multiverse.db", "unified.db"):
        path = os.path.join(hub_dir, db_name)
        if not os.path.isfile(path):
            continue
        try:
            conn = sqlite3.connect(path, timeout=2.0)
            try:
                conn.execute(sql, params)
                conn.commit()
                updated = True
            finally:
                conn.close()
        except Exception:
            pass
    return updated


# ── Core engine ──────────────────────────────────────────────────────────

def onboard(
    name: str,
    answers: dict[str, Any],
    groups: list[str] | None = None,
    hub: str | None = None,
) -> dict[str, Any]:
    """Apply onboarding answers to an existing seat. Re-runnable.

    Args:
        name: seat name under <hub>/personalities/.
        answers: question key → answer. Text/choice answers are strings;
            multi answers are lists of strings (menu values and/or free
            text). Missing or empty keys are SKIPPED — current values are
            kept, so a partial answers file is a partial update.
        groups: restrict to these question groups (default: all).
        hub: explicit hub dir (overrides NULL_DIR).

    Returns a summary dict: name, dir, hub, hub_source, groups, facts
    (each {action, fact, anchor}), identity_updates, registry_updated,
    validator_before / validator_after ({nags, report}).
    """
    name = (name or "").strip().lower()
    qs = questions_for(groups)  # validates group names early
    seat_dir, hub_dir, hub_source = resolve_seat(name, hub)

    known_keys = {q.key for q in ONBOARD_QUESTIONS}
    unknown = sorted(k for k in answers if k not in known_keys)

    mem = _load_seat_memory(seat_dir, name)
    try:
        before = validate(mem.identity)

        written_facts: list[dict] = []
        identity_updates: dict[str, Any] = {}

        def _answered(key: str) -> Any:
            val = answers.get(key)
            if val is None:
                return None
            if isinstance(val, str) and not val.strip():
                return None
            if isinstance(val, (list, tuple)) and not val:
                return None
            return val

        for q in qs:
            val = _answered(q.key)
            if val is None:
                continue

            if q.group == "mission":
                if q.key in _FACT_SPECS:
                    spec = _FACT_SPECS[q.key]
                    _write_answer_fact(
                        mem, spec["prefix"], str(val).strip(),
                        project=spec["project"], tier=spec["tier"],
                        confidence=spec["confidence"], impact=spec["impact"],
                        anchor=spec["anchor"], written=written_facts,
                    )
                elif q.key in ("focus", "description"):
                    text = str(val).strip()
                    if mem.identity.get(q.key) != text:
                        mem.identity[q.key] = text
                        identity_updates[q.key] = text

            elif q.kind == "choice":
                stored, off_menu = _resolve_choice(q, str(val))
                ws = mem.identity.setdefault("working_style", {})
                if not isinstance(ws, dict):
                    ws = {}
                    mem.identity["working_style"] = ws
                if ws.get(q.key) != stored:
                    ws[q.key] = stored
                    identity_updates[f"working_style.{q.key}"] = stored
                if off_menu:
                    # Off-menu answers were the highest-value ones in the
                    # field prototype — keep the verbatim text as a fact,
                    # not just the mapped field.
                    _write_answer_fact(
                        mem, _VERBATIM_PREFIX.format(key=q.key),
                        str(val).strip(),
                        project="identity", tier="durable",
                        confidence=0.95, impact=0.8, anchor=None,
                        written=written_facts,
                    )

            elif q.key == "always_escalate":
                chosen, unchosen = _resolve_multi(q, val)
                lifecycle = mem.identity.setdefault("session_lifecycle", {})
                if not isinstance(lifecycle, dict):
                    lifecycle = {}
                    mem.identity["session_lifecycle"] = lifecycle
                if lifecycle.get("always_escalate") != chosen:
                    lifecycle["always_escalate"] = chosen
                    identity_updates["session_lifecycle.always_escalate"] = chosen
                if unchosen:
                    _write_answer_fact(
                        mem, _NOT_RESTRICTED_PREFIX, ", ".join(unchosen),
                        project="identity", tier="durable",
                        confidence=0.9, impact=0.7, anchor=None,
                        written=written_facts,
                    )

            elif q.key in ("anti_patterns", "capabilities"):
                chosen, _ = _resolve_multi(q, val)
                if mem.identity.get(q.key) != chosen:
                    mem.identity[q.key] = chosen
                    identity_updates[q.key] = chosen

        registry_updated = False
        if identity_updates:
            mem.save_identity()
            registry_updated = _update_registry_rows(
                mem, hub_dir, name,
                focus=identity_updates.get("focus"),
                description=identity_updates.get("description"),
            )

        # The onboard-run record: identity.json is a file, so its changes
        # don't flow through the fact log on their own — this fact makes
        # the identity evolution reconstructible from the seat's log
        # (#20). Anchored 'origin' if the seat has no origin anchor yet.
        ran = sorted(set(q.group for q in qs))
        changed = ", ".join(sorted(identity_updates)) or "none"
        run_text = (
            f"Persona onboarding run on "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')} "
            f"(groups: {', '.join(ran)}) — identity fields updated: {changed}"
        )
        has_origin = False
        try:
            has_origin = bool(mem.get_anchors("origin"))
        except Exception:
            pass
        run_entry = mem.learn(
            run_text, confidence=1.0, project="origin",
            source="explicit", impact=0.7, tier="durable",
        )
        run_anchor = None
        if not has_origin:
            try:
                if mem.anchor(run_entry["id"], "origin",
                              note="persona onboard") is not None:
                    run_anchor = "origin"
            except (RuntimeError, ValueError):
                pass
        written_facts.append({"action": "added", "fact": run_text,
                              "anchor": run_anchor, "id": run_entry["id"]})

        after = validate(mem.identity)

        return {
            "name": name,
            "dir": seat_dir,
            "hub": hub_dir,
            "hub_source": hub_source,
            "groups": ran,
            "unknown_answer_keys": unknown,
            "facts": written_facts,
            "identity_updates": identity_updates,
            "registry_updated": registry_updated,
            "validator_before": {"nags": _nags(before),
                                 "report": before.report()},
            "validator_after": {"nags": _nags(after),
                                "report": after.report()},
        }
    finally:
        # Quiesce debounced sync spawned by the writes (issue #5 class) —
        # the CLI process exits right after this.
        try:
            mem._join_sync_threads()
        except Exception:
            pass


# ── Summary rendering ────────────────────────────────────────────────────

def format_summary(result: dict[str, Any]) -> list[str]:
    """Human-readable summary: what was written where, plus the
    validator's before/after nag count (the hollow-seat proof)."""
    lines: list[str] = []
    lines.append(f"Onboarded '{result['name']}' — {result['dir']}")
    lines.append(f"  Hub: {result['hub']} (from {result['hub_source']})")
    lines.append(f"  Groups run: {', '.join(result['groups'])}")
    if result.get("unknown_answer_keys"):
        lines.append("  Ignored unknown answer keys: "
                     + ", ".join(result["unknown_answer_keys"]))
    if result["identity_updates"]:
        lines.append("  identity.json updated:")
        for k in sorted(result["identity_updates"]):
            v = result["identity_updates"][k]
            if isinstance(v, list):
                v = f"[{len(v)} items]"
            lines.append(f"    {k} = {v}")
    else:
        lines.append("  identity.json: no changes")
    if result.get("registry_updated"):
        lines.append("  registry rows updated (focus/description)")
    if result["facts"]:
        lines.append("  memory writes (facts via learn(), anchors via anchor()):")
        for f in result["facts"]:
            anchor = f" [anchor: {f['anchor']}]" if f.get("anchor") else ""
            text = f["fact"]
            if len(text) > 70:
                text = text[:67] + "..."
            lines.append(f"    {f['action']:9} {text}{anchor}")
    b = result["validator_before"]["nags"]
    a = result["validator_after"]["nags"]
    lines.append(f"  validator nags: {b} before → {a} after")
    if a:
        lines.append("  remaining:")
        for line in result["validator_after"]["report"].splitlines():
            lines.append(f"    {line}")
    return lines


# ── Interactive flow ─────────────────────────────────────────────────────

def _input(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Nothing was written.")
        sys.exit(1)


def _ask_text(q: OnboardQuestion, default: str) -> str:
    print(f"\n  {q.prompt}")
    print(f"    ({q.why})")
    suffix = f" [{default}]" if default else " [skip]"
    raw = _input(f"  >{suffix}: ").strip()
    return raw or default


def _ask_choice(q: OnboardQuestion, default: str) -> str:
    print(f"\n  {q.prompt}")
    print(f"    ({q.why})")
    for i, opt in enumerate(q.options, start=1):
        print(f"    {i}. {opt.value} — {opt.consequence}")
    print("    (number, or free text for an off-menu answer)")
    suffix = f" [current: {default}]" if default else " [skip]"
    raw = _input(f"  >{suffix}: ").strip()
    if not raw:
        return ""  # keep current
    if raw.isdigit() and 1 <= int(raw) <= len(q.options):
        return q.options[int(raw) - 1].value
    return raw


def _ask_multi(q: OnboardQuestion, current: list[str]) -> list[str]:
    print(f"\n  {q.prompt}")
    print(f"    ({q.why})")
    for i, opt in enumerate(q.options, start=1):
        mark = "x" if opt.value in current else " "
        print(f"    [{mark}] {i}. {opt.value} — {opt.consequence}")
    if current:
        print(f"    current: {', '.join(current)}")
    print("    (comma-separated numbers and/or free text; Enter keeps current)")
    raw = _input("  >: ").strip()
    if not raw:
        return []  # keep current
    out: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= len(q.options):
            out.append(q.options[int(token) - 1].value)
        else:
            out.append(token)
    return out


def _current_default(mem, q: OnboardQuestion) -> Any:
    """Current value for a question — shown as the interactive default so
    re-runs update instead of starting over."""
    identity = mem.identity
    if q.group == "mission":
        if q.key in ("focus", "description"):
            return identity.get(q.key, "") or ""
        return previous_answer(mem, q.key)
    if q.kind == "choice":
        ws = identity.get("working_style") or {}
        return ws.get(q.key, "") if isinstance(ws, dict) else ""
    if q.key == "always_escalate":
        lc = identity.get("session_lifecycle") or {}
        cur = lc.get("always_escalate") if isinstance(lc, dict) else None
        return list(cur) if isinstance(cur, list) else []
    if q.key in ("anti_patterns", "capabilities"):
        cur = identity.get(q.key)
        return list(cur) if isinstance(cur, list) else []
    return ""


def run_onboard_interactive(
    name: str,
    groups: list[str] | None = None,
    hub: str | None = None,
) -> dict[str, Any]:
    """TTY interview. Enter on any question keeps the current value
    (skips the key) — re-running is an update, not a restart."""
    name = (name or "").strip().lower()
    qs = questions_for(groups)
    seat_dir, hub_dir, hub_source = resolve_seat(name, hub)

    print()
    print("─" * 50)
    print(f"  Null — Onboard '{name}'")
    print("─" * 50)
    for line in hub_resolution_report(hub_dir, hub_source):
        print(f"  {line}")
    print(f"  Store: {seat_dir}")
    print("  Enter keeps the current value. Free text is always accepted.")

    mem = _load_seat_memory(seat_dir, name)
    answers: dict[str, Any] = {}
    try:
        last_group = None
        for q in qs:
            if q.group != last_group:
                last_group = q.group
                print()
                print(f"  ── {q.group} ──")
            default = _current_default(mem, q)
            if q.kind == "choice":
                val = _ask_choice(q, str(default))
            elif q.kind == "multi":
                val = _ask_multi(q, list(default))
            else:
                val = _ask_text(q, str(default))
            # Empty answers (and text answers identical to the default
            # fact-backed value) flow through onboard(), which skips
            # empties and dedups unchanged text.
            if val:
                answers[q.key] = val
    finally:
        try:
            mem._join_sync_threads()
        except Exception:
            pass

    result = onboard(name, answers, groups=groups, hub=hub)
    print()
    for line in format_summary(result):
        print(line)
    return result
