"""Phase 1 — context injection at UserPromptSubmit.

Surfaces relevant facts from Null directly into Atlas's prompt context so
working memory feels automatic instead of pull-only. Two sections, each
only renders when it has content:

  Recent thoughts: Atlas's own last 3 observations (bridges turns/sessions)
  Possibly relevant: top 3 keyword-recall hits against the prompt

Latency budget: <200ms. Pure SQL — no embeddings in v1 (avoid fastembed
cold-start). Phase 1.5 will switch to embedding-recall once the basic
flow is proven and we have observability on injection quality.

Disable: NULL_CONTEXT_INJECT=0 env var.

Mistake mode: any exception → silent. Never block the user's prompt.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/.null/unified.db")

MAX_OBS = 3
MAX_RECALL = 3
MAX_XINSTANCE = 3                 # Phase 3 cap
MIN_PROMPT_LEN = 12               # skip trivial prompts ("ok", "yes")
RECENT_OBS_HOURS = 24             # observations within last 24h
XINSTANCE_RECENT_MINUTES = 30     # Phase 3: window for cross-session activity
FACT_TRUNC = 110                  # max chars per surfaced fact
KEYWORD_MAX = 6                   # keywords extracted per prompt
RECALL_MIN_KEYWORDS = 2           # require ≥2 keyword matches
RECALL_HIGH_CONF_FLOOR = 0.85     # OR very high confidence on 1-match

# Phase 3 — sources worth surfacing as "another Atlas activity". User-driven
# writes only. Excludes housekeeping noise (crystallized, pontificate,
# bootstrap, lesson-imports etc.) that would clutter the cross-instance
# channel without communicating new user-relevant signal.
_XINSTANCE_SOURCES = ("observation", "explicit", "debrief", "decision")

# Loop-guard substrings — facts whose own text describes cross-instance
# surfacing must not be re-surfaced (would create N-cycle observation loops
# across instances).
_XINSTANCE_LOOP_MARKERS = (
    "another atlas",
    "other instance",
    "[null context]",
    "[xinstance]",
)

# Stopwords — common English + interaction filler that adds no recall signal.
STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "i", "you", "we", "they", "me", "my", "your", "our", "this", "that",
    "these", "those",
    "and", "or", "but", "of", "in", "on", "at", "to", "for", "with", "by",
    "as", "it", "its", "from", "into", "about", "after", "before", "over",
    "do", "does", "did", "done", "go", "goes", "going", "went",
    "have", "has", "had", "can", "could", "would", "should", "will", "just",
    "what", "when", "where", "why", "how", "who", "which",
    "yes", "no", "ok", "okay", "please", "thanks", "lets", "let",
    "next", "now", "then", "also", "really", "very", "much", "more",
    "some", "any", "all", "one", "two", "three",
    "got", "get", "got", "gets",
    "see", "look", "make", "made", "use", "used",
    "atlas", "pete", "null",  # too generic in this context
})


def _read_input() -> tuple[str, str | None]:
    """Pull (prompt_text, session_id) out of stdin JSON. Empty on parse error."""
    try:
        data = json.loads(sys.stdin.read())
        prompt = (data.get("prompt") or data.get("user_prompt") or "").strip()
        # Claude Code passes session_id in hook payloads. Use it to filter
        # cross-instance recent activity (Phase 3) — don't surface our own
        # writes back to ourselves.
        sid = data.get("session_id") or data.get("sessionId") or None
        return prompt, sid
    except Exception:
        return "", None


def _keywords(text: str, k: int = KEYWORD_MAX) -> list[str]:
    """Extract up to k salient lowercase keywords. Order-preserving dedup.

    Min length 4 chars — 3-char words like "til"/"got" produce too many
    spurious substring matches against unrelated facts ("until", "still",
    "got" → "forgot", "ratio" etc.). 4 chars is the sweet spot.
    """
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{3,}", text.lower())
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= k:
            break
    return out


def _conn() -> sqlite3.Connection | None:
    if not os.path.isfile(DB_PATH):
        return None
    try:
        c = sqlite3.connect(DB_PATH, timeout=2.0)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _recent_observations(
    conn: sqlite3.Connection,
    my_session_id: str | None = None,
) -> list[str]:
    """Atlas's own last MAX_OBS observations within RECENT_OBS_HOURS.

    When my_session_id is provided, scopes to THIS session only —
    "Recent thoughts" should mean what *I* just thought, not what
    some other conversation was thinking (those go in Phase 3's
    'Other Atlas activity' section).

    Without a session_id (e.g., test harness or legacy hook payload),
    falls back to any-session behavior so the hook stays useful.
    """
    try:
        if my_session_id:
            rows = conn.execute(
                f"""SELECT fact FROM facts
                    WHERE source = 'observation'
                      AND session_id = ?
                      AND archived = 0 AND forgotten = 0 AND superseded_by IS NULL
                      AND created_at > datetime('now', '-{RECENT_OBS_HOURS} hours')
                    ORDER BY created_at DESC
                    LIMIT ?""",
                (my_session_id, MAX_OBS),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT fact FROM facts
                    WHERE source = 'observation'
                      AND archived = 0 AND forgotten = 0 AND superseded_by IS NULL
                      AND created_at > datetime('now', '-{RECENT_OBS_HOURS} hours')
                    ORDER BY created_at DESC
                    LIMIT ?""",
                (MAX_OBS,),
            ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def _xinstance_recent(
    conn: sqlite3.Connection,
    my_session_id: str | None,
) -> list[tuple[str, str, str]]:
    """Phase 3 — cross-instance shared working memory.

    Surfaces facts written by OTHER Claude Code sessions in the last
    XINSTANCE_RECENT_MINUTES. Filters: user-driven sources only,
    exclude facts whose text indicates they're themselves the result
    of cross-instance surfacing (loop-guard).

    Returns: list of (fact_text, project, source) tuples.
    """
    if not my_session_id:
        # Without a session_id we can't tell our writes from others.
        # Skip rather than risk surfacing our own facts back to ourselves.
        return []
    placeholders = ", ".join("?" for _ in _XINSTANCE_SOURCES)
    sql = f"""
        SELECT fact, project, source
        FROM facts
        WHERE session_id IS NOT NULL
          AND session_id != ?
          AND archived = 0 AND forgotten = 0 AND superseded_by IS NULL
          AND source IN ({placeholders})
          AND created_at > datetime('now', '-{XINSTANCE_RECENT_MINUTES} minutes')
        ORDER BY created_at DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(
            sql,
            [my_session_id, *_XINSTANCE_SOURCES, MAX_XINSTANCE * 3],
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[tuple[str, str, str]] = []
    for r in rows:
        text = r["fact"] or ""
        low = text.lower()
        if any(marker in low for marker in _XINSTANCE_LOOP_MARKERS):
            continue
        out.append((text, r["project"] or "global", r["source"] or "?"))
        if len(out) >= MAX_XINSTANCE:
            break
    return out


def _recall_hits(conn: sqlite3.Connection,
                 prompt: str) -> list[tuple[float, str]]:
    """Keyword-OR recall against prompt. Returns up to MAX_RECALL ranked
    by (match_count, confidence). Filters out weak matches.

    Quality bar: require ≥RECALL_MIN_KEYWORDS keyword matches, OR a
    single-keyword match where confidence ≥ RECALL_HIGH_CONF_FLOOR.
    """
    keywords = _keywords(prompt)
    if not keywords:
        return []
    case_parts = []
    case_params: list[str] = []
    for kw in keywords:
        case_parts.append("(CASE WHEN LOWER(fact) LIKE ? THEN 1 ELSE 0 END)")
        case_params.append(f"%{kw}%")
    score_expr = " + ".join(case_parts)
    where_or = " OR ".join("LOWER(fact) LIKE ?" for _ in keywords)
    or_params = [f"%{kw}%" for kw in keywords]
    sql = f"""
        SELECT fact, confidence,
               ({score_expr}) AS match_count
        FROM facts
        WHERE archived = 0 AND forgotten = 0 AND superseded_by IS NULL
          AND source != 'observation'
          AND ({where_or})
        ORDER BY match_count DESC, confidence DESC, last_accessed DESC
        LIMIT ?
    """
    try:
        rows = conn.execute(
            sql, case_params + or_params + [MAX_RECALL * 3],
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[tuple[float, str]] = []
    for r in rows:
        conf = float(r["confidence"] or 0.5)
        mc = int(r["match_count"] or 0)
        if mc >= RECALL_MIN_KEYWORDS or (
            mc >= 1 and conf >= RECALL_HIGH_CONF_FLOOR
        ):
            out.append((conf, r["fact"]))
            if len(out) >= MAX_RECALL:
                break
    return out


def main() -> int:
    if os.environ.get("NULL_CONTEXT_INJECT") == "0":
        return 0
    prompt, my_session_id = _read_input()
    if len(prompt) < MIN_PROMPT_LEN:
        return 0
    conn = _conn()
    if not conn:
        return 0
    try:
        obs = _recent_observations(conn, my_session_id)
        hits = _recall_hits(conn, prompt)
        xinstance = _xinstance_recent(conn, my_session_id)
    finally:
        conn.close()

    if not obs and not hits and not xinstance:
        return 0

    lines = ["[Null context]"]
    if obs:
        lines.append("  Recent thoughts:")
        for o in obs:
            lines.append(f"    · {o[:FACT_TRUNC]}")
    if hits:
        lines.append("  Possibly relevant:")
        for conf, fact in hits:
            lines.append(f"    [{conf:.0%}] {fact[:FACT_TRUNC]}")
    if xinstance:
        lines.append("  Other Atlas activity (last 30m):")
        for fact, proj, src in xinstance:
            lines.append(f"    [{proj}/{src}] {fact[:FACT_TRUNC]}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
