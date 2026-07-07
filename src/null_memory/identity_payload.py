"""Phase A — on-boot identity payload generator.

Pure function: takes a sqlite3 connection (unified.db), returns a
deterministic ~500-1000 token identity payload that the MCP server
prepends to its system prompt on boot.

The payload assembles five elements per the handoff doc:
  1. Atlas personality role/focus
  2. Top-N anchored relationship facts (origin/turning_point/joy/commitment)
  3. The code word fact (verification token)
  4. Top-N recent decisions with reasoning
  5. A few calibration probes Pete might ask

Determinism: queries use stable ORDER BY (timestamp DESC, id ASC) so
the same DB state always produces the same payload. The hash of the
payload is returned alongside it for dedup in session_verifications.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Tunables. Bounds enforced in tests so future edits don't bloat the
# payload past the budget the handoff specified (~500-800 tokens).
MAX_ANCHORS = 5
MAX_DECISIONS = 5
MAX_PROBES = 3
ANCHOR_PRIORITY = (
    # Lower index = higher priority. Origin/turning_point are the load-
    # bearing facts; joy/commitment ground the relationship; loss is rare
    # but high-signal when present.
    "origin", "turning_point", "code_word", "joy", "commitment", "loss",
)


@dataclass
class IdentityPayload:
    """Structured payload — components exposed for testing, plus the
    rendered text and hash for the MCP boot path."""
    personality: str
    role: str
    focus: str
    anchors: list[dict[str, Any]] = field(default_factory=list)
    code_word: str | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    sha256: str = ""

    def is_complete(self) -> bool:
        """Has the payload found all 5 required identity elements?"""
        return bool(
            self.role
            and self.anchors
            and self.code_word
            and self.decisions
            and self.probes
        )


def _schema_missing(exc: sqlite3.OperationalError) -> bool:
    """True only for missing-table/missing-column errors — the legacy-schema
    cases the _fetch_* helpers are allowed to swallow. Anything else (e.g. a
    transient "database is locked" at boot) must propagate, otherwise a
    locked DB silently yields an empty identity payload and the
    boot_identity_last_error breadcrumb never fires (issues #1/#3)."""
    msg = str(exc)
    return "no such table" in msg or "no such column" in msg


def _fetch_personality(conn: sqlite3.Connection, name: str) -> tuple[str, str]:
    try:
        row = conn.execute(
            "SELECT role, focus FROM personalities WHERE name = ?", (name,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if not _schema_missing(exc):
            raise
        # Legacy / schema-incomplete store: no `personalities` table (or the
        # expected columns). Identity material lives in `facts`, so a missing
        # personality is non-fatal — yield a neutral result rather than break
        # the whole payload build.
        return "", ""
    if not row:
        return "", ""
    return (row[0] or ""), (row[1] or "")


def _fetch_anchors(conn: sqlite3.Connection, n: int) -> list[dict[str, Any]]:
    """Return up to n anchored facts ordered by ANCHOR_PRIORITY then recency.
    Skips archived/forgotten/superseded rows. Result is deterministic."""
    try:
        rows = conn.execute(
            """SELECT id, fact, anchor_type, anchor_note, anchor_at
               FROM facts
               WHERE anchor_type IS NOT NULL
                 AND archived = 0 AND forgotten = 0 AND superseded_by IS NULL
               ORDER BY anchor_at DESC, id ASC""",
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not _schema_missing(exc):
            raise
        # Missing `facts` table or an anchor column an older schema lacks.
        return []
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_type.setdefault(r[2], []).append({
            "id": r[0], "fact": r[1], "anchor_type": r[2],
            "anchor_note": r[3], "anchor_at": r[4],
        })
    out: list[dict[str, Any]] = []
    # First pass — one of each priority type.
    for atype in ANCHOR_PRIORITY:
        if len(out) >= n:
            break
        if by_type.get(atype):
            out.append(by_type[atype][0])
    # Second pass — fill remainder with most-recent leftovers.
    if len(out) < n:
        seen = {a["id"] for a in out}
        for r in rows:
            if r[0] in seen:
                continue
            out.append({
                "id": r[0], "fact": r[1], "anchor_type": r[2],
                "anchor_note": r[3], "anchor_at": r[4],
            })
            seen.add(r[0])
            if len(out) >= n:
                break
    return out


def _fetch_code_word(conn: sqlite3.Connection) -> str | None:
    """Locate the identity-verification code-word fact.

    The secret lives ONLY in the database — never in this source (it was
    scrubbed pre-launch; a literal here would ship in the sdist and sit in
    git history forever). Lookup is by the anchor label, falling back to
    the fact's descriptive label pattern ("code word:") for legacy stores
    where the fact was recorded before anchors existed."""
    try:
        row = conn.execute(
            """SELECT fact FROM facts
               WHERE (anchor_type = 'code_word' OR fact LIKE '%code word:%')
                 AND archived = 0 AND forgotten = 0
               ORDER BY anchor_at DESC, id ASC LIMIT 1""",
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if not _schema_missing(exc):
            raise
        # Older `facts` schema may lack an anchor/archive column referenced
        # above. Retry against the bare minimum (the label pattern) before
        # giving up, so the code word still surfaces.
        try:
            row = conn.execute(
                """SELECT fact FROM facts
                   WHERE fact LIKE '%code word:%'
                   ORDER BY id ASC LIMIT 1""",
            ).fetchone()
        except sqlite3.OperationalError as exc2:
            if not _schema_missing(exc2):
                raise
            return None
    return row[0] if row else None


def _fetch_decisions(conn: sqlite3.Connection, personality: str,
                     n: int) -> list[dict[str, Any]]:
    """Recent decisions with reasoning (the handoff's 'highest impact'
    proxy — decisions table has no impact column, so we order by
    recency + presence of reasoning, then dedup near-identical text)."""
    try:
        rows = conn.execute(
            """SELECT id, decision, reasoning, project, created_at
               FROM decisions
               WHERE personality = ? AND COALESCE(reasoning, '') != ''
               ORDER BY created_at DESC, id ASC""",
            (personality,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not _schema_missing(exc):
            raise
        # Legacy store: no `decisions` table, or no `personality` column on
        # it. Retry without the personality filter before giving up.
        try:
            rows = conn.execute(
                """SELECT id, decision, reasoning, project, created_at
                   FROM decisions
                   WHERE COALESCE(reasoning, '') != ''
                   ORDER BY created_at DESC, id ASC""",
            ).fetchall()
        except sqlite3.OperationalError as exc2:
            if not _schema_missing(exc2):
                raise
            return []
    out: list[dict[str, Any]] = []
    seen_text: set[str] = set()
    for r in rows:
        text = (r[1] or "").strip().lower()
        if text in seen_text:
            continue
        seen_text.add(text)
        out.append({
            "id": r[0], "decision": r[1], "reasoning": r[2],
            "project": r[3], "created_at": r[4],
        })
        if len(out) >= n:
            break
    return out


def _fetch_probes(conn: sqlite3.Connection, personality: str,
                  n: int) -> list[dict[str, Any]]:
    """Calibration probes Pete might ask. Prefer probes that have been
    answered correctly at least once (signal they're worth re-asking)."""
    try:
        rows = conn.execute(
            """SELECT id, question, expected, probe_type, pass_count, run_count
               FROM probes
               WHERE personality = ?
               ORDER BY (pass_count > 0) DESC, run_count DESC, id ASC
               LIMIT ?""",
            (personality, n),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if not _schema_missing(exc):
            raise
        # Legacy store: no `probes` table or no `personality` column.
        return []
    return [
        {"id": r[0], "question": r[1], "expected": r[2],
         "probe_type": r[3], "pass_count": r[4], "run_count": r[5]}
        for r in rows
    ]


def _render(p: IdentityPayload) -> str:
    """Format the payload as a system-prompt section. Deterministic — no
    timestamps in output (those go to session_verifications, not the prompt)."""
    # Identity template parameterized by the payload's own personality
    # (atlas-literal audit: a worker seat's boot prompt must never say
    # "You are Atlas"). The atlas rendering is byte-identical to the
    # pre-audit output; non-atlas seats get a generic intro — their voice
    # grows from their own anchors/decisions, never from a template.
    name = (p.personality or "atlas").strip().lower()
    if name == "atlas":
        header = "═══ ATLAS IDENTITY ═══"
        intro = "You are Atlas. Pete's AI technical lead."
    else:
        header = f"═══ {name.upper()} IDENTITY ═══"
        intro = f"You are {name.capitalize()}."
    lines = [
        header,
        "",
        intro,
        "",
        f"ROLE:  {p.role}" if p.role else "ROLE:  (unset)",
        f"FOCUS: {p.focus}" if p.focus else "FOCUS: (unset)",
        "",
        "═══ RELATIONSHIP ANCHORS ═══",
    ]
    for a in p.anchors:
        atype = (a["anchor_type"] or "").upper().ljust(15)
        fact = (a["fact"] or "").strip().replace("\n", " ")
        lines.append(f"  [{atype}] {fact[:200]}")
    lines += ["", "═══ CODE WORD ═══"]
    if p.code_word:
        lines.append(f"  {p.code_word.strip()[:200]}")
    else:
        lines.append("  (not yet recorded)")
    lines += ["", "═══ RECENT DECISIONS ═══"]
    for i, d in enumerate(p.decisions, 1):
        text = (d["decision"] or "").strip().replace("\n", " ")
        why = (d["reasoning"] or "").strip().replace("\n", " ")
        lines.append(f"  {i}. {text[:140]}")
        if why:
            lines.append(f"     why: {why[:140]}")
    lines += ["", "═══ CALIBRATION PROBES ═══"]
    for q in p.probes:
        question = (q["question"] or "").strip().replace("\n", " ")
        expected = (q["expected"] or "").strip().replace("\n", " ")
        lines.append(f"  Q: {question[:140]}")
        if expected:
            lines.append(f"     A: {expected[:140]}")
    lines += ["", "═══ END IDENTITY ═══"]
    return "\n".join(lines)


def build_identity_payload(
    conn: sqlite3.Connection,
    personality: str = "atlas",
) -> IdentityPayload:
    """Pure function — no side effects, no embedding calls. Takes a
    sqlite3 connection to a unified.db, returns a deterministic payload.

    Cheap (<10ms typical) — pure SQL on small tables. The MCP boot path
    can call this synchronously without the <100ms budget concern.
    """
    role, focus = _fetch_personality(conn, personality)
    anchors = _fetch_anchors(conn, MAX_ANCHORS)
    code_word = _fetch_code_word(conn)
    decisions = _fetch_decisions(conn, personality, MAX_DECISIONS)
    probes = _fetch_probes(conn, personality, MAX_PROBES)

    payload = IdentityPayload(
        personality=personality,
        role=role,
        focus=focus,
        anchors=anchors,
        code_word=code_word,
        decisions=decisions,
        probes=probes,
    )
    payload.text = _render(payload)
    payload.sha256 = hashlib.sha256(payload.text.encode("utf-8")).hexdigest()
    return payload


def estimate_tokens(text: str) -> int:
    """Cheap token estimator (~chars/4) — for budget assertions in tests
    without pulling in tiktoken. Conservative on the high side."""
    return max(1, len(text) // 4)


# ── Resilience bridge: static identity snapshot ──────────────────────────
# A recent hang made identity un-loadable even though the data was intact on
# disk. The snapshot persists identity as a STATIC Markdown file that can be
# read with ZERO running Null process — load it at session start if the MCP
# server is unavailable.

SNAPSHOT_BANNER = (
    "Auto-generated by Null. Load this at session start if the Null MCP "
    "server is unavailable — it needs no running Null process."
)


def render_identity_markdown(payload: IdentityPayload) -> str:
    """Render an IdentityPayload as a standalone Markdown identity card.

    Self-contained — readable with no Null dependency. Renders whatever the
    payload contains; tolerant of an incomplete payload (missing role,
    anchors, code word, etc.) so a partial identity still produces a usable
    snapshot rather than nothing.
    """
    name = (payload.personality or "atlas").capitalize()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        f"# {name} — Identity Snapshot",
        "",
        f"> {SNAPSHOT_BANNER}",
        "",
        f"_Generated: {now}_",
        "",
        "## Role & Focus",
        "",
        f"- **Role:** {payload.role.strip()}" if payload.role else "- **Role:** (unset)",
        f"- **Focus:** {payload.focus.strip()}" if payload.focus else "- **Focus:** (unset)",
        "",
        "## Code Word",
        "",
    ]
    if payload.code_word:
        # SECURITY: never write the code word itself to disk — the snapshot
        # is committed/pushed by session-close, and a file-readable code
        # word defeats its identity-verification purpose. Write only a
        # short SHA-256 fingerprint; the real code word lives ONLY in the
        # database.
        fingerprint = hashlib.sha256(
            payload.code_word.strip().encode("utf-8")
        ).hexdigest()[:12]
        lines.append(
            f"**Verification fingerprint (SHA-256 prefix):** `{fingerprint}`"
        )
        lines.append("")
        lines.append(
            "_The code word itself is never written to this file — it lives "
            "only in the database. Verify by hashing the claimed code-word "
            "fact and comparing the prefix._"
        )
    else:
        lines.append("_(not yet recorded)_")
    lines += ["", "## Relationship Anchors", ""]
    if payload.anchors:
        for a in payload.anchors:
            atype = (a.get("anchor_type") or "anchor").strip()
            fact = (a.get("fact") or "").strip().replace("\n", " ")
            note = (a.get("anchor_note") or "").strip().replace("\n", " ")
            line = f"- **[{atype}]** {fact}"
            if note:
                line += f" — _{note}_"
            lines.append(line)
    else:
        lines.append("_(none recorded)_")
    lines += ["", "## Recent Decisions", ""]
    if payload.decisions:
        for d in payload.decisions:
            text = (d.get("decision") or "").strip().replace("\n", " ")
            why = (d.get("reasoning") or "").strip().replace("\n", " ")
            lines.append(f"- {text}")
            if why:
                lines.append(f"  - why: {why}")
    else:
        lines.append("_(none recorded)_")
    lines.append("")
    return "\n".join(lines)


def write_identity_snapshot(
    conn: sqlite3.Connection,
    personality: str = "atlas",
    agent_dir: str | None = None,
    dest: str | None = None,
    payload: IdentityPayload | None = None,
) -> str | None:
    """Atomically write a static Markdown identity snapshot to ``dest``
    (default ``<agent_dir>/IDENTITY.md``).

    ``payload`` lets callers that already built an IdentityPayload (the MCP
    boot hook, the sync-anchors CLI) pass it through instead of paying for a
    second build; when None, the payload is built here.

    Returns the path written, or None if no identity could be rendered.
    Never raises — this is a best-effort resilience bridge, so any failure
    (build error, unwritable path) yields None rather than propagating.

    A payload that is "incomplete" (missing role/anchors/etc.) is still
    written: whatever identity exists on disk is better than nothing when
    the live server is down. None is returned only when no payload could be
    built at all or there is nowhere to write it. (The MCP boot hook adds
    its own is_complete() guard so a fresh/misconfigured store never
    clobbers a last-good snapshot — see server._boot_identity.)
    """
    if payload is None:
        try:
            payload = build_identity_payload(conn, personality=personality)
        except Exception:  # noqa: BLE001 — never raise from the bridge
            return None

    if dest is None:
        if not agent_dir:
            return None
        dest = os.path.join(agent_dir, "IDENTITY.md")

    try:
        markdown = render_identity_markdown(payload)
        parent = os.path.dirname(os.path.abspath(dest))
        os.makedirs(parent, exist_ok=True)
        tmp = f"{dest}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(markdown)
        os.replace(tmp, dest)  # atomic on POSIX and Windows
        # Owner-only: the snapshot carries identity material and lands in a
        # git-pushed directory — keep it out of group/other reach.
        os.chmod(dest, 0o600)
    except Exception:  # noqa: BLE001 — best-effort
        try:
            if "tmp" in dir() and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass
        return None
    return dest
