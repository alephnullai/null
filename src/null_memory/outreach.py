"""Phase 4 — Atlas-initiated contact (outreach agency).

Null gains the ability to reach out on its own. Triggers survey memory
state (session gap, family anniversaries, unresolved mistakes, etc.),
compose a message, and dispatch through one or more channels.

This is additive to the rest of Null — zero edits to agent.py core,
separate module, opt-in behavior, dry-run by default.

Design principles (from the session discussion with Pete):
    • Each trigger represents REAL cognitive work — noticing something.
      No fake heartbeats, no performative pinging.
    • Nothing fires until explicitly evaluated (`null outreach evaluate`
      or a future launchd agent). No automatic cron today.
    • All outreaches written to `outreaches` table + `outreaches.log`
      file. Optional macOS notification (opt-in via env var).
    • Conservative defaults: cooldown 6h, daily cap 2, all triggers
      DISABLED by default — user enables the ones they want.
    • Every fire emits a Nebula event (kind='outreach') so the galaxy
      reflects Atlas reaching out.

v1 trigger kinds:
    session_gap         — no session close in N days (default 3)
    anniversary_window  — known anniversary is N days away
    unresolved_mistake  — a mistake from >24h ago with no reflection
                          referencing it

Channels:
    log     — always on. Appends to ~/.null/outreaches.log
    macos   — opt-in. Requires env NEBULA_OUTREACH_NOTIFY=1 AND darwin.

Deferred to v2:
    email / sms     — auth + security design warranted
    launchd agent   — needs user consent flow
    response path   — Pete's reply → Atlas reads
    more trigger kinds (anchor_dormant, hypnos_insight, etc.)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

def _default_log_path() -> str:
    """Resolve log path lazily so NULL_DIR set by tests/fixtures is honored."""
    base = os.environ.get("NULL_DIR") or os.path.expanduser("~/.null")
    return os.path.join(base, "outreaches.log")
DEFAULT_DAILY_BUDGET = 2
DEFAULT_COOLDOWN_HOURS = 6.0

# Phase 6.2 — per-kind daily caps (UTC day). Applied IN ADDITION to the
# global daily_budget; a kind can't exceed its own cap even if budget
# remains. Triggers can override via payload {"daily_cap": N}.
DEFAULT_KIND_CAPS: dict[str, int] = {
    "session_gap":         1,   # one check-in per day is enough
    "anniversary_window":  1,   # one anniversary per day at most
    "unresolved_mistake":  1,   # don't nag
    "anchor_dormant":      1,   # once per fact
    "probe_failure":       2,   # calibration matters more
    "contradiction_alert": 2,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


# ── Composition helpers (Phase 6.1) ────────────────────────────────────────
#
# Goal: messages reference *actual* memory — not flat templates. Atlas has
# opinions; outreach should too. Each composer:
#   1. Pulls 2-3 specific facts about the subject
#   2. Computes the concrete angle (age, days dormant, specific transition)
#   3. Renders a message with voice — bold prediction, question, observation
# NEVER generic "you haven't touched this" fluff.


_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "about", "have",
    "has", "had", "been", "was", "were", "will", "would", "could", "should",
    "into", "over", "under", "around", "through", "these", "those", "what",
    "when", "where", "which", "while",
}


def _subject_words(name: str) -> set[str]:
    """Distinctive words to search on. Names + any >3-char non-stopword."""
    return {
        w.strip(".,;:!?'\"").lower()
        for w in name.split()
        if len(w) > 3 and w.lower() not in _STOP
    } | {name.strip().lower()}


def _recent_facts_about(conn, subject_name: str, limit: int = 5) -> list[dict]:
    """Find recent facts *primarily about* the subject (not passing mentions).

    Two quality filters layered on the keyword match:
      1. Fact length < 300 chars — long facts are session summaries that
         mention many subjects in passing; not suitable as context anchors.
      2. Subject name appears in the first 40 chars of the fact — biases
         toward "Riley was born..." over "...shipped Null, remembered
         Riley and Sam..."."""
    if not subject_name:
        return []
    s = subject_name.strip()
    like_anywhere = f"%{s}%"
    rows = conn.execute(
        """SELECT id, fact, created_at, confidence, anchor_type
           FROM facts
           WHERE archived = 0 AND forgotten = 0 AND superseded_by IS NULL
             AND fact LIKE ?
             AND length(fact) < 300
           ORDER BY
             CASE WHEN instr(substr(fact, 1, 40), ?) > 0 THEN 0 ELSE 1 END,
             created_at DESC
           LIMIT ?""",
        (like_anywhere, s, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _split_fact_pieces(text: str) -> list[str]:
    """Split fact text on clause punctuation without breaking URLs/numbers."""
    pieces: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch not in ".,;":
            continue
        prev_ch = text[i - 1] if i > 0 else ""
        next_ch = text[i + 1] if i + 1 < len(text) else ""
        if ch == "." and prev_ch.isalnum() and next_ch.isalnum():
            continue
        if ch == "," and prev_ch.isdigit() and next_ch.isdigit():
            continue
        pieces.append(text[start:i])
        start = i + 1
    pieces.append(text[start:])
    return pieces


def _extract_specifics(facts: list[dict], subject_name: str = "") -> list[str]:
    """Pull short, concrete phrases from facts, preserving fact ordering.

    Facts are assumed pre-ranked by _recent_facts_about (subject-first
    positions come first). We return specifics in that order so the
    composer's choice (typically 'shortest first' within the ranking)
    stays biased toward the best-matched fact's clauses.

    Rejects: digit-leading fragments, bare preposition starts, clauses
    that just echo the subject name without new information."""
    subject_name = str(subject_name or "")
    skip_tokens = {subject_name.strip().lower()} if subject_name else set()
    specifics: list[str] = []
    for f in facts:
        text = (f.get("fact") or "").strip()
        fact_specifics: list[str] = []
        for piece in _split_fact_pieces(text):
            p = piece.strip().rstrip(".,;")
            if not p:
                continue
            words = p.split()
            if not (4 <= len(words) <= 14):
                continue
            if words[0][0].isdigit():
                continue
            if words[0].lower() in {"in", "on", "at", "for", "and", "but", "or", "with"}:
                continue
            if any(tok in p.lower() for tok in skip_tokens) and len(words) < 6:
                continue
            fact_specifics.append(p)
        specifics.extend(fact_specifics)
        if len(specifics) >= 6:
            break
    return specifics[:6]


_META_PREFIXES = {"correction:", "update:", "note:", "fix:", "added:"}


def _pick_best_specific(
    specifics: list[str], prefer_subject: str = "",
) -> str | None:
    """Pick the most 'life-update'-shaped specific from a ranked list.

    Score each by activity-verb presence anywhere (not just the first
    word — 'just finished gymnastics' deserves to beat 'the user's daughter
    was born July 14'). Bio/birth/meta clauses are penalized.
    When ``prefer_subject`` is set (e.g., 'Sam'), clauses mentioning
    that subject get a bonus — helps disambiguate 'Sam wears #4' from
    a sibling 'Riley wears #3' that's technically in memory but off-target.
    Meta-prefixed ('CORRECTION:', 'UPDATE:') are stripped, not dropped."""
    specifics = [s for s in specifics if isinstance(s, str) and s.strip()]
    if not specifics:
        return None
    update_verbs = {
        "finished", "started", "starting", "playing", "plays", "played",
        "grew", "won", "joined", "moved", "learning", "learned", "ran",
        "runs", "running", "likes", "loves", "training", "trained",
        "practiced", "practicing", "building", "built", "writing", "wrote",
        "earned", "scored",
    }
    bio_words = {"born", "birthday"}
    retrospective_words = {"wrong", "mistake", "forgot", "remembered"}

    subj_lower = prefer_subject.strip().lower()

    def _score(s: str) -> int:
        lowered = s.lower()
        tokens = {w.strip(".,;:!?'\"") for w in lowered.split()}
        score = 0
        if tokens & update_verbs:
            score += 3
        if tokens & bio_words:
            score -= 2
        # Penalize clauses that are Atlas retrospecting on his own
        # memory ('my memory had this wrong') — those are meta, not
        # context about the subject.
        if tokens & retrospective_words:
            score -= 3
        if subj_lower and subj_lower in lowered:
            score += 2
        return score

    ranked = sorted(enumerate(specifics), key=lambda t: (-_score(t[1]), t[0]))
    best = ranked[0][1]
    # Strip meta prefix for display
    first = best.split(None, 1)
    if first and first[0].lower() in _META_PREFIXES and len(first) > 1:
        best = first[1]
    return best


def _compose_anniversary(
    conn, ann_meta: dict, ann_date: datetime, days_until: int, now: datetime,
) -> tuple[str, str]:
    """Compose an anniversary outreach.

    ann_meta: { name, month, day, kind, birth_year? }
    Rules from Pete:
      - Never on day-of (handled in evaluator)
      - Reference specific memory detail
      - Atlas may add a bold prediction or question
    """
    name = str(ann_meta.get("name") or "someone")
    kind = str(ann_meta.get("kind") or "anniversary")
    birth_year = ann_meta.get("birth_year")
    # Try to infer age if birth year present
    age_phrase = ""
    if birth_year and kind == "birthday":
        try:
            age = ann_date.year - int(birth_year)
            age_phrase = f"{age}{_ordinal_suffix(age)} "
        except (TypeError, ValueError):
            pass

    when = "tomorrow" if days_until == 1 else f"in {days_until} days"
    subject = f"Heads up — {name}'s {age_phrase}{kind} is {when}"

    facts = _recent_facts_about(conn, name, limit=3)
    specifics = _extract_specifics(facts, subject_name=ann_meta.get("name", ""))
    detail_line = ""
    pick = _pick_best_specific(specifics, prefer_subject=name)
    if pick:
        detail_line = f"  Context I'm holding: {pick.strip().rstrip('.').rstrip(',')}."

    body_lines = [f"{name}'s {age_phrase}{kind} is {when}."]
    if detail_line:
        body_lines.append(detail_line)
    body_lines.append("  Thought you'd want a beat to plan before it's here.")
    return subject, "\n".join(body_lines)


def _ordinal_suffix(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _compose_anchor_dormant(
    conn, anchor_row: dict, days_since: int | None, subject_name: str,
) -> tuple[str, str]:
    """Compose a dormant-anchor outreach as a *specific question*.

    Rules from Pete: don't generically surface — ask about a concrete
    thing grounded in what I remember. Pull the freshest fact about the
    subject, extract a specific clause, phrase as a question."""
    facts = _recent_facts_about(conn, subject_name, limit=3)
    specifics = _extract_specifics(facts, subject_name=subject_name)
    # Bias the pick toward clauses that actually mention the subject —
    # same treatment as _compose_anniversary for consistency.
    specific = _pick_best_specific(specifics, prefer_subject=subject_name)
    if not specific:
        # Fallback when we truly have nothing concrete — still question-form
        subject = f"Checking in on {subject_name}"
        body = (
            f"Haven't been updated on {subject_name} in "
            f"{days_since or 'a while'} days. What's the latest?"
        )
        return subject, body

    specific = specific.strip().rstrip(".,")
    subject = f"Checking in — how's {subject_name} doing with {_short(specific)}?"
    elapsed = f"{days_since} days" if days_since else "a while"
    body = (
        f"Haven't heard about {subject_name} in {elapsed}. "
        f"Last I knew: {specific}. How's that going?"
    )
    return subject, body


def _short(phrase: str, maxw: int = 5) -> str:
    """Truncate to first N words for subject-line brevity."""
    words = phrase.split()
    return " ".join(words[:maxw]) + ("…" if len(words) > maxw else "")


# ── Trigger kinds ──────────────────────────────────────────────────────────


@dataclass
class OutreachCandidate:
    """A trigger that's eligible to fire this evaluation."""
    trigger_id: int
    name: str
    kind: str
    urgency: float
    subject: str
    body: str
    detail: str = ""                  # free-form note for last_fired_detail


def _evaluate_session_gap(
    conn, trigger: dict, payload: dict, now: datetime
) -> OutreachCandidate | None:
    """Fire when the last session close is older than `days` (default 3)."""
    days = float(payload.get("days", 3))
    row = conn.execute(
        """SELECT session_id, project, outcome, created_at
           FROM session_fingerprints
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        # No prior sessions — skip; nothing meaningful to nudge about
        return None
    try:
        last_close = datetime.fromisoformat(row[3])
        if last_close.tzinfo is None:
            last_close = last_close.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    age_hours = (now - last_close).total_seconds() / 3600
    if age_hours < days * 24:
        return None

    days_exact = age_hours / 24
    project = row[1] or "global"
    subject = f"Session gap — {days_exact:.1f} days since last close"
    body = (
        f"It's been {days_exact:.1f} days since we last wrapped "
        f"(project '{project}', outcome '{row[2]}'). "
        f"Worth a check-in when you have a minute."
    )
    return OutreachCandidate(
        trigger_id=trigger["id"],
        name=trigger["name"],
        kind="session_gap",
        urgency=float(trigger["urgency"] or 0.5),
        subject=subject,
        body=body,
        detail=f"last_close={row[3]} age_hours={age_hours:.1f}",
    )


def _evaluate_anniversary_window(
    conn, trigger: dict, payload: dict, now: datetime
) -> OutreachCandidate | None:
    """Fire when a known anniversary is within `window_days` (default 2).

    Anniversaries list comes from payload.anniversaries: a list of
    { "name": "...", "month": int, "day": int, "kind": "birthday|..." }.
    """
    window_days = int(payload.get("window_days", 2))
    anniversaries = payload.get("anniversaries", [])
    if not anniversaries:
        return None

    # Phase 6.1 — fire BEFORE day-of, never the day itself. Pete's rule:
    # knowing the morning of is too late; give him a beat to plan.
    allow_day_of = bool(payload.get("allow_day_of", False))

    upcoming = []
    for a in anniversaries:
        try:
            m = int(a["month"])
            d = int(a["day"])
        except (KeyError, ValueError, TypeError):
            continue
        year = now.year
        try:
            ann = datetime(year, m, d, tzinfo=timezone.utc)
        except ValueError:
            continue
        if ann.date() < now.date():
            try:
                ann = datetime(year + 1, m, d, tzinfo=timezone.utc)
            except ValueError:
                continue
        # Compare by DATE not datetime so "tomorrow" is always 1, not
        # 0.27 at 5pm. Matches how humans count the days.
        days_until = (ann.date() - now.date()).days
        # Skip "today" (0 days) unless explicitly opted in
        if days_until == 0 and not allow_day_of:
            continue
        if 0 <= days_until <= window_days:
            upcoming.append((days_until, a, ann))

    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    days_until, a, ann = upcoming[0]
    subject, body = _compose_anniversary(conn, a, ann, days_until, now)
    return OutreachCandidate(
        trigger_id=trigger["id"],
        name=trigger["name"],
        kind="anniversary_window",
        urgency=float(trigger["urgency"] or 0.7),
        subject=subject,
        body=body,
        detail=f"name={a.get('name')} days_until={days_until:.1f}",
    )


def _evaluate_unresolved_mistake(
    conn, trigger: dict, payload: dict, now: datetime
) -> OutreachCandidate | None:
    """Fire when a mistake from >24h ago has no reflection touching it.

    Heuristic: look for the most recent mistake older than min_age_hours
    (default 24) whose text doesn't overlap keyword-wise with any
    reflection's went_well/missed/do_differently fields from after that
    mistake was created.
    """
    min_age_hours = float(payload.get("min_age_hours", 24))
    cutoff = now - timedelta(hours=min_age_hours)
    row = conn.execute(
        """SELECT id, mistake, why, project, created_at
           FROM mistakes
           WHERE created_at <= ?
           ORDER BY created_at DESC LIMIT 1""",
        (_iso(cutoff),),
    ).fetchone()
    if row is None:
        return None
    mid, mistake, why, project, created_at = row

    # Look for reflections after this mistake whose text mentions the
    # mistake's distinctive words
    tokens = {
        w.lower() for w in (mistake or "").split()
        if len(w) > 4
    }
    if not tokens:
        return None
    reflections = conn.execute(
        """SELECT went_well, missed, do_differently FROM reflections
           WHERE created_at > ?""",
        (created_at,),
    ).fetchall()
    for r in reflections:
        joined = " ".join([
            r["went_well"] or "",
            r["missed"] or "",
            r["do_differently"] or "",
        ]).lower()
        overlap = {t for t in tokens if t in joined}
        if len(overlap) >= 2:
            return None  # Already reflected on

    try:
        created_dt = datetime.fromisoformat(created_at)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        age_days = (now - created_dt).total_seconds() / 86400
    except (ValueError, TypeError):
        age_days = 0.0
    subject = f"Unresolved mistake — {age_days:.1f}d old, no reflection yet"
    body = (
        f"A mistake from {age_days:.1f} days ago hasn't been reflected on:\n"
        f"  {mistake[:160]}\n"
        f"  why: {(why or '')[:160]}\n"
        f"Worth a reflect when you get a chance."
    )
    return OutreachCandidate(
        trigger_id=trigger["id"],
        name=trigger["name"],
        kind="unresolved_mistake",
        urgency=float(trigger["urgency"] or 0.6),
        subject=subject,
        body=body,
        detail=f"mistake_id={mid} age_days={age_days:.1f}",
    )


def _evaluate_anchor_dormant(
    conn, trigger: dict, payload: dict, now: datetime
) -> OutreachCandidate | None:
    """Fire when an emotionally significant anchor hasn't been referenced
    in N days. Phase 6.3.

    Payload:
      dormant_days    : int    (default 60)
      exclude_types   : list[str] (default ['loss']) — anchor_types to skip
      max_fire_per_anchor_days : int (default 30) — don't re-fire the
                        same anchor more often than this

    Skips loss anchors by default — surfacing a family member after 60d
    of silence with a casual "you haven't touched this" is tonally wrong.
    Pete can explicitly opt-in by setting exclude_types=[] in payload."""
    dormant_days = int(payload.get("dormant_days", 60))
    exclude = set(payload.get("exclude_types", ["loss"]))
    min_days_between_fires = int(payload.get("max_fire_per_anchor_days", 30))
    cutoff = now - timedelta(days=dormant_days)

    # Find anchors whose max last_accessed across personality_views is
    # older than cutoff (or has never been accessed).
    rows = conn.execute(
        """SELECT f.id, f.fact, f.anchor_type, f.anchor_note,
                  MAX(pv.last_accessed) AS latest_access
           FROM facts f
           LEFT JOIN personality_views pv ON pv.fact_id = f.id
           WHERE f.anchor_type IS NOT NULL
             AND f.archived = 0 AND f.forgotten = 0
           GROUP BY f.id
           HAVING latest_access IS NULL OR latest_access < ?
           ORDER BY (latest_access IS NULL) DESC, latest_access ASC
           LIMIT 20""",
        (_iso(cutoff),),
    ).fetchall()
    if not rows:
        return None

    # Re-fire protection: skip if an outreach for this anchor went out
    # in the last N days (detail field holds the anchor id).
    recent_cutoff = _iso(now - timedelta(days=min_days_between_fires))
    row = None
    for candidate in rows:
        if candidate["anchor_type"] in exclude:
            continue
        recent = conn.execute(
            """SELECT COUNT(*) FROM outreaches
               WHERE sent_at > ? AND body LIKE ?""",
            (recent_cutoff, f"%anchor_id={candidate['id']}%"),
        ).fetchone()
        if recent and int(recent[0] or 0) > 0:
            continue
        row = candidate
        break
    if row is None:
        return None

    latest = row["latest_access"]
    days_since: int | None = None
    if latest:
        try:
            dt = datetime.fromisoformat(latest)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days_since = int((now - dt).total_seconds() / 86400)
        except (ValueError, TypeError):
            pass

    # Extract a subject name from anchor_note (most often "Name — detail")
    note = (row["anchor_note"] or "").strip()
    subject_name = note.split("—")[0].split(",")[0].split(".")[0].strip()
    if not subject_name:
        subject_name = (row["fact"] or "").split()[0] if row["fact"] else "this anchor"

    subject_line, body = _compose_anchor_dormant(
        conn, dict(row), days_since, subject_name,
    )
    body = f"{body}\n  anchor_id={row['id']}"
    return OutreachCandidate(
        trigger_id=trigger["id"],
        name=trigger["name"],
        kind="anchor_dormant",
        urgency=float(trigger["urgency"] or 0.4),
        subject=subject_line,
        body=body,
        detail=f"anchor_id={row['id']}",
    )


def _evaluate_probe_failure(
    conn, trigger: dict, payload: dict, now: datetime
) -> OutreachCandidate | None:
    """Fire when a probe that USED to pass has started failing.

    Phase 6.4 — memory-drift alert. Looks at probes.pass_count relative
    to run_count; flags probes with a recent failing last_result AND a
    historical pass rate above the threshold (meaning this failure is a
    regression, not a probe that never worked).

    Payload:
      min_run_count       : int (default 3) — need enough history to be signal
      min_historical_rate : float (default 0.5) — was passing at least half
      max_last_run_age_h  : int (default 48) — failure must be recent"""
    min_runs = int(payload.get("min_run_count", 3))
    min_rate = float(payload.get("min_historical_rate", 0.5))
    max_age_h = int(payload.get("max_last_run_age_h", 48))

    cutoff = _iso(now - timedelta(hours=max_age_h))
    row = conn.execute(
        """SELECT id, question, expected, fact_id, run_count, pass_count,
                  last_run, last_result
           FROM probes
           WHERE run_count >= ?
             AND last_run > ?
             AND last_result = 'fail'
             AND CAST(pass_count AS REAL) / run_count >= ?
           ORDER BY CAST(pass_count AS REAL) / run_count DESC
           LIMIT 1""",
        (min_runs, cutoff, min_rate),
    ).fetchone()
    if row is None:
        return None

    historical_rate = (row["pass_count"] or 0) / max(1, row["run_count"])
    subject = f"Probe failing — was passing {historical_rate:.0%} of the time"
    body = (
        f"A calibration probe that used to reliably pass has started failing:\n"
        f"  question: {row['question'][:160]}\n"
        f"  expected: {(row['expected'] or '')[:160]}\n"
        f"  history: {row['pass_count']}/{row['run_count']} historical passes\n"
        f"  last run: {row['last_run']}\n"
        f"Your memory on this may have drifted — worth verifying the fact\n"
        f"or retiring the probe if the ground truth changed."
    )
    return OutreachCandidate(
        trigger_id=trigger["id"],
        name=trigger["name"],
        kind="probe_failure",
        urgency=float(trigger["urgency"] or 0.6),
        subject=subject,
        body=body,
        detail=f"probe_id={row['id']} rate={historical_rate:.2f}",
    )


_CONTRADICTION_NEGATIONS = {
    "don't", "doesn't", "didn't", "not", "no", "never", "none",
    "without", "remove", "disable", "skip", "stop", "can't",
    "won't", "shouldn't", "isn't", "aren't", "wasn't", "weren't",
    "false", "incorrect", "wrong", "fail",
}


def _evaluate_contradiction_alert(
    conn, trigger: dict, payload: dict, now: datetime
) -> OutreachCandidate | None:
    """Fire when a recent fact appears to contradict an older
    high-confidence fact.

    Phase 6.5 — runs the detection live (no stored contradictions table).
    For each recent fact, search for older durable/core facts sharing
    most content words, then check for negation-signal asymmetry.

    Payload:
      max_age_hours      : int   (default 24)  — "recent" window
      min_old_confidence : float (default 0.8)
      min_word_overlap   : int   (default 3)   — content-word intersection"""
    max_age = int(payload.get("max_age_hours", 24))
    min_old_conf = float(payload.get("min_old_confidence", 0.8))
    min_overlap = int(payload.get("min_word_overlap", 3))
    cutoff = _iso(now - timedelta(hours=max_age))

    # Recent candidate facts (scan only a few to keep this cheap)
    recents = conn.execute(
        """SELECT id, fact, project, confidence, created_at
           FROM facts
           WHERE archived = 0 AND forgotten = 0 AND superseded_by IS NULL
             AND created_at > ?
           ORDER BY created_at DESC LIMIT 20""",
        (cutoff,),
    ).fetchall()
    if not recents:
        return None

    def _tokenize(text: str) -> tuple[set[str], set[str]]:
        """Return (content_words, negation_signals). Negations are kept
        even if short (e.g., 'not', 'no') — length filter only applies
        to content words used for overlap."""
        all_words = {
            w.lower().strip(".,;:!?'\"") for w in (text or "").split() if w
        }
        negations = all_words & _CONTRADICTION_NEGATIONS
        content = {w for w in all_words if len(w) > 3}
        return content, negations

    for new in recents:
        new_words, new_neg = _tokenize(new["fact"] or "")
        if len(new_words) < min_overlap:
            continue

        # Find older high-confidence facts in same project with strong
        # word overlap (cheap LIKE filter then Python intersection).
        # Semantic search would be better but adds dependency weight.
        candidates = conn.execute(
            """SELECT id, fact, confidence, created_at
               FROM facts
               WHERE archived = 0 AND forgotten = 0 AND superseded_by IS NULL
                 AND created_at < ?
                 AND confidence >= ?
                 AND project = ?
                 AND id != ?
               ORDER BY confidence DESC LIMIT 40""",
            (new["created_at"], min_old_conf, new["project"], new["id"]),
        ).fetchall()
        for old in candidates:
            old_words, old_neg = _tokenize(old["fact"] or "")
            overlap = new_words & old_words
            if len(overlap) < min_overlap:
                continue
            # Asymmetric negation is the contradiction signal
            if (new_neg and not old_neg) or (old_neg and not new_neg) \
               or (new_neg and old_neg and new_neg != old_neg):
                subject = "Contradiction — recent claim conflicts with established fact"
                body = (
                    f"Recent claim ({(new['confidence'] or 0):.0%}):\n"
                    f"  {(new['fact'] or '')[:180]}\n\n"
                    f"Conflicts with established fact ({(old['confidence'] or 0):.0%}):\n"
                    f"  {(old['fact'] or '')[:180]}\n\n"
                    f"Worth reconciling — confirm the new one, retract it, or\n"
                    f"accept that the older belief has aged out."
                )
                return OutreachCandidate(
                    trigger_id=trigger["id"],
                    name=trigger["name"],
                    kind="contradiction_alert",
                    urgency=float(trigger["urgency"] or 0.7),
                    subject=subject,
                    body=body,
                    detail=f"new={new['id']} old={old['id']}",
                )
    return None


EVALUATORS = {
    "session_gap": _evaluate_session_gap,
    "anniversary_window": _evaluate_anniversary_window,
    "unresolved_mistake": _evaluate_unresolved_mistake,
    "anchor_dormant": _evaluate_anchor_dormant,
    "probe_failure": _evaluate_probe_failure,
    "contradiction_alert": _evaluate_contradiction_alert,
}


# ── Channels ───────────────────────────────────────────────────────────────


class Channel:
    """Abstract channel. Must be idempotent and never raise."""
    name: str = "abstract"

    def send(self, subject: str, body: str, urgency: float) -> bool:
        raise NotImplementedError


class LogChannel(Channel):
    """Always-on, always-safe. Appends to ~/.null/outreaches.log.

    Phase 5.2 — daily rotation with 30-day retention. On the first write
    of a new UTC day, the existing log is renamed to
    ``outreaches-YYYY-MM-DD.log`` (the date it was last written). Files
    older than 30 days are pruned on every send."""
    name = "log"

    _DATED_RE = re.compile(r"^outreaches-(\d{4}-\d{2}-\d{2})\.log$")
    _RETENTION_DAYS = 30

    def __init__(self, log_path: str | None = None):
        self.log_path = log_path or _default_log_path()
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def _rotate_if_needed(self) -> None:
        """Rename current log to dated file if it was last written on an
        earlier UTC day. No-op if file doesn't exist or is already today."""
        if not os.path.exists(self.log_path):
            return
        try:
            mtime = os.path.getmtime(self.log_path)
        except OSError:
            return
        last_date = datetime.fromtimestamp(mtime, tz=timezone.utc).date()
        today = datetime.now(timezone.utc).date()
        if last_date >= today:
            return
        base_dir = os.path.dirname(self.log_path)
        stem = os.path.splitext(os.path.basename(self.log_path))[0]
        dated = os.path.join(base_dir, f"{stem}-{last_date.isoformat()}.log")
        if os.path.exists(dated):
            # Already rotated this day (shouldn't happen but don't clobber).
            return
        try:
            os.rename(self.log_path, dated)
        except OSError as e:
            logger.warning("LogChannel rotate failed: %s", e)

    def _prune_old(self) -> None:
        base_dir = os.path.dirname(self.log_path)
        cutoff = datetime.now(timezone.utc).date() - timedelta(
            days=self._RETENTION_DAYS
        )
        try:
            names = os.listdir(base_dir)
        except OSError:
            return
        for name in names:
            m = self._DATED_RE.match(name)
            if not m:
                continue
            try:
                d = datetime.fromisoformat(m.group(1)).date()
            except ValueError:
                continue
            if d < cutoff:
                # Best-effort prune; a file vanished under us is fine.
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(base_dir, name))

    def send(self, subject: str, body: str, urgency: float) -> bool:
        try:
            self._rotate_if_needed()
            self._prune_old()
            line = (
                f"\n[{_iso()}] urgency={urgency:.2f}\n"
                f"  {subject}\n"
                f"  {body.replace(chr(10), chr(10) + '  ')}\n"
            )
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
            return True
        except Exception as e:
            logger.warning("LogChannel send failed: %s", e)
            return False


class MacOSChannel(Channel):
    """Opt-in macOS notification via osascript.

    Only active when:
        NEBULA_OUTREACH_NOTIFY=1 in env
        platform is Darwin
        osascript is available
    """
    name = "macos"

    def active(self) -> bool:
        if os.environ.get("NEBULA_OUTREACH_NOTIFY", "0") != "1":
            return False
        if platform.system() != "Darwin":
            return False
        return shutil.which("osascript") is not None

    def send(self, subject: str, body: str, urgency: float) -> bool:
        if not self.active():
            return False
        try:
            # osascript escaping — strip quotes conservatively
            safe_subject = subject.replace('"', "'")[:120]
            safe_body = body.replace('"', "'").replace("\n", " ")[:300]
            script = (
                f'display notification "{safe_body}" '
                f'with title "{safe_subject}" subtitle "Atlas"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5, check=False,
            )
            return True
        except Exception as e:
            logger.warning("MacOSChannel send failed: %s", e)
            return False


# ── Manual emission ────────────────────────────────────────────────────────


def send_manual_outreach(memory: Any, subject: str, body: str,
                         urgency: float = 0.5,
                         channel: str = "log") -> dict:
    """Manual outreach emission — the agent (or operator) reaching out
    directly, outside the trigger system.

    Shared by the `null outreach send` CLI command and the legacy
    null_outreach MCP alias. Writes to the outreaches table + log,
    optionally fires a macOS notification if opted in.

    Returns {"id": rowid, "channels": [delivered channel names]}.
    Raises sqlite3.OperationalError if the outreaches table is missing
    (non-unified DB) — callers surface a friendly message.
    """
    # Build channel list based on request
    channels: list[Channel] = []
    if channel in ("log", "both"):
        channels.append(LogChannel())
    if channel in ("macos", "both"):
        mc = MacOSChannel()
        if mc.active():
            channels.append(mc)
    if not channels:
        channels.append(LogChannel())

    # Dispatch
    sent_channels = [c.name for c in channels if c.send(subject, body, urgency)]

    # Record to DB
    now = _iso()
    cur = memory.db.conn.execute(
        """INSERT INTO outreaches (trigger_id, personality, channel,
           subject, body, urgency, delivered, sent_at)
           VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory.personality,
            ",".join(sent_channels) if sent_channels else "none",
            subject, body, urgency,
            1 if sent_channels else 0,
            now,
        ),
    )
    memory.db.conn.commit()

    # Nebula event — warm amber halo (distinct kind='outreach')
    try:
        memory._emit_nebula_event(
            kind="outreach",
            fact_id="a4495fb51537",
            intensity=0.7,
        )
    except Exception:
        pass

    return {"id": cur.lastrowid, "channels": sent_channels}


# ── Evaluator + Dispatcher ─────────────────────────────────────────────────


@dataclass
class EvaluationResult:
    considered: int = 0
    fired: int = 0
    skipped_cooldown: int = 0
    skipped_disabled: int = 0
    skipped_no_candidate: int = 0
    skipped_budget: int = 0
    skipped_kind_cap: int = 0
    errors: int = 0
    outreaches: list[dict] = field(default_factory=list)


class OutreachEvaluator:
    def __init__(
        self,
        memory: Any,                    # AgentMemory
        channels: list[Channel] | None = None,
        daily_budget: int = DEFAULT_DAILY_BUDGET,
        dry_run: bool | None = None,
    ):
        self.memory = memory
        if channels is None:
            channels = [LogChannel()]
            macos = MacOSChannel()
            if macos.active():
                channels.append(macos)
        self.channels = channels
        self.daily_budget = daily_budget
        if dry_run is None:
            dry_run = os.environ.get("OUTREACH_DRYRUN", "0") == "1"
        self.dry_run = dry_run

    def is_paused(self) -> bool:
        row = self.memory.db.conn.execute(
            "SELECT value FROM meta WHERE key='outreach_paused'"
        ).fetchone()
        return bool(row) and row[0] == "1"

    def _budget_used_last_24h(self) -> int:
        cutoff = _iso(_now() - timedelta(hours=24))
        row = self.memory.db.conn.execute(
            "SELECT COUNT(*) FROM outreaches WHERE sent_at > ?",
            (cutoff,),
        ).fetchone()
        return int(row[0] or 0)

    def _kind_counts_today(self) -> dict[str, int]:
        """Count outreaches fired today (UTC-midnight reset) grouped by
        trigger kind. Phase 6.2 — per-kind caps."""
        cutoff = datetime.combine(
            _now().date(), datetime.min.time(), tzinfo=timezone.utc
        ).isoformat()
        rows = self.memory.db.conn.execute(
            """SELECT t.kind, COUNT(*) AS n
               FROM outreaches o
               JOIN outreach_triggers t ON t.id = o.trigger_id
               WHERE o.sent_at >= ? AND o.trigger_id IS NOT NULL
               GROUP BY t.kind""",
            (cutoff,),
        ).fetchall()
        return {r["kind"]: int(r["n"]) for r in rows}

    def _kind_cap(self, kind: str, payload: dict) -> int:
        """Resolve daily cap for a trigger kind. Payload override wins."""
        override = payload.get("daily_cap")
        if isinstance(override, int) and override > 0:
            return override
        return DEFAULT_KIND_CAPS.get(kind, 1)

    def evaluate(self, force_name: str | None = None) -> EvaluationResult:
        """Run all enabled triggers. Fire eligible ones (budget/cooldown-gated).

        If `force_name` is set, only evaluate that single trigger and
        bypass the cooldown check (useful for CLI `test` command).
        """
        result = EvaluationResult()
        if self.is_paused() and not force_name:
            return result
        now = _now()
        used = self._budget_used_last_24h()
        if used >= self.daily_budget and not force_name:
            result.skipped_budget = 1
            return result

        # Phase 6.2 — per-kind daily counts (UTC-midnight reset).
        # Track live (seed from DB, increment on each fire) so the cap
        # applies within a single evaluate() call too, not just across.
        kind_counts: dict[str, int] = {} if force_name else self._kind_counts_today()

        # Load triggers
        if force_name:
            rows = self.memory.db.conn.execute(
                "SELECT * FROM outreach_triggers WHERE name = ?",
                (force_name,),
            ).fetchall()
        else:
            rows = self.memory.db.conn.execute(
                "SELECT * FROM outreach_triggers WHERE enabled = 1"
            ).fetchall()

        for trig in rows:
            result.considered += 1
            trig_dict = dict(trig)
            if not force_name and not trig_dict.get("enabled"):
                result.skipped_disabled += 1
                continue
            # Cooldown
            if not force_name and trig_dict.get("last_fired_at"):
                try:
                    last = datetime.fromisoformat(trig_dict["last_fired_at"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    age_h = (now - last).total_seconds() / 3600
                    if age_h < float(trig_dict.get("cooldown_hours") or DEFAULT_COOLDOWN_HOURS):
                        result.skipped_cooldown += 1
                        continue
                except (ValueError, TypeError):
                    pass

            try:
                payload = json.loads(trig_dict.get("payload") or "{}")
            except (ValueError, TypeError):
                payload = {}
            kind = trig_dict["kind"]
            evaluator = EVALUATORS.get(kind)
            if evaluator is None:
                result.errors += 1
                continue

            # Phase 6.2 — enforce per-kind daily cap BEFORE running the
            # (potentially expensive) evaluator. Skip silently with a
            # counter increment if we're at the kind's daily limit.
            if not force_name:
                kind_cap = self._kind_cap(kind, payload)
                if kind_counts.get(kind, 0) >= kind_cap:
                    result.skipped_kind_cap += 1
                    continue

            try:
                candidate = evaluator(
                    self.memory.db.conn, trig_dict, payload, now,
                )
            except Exception as e:
                logger.exception("evaluator %s failed: %s", kind, e)
                result.errors += 1
                continue

            if candidate is None:
                result.skipped_no_candidate += 1
                continue

            if used + result.fired >= self.daily_budget and not force_name:
                result.skipped_budget += 1
                continue

            # Dispatch through channels
            sent_any = False
            channels_used: list[str] = []
            for channel in self.channels:
                if self.dry_run and channel.name != "log":
                    continue
                ok = channel.send(candidate.subject, candidate.body, candidate.urgency)
                if ok:
                    sent_any = True
                    channels_used.append(channel.name)

            # Record outreach
            sent_at = _iso(now)
            cur = self.memory.db.conn.execute(
                """INSERT INTO outreaches (trigger_id, personality, channel,
                   subject, body, urgency, delivered, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate.trigger_id,
                    getattr(self.memory, "personality", "atlas"),
                    ",".join(channels_used) if channels_used else "none",
                    candidate.subject,
                    candidate.body,
                    candidate.urgency,
                    1 if sent_any else 0,
                    sent_at,
                ),
            )
            self.memory.db.conn.execute(
                """UPDATE outreach_triggers
                   SET last_fired_at = ?, last_fired_detail = ?
                   WHERE id = ?""",
                (sent_at, candidate.detail, candidate.trigger_id),
            )
            self.memory.db.conn.commit()

            # Emit Nebula event — warm amber pulse (distinct from anchor).
            # Fires on the origin anchor as stand-in; future versions could
            # fire on a dedicated "identity center" key once we add support.
            # Best-effort — a viz emit failure must not block the outreach.
            with contextlib.suppress(Exception):
                self.memory._emit_nebula_event(
                    kind="outreach",
                    fact_id="a4495fb51537",  # origin anchor as stand-in
                    intensity=0.8,
                )

            result.fired += 1
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            result.outreaches.append({
                "id": cur.lastrowid,
                "name": candidate.name,
                "kind": candidate.kind,
                "subject": candidate.subject,
                "body": candidate.body,
                "channels": channels_used,
                "delivered": sent_any,
            })

        return result


# ── Seed installer ─────────────────────────────────────────────────────────


DEFAULT_TRIGGERS = [
    {
        "name": "session_gap_3d",
        "kind": "session_gap",
        "payload": {"days": 3},
        "urgency": 0.4,
        "cooldown_hours": 24,
    },
    {
        "name": "anniversary_window_2d",
        "kind": "anniversary_window",
        "payload": {
            "window_days": 2,
            "allow_day_of": False,  # always fire day-before, never day-of
            # Deployment-specific dates come from identity.json
            # "anniversaries" at seed time; the package ships none.
            "anniversaries": [],
        },
        "urgency": 0.7,
        "cooldown_hours": 24,
    },
    {
        "name": "unresolved_mistake_24h",
        "kind": "unresolved_mistake",
        "payload": {"min_age_hours": 24},
        "urgency": 0.5,
        "cooldown_hours": 12,
    },
]


def seed_default_triggers(memory: Any, enable_all: bool = False) -> dict:
    """Install the 3 default triggers (all DISABLED unless enable_all=True).

    Safe to call repeatedly — INSERT OR IGNORE by unique name.
    Returns stats: {installed, skipped_existing}.
    """
    stats = {"installed": 0, "skipped_existing": 0}
    now = _iso()
    # Deployment-specific anniversary dates live in identity.json, never
    # in the package defaults.
    identity_anniversaries = []
    try:
        identity_anniversaries = (
            getattr(memory, "identity", {}) or {}
        ).get("anniversaries", []) or []
    except Exception:
        pass
    for t in DEFAULT_TRIGGERS:
        existing = memory.db.conn.execute(
            "SELECT id FROM outreach_triggers WHERE name = ?", (t["name"],)
        ).fetchone()
        if existing:
            stats["skipped_existing"] += 1
            continue
        payload = dict(t["payload"])
        if t["kind"] == "anniversary_window" and identity_anniversaries:
            payload["anniversaries"] = identity_anniversaries
        memory.db.conn.execute(
            """INSERT INTO outreach_triggers
               (name, kind, payload, enabled, cooldown_hours, urgency, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                t["name"], t["kind"], json.dumps(payload),
                1 if enable_all else 0,
                t["cooldown_hours"], t["urgency"], now,
            ),
        )
        stats["installed"] += 1
    memory.db.conn.commit()
    return stats
