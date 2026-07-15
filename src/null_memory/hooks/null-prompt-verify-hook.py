"""UserPromptSubmit hook helper — scans the user's prompt for live-state
questions and pre-emits status from two complementary sources:

  1. doc_claims cache: hits where Pete's docs (CLAUDE.md, handoffs)
     contain a claim that's been refuted/stale by Hypnos doc-audit.
     Warns: "if you trust the doc, you'll be wrong."

  2. Live verifiers (Phase 2 of working-memory plan): runs the same
     _verify_* functions from doc_audit DIRECTLY against the live
     system (git log, file existence, schema_meta). Pre-arms Atlas's
     context with the actual current truth before responding.

Input:  stdin JSON from Claude Code with shape:
        {"hook_event_name": "UserPromptSubmit",
         "prompt": "<user text>", ...}

Output: text on stdout — gets prepended to the next model turn.
        Silent (empty stdout) when no live-state pattern matched.

Mistake mode: any exception → silent. Never block the user's prompt.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys


# Patterns that suggest the user is asking about live system state.
# Each pattern's matched text is also used as the doc_claims search hint.
LIVE_STATE_PATTERNS = [
    # Phase ship status
    re.compile(r"(phase\s+\d+(?:\.\d+)?)\s+(?:shipped|done|complete|"
               r"todo|pending|implemented|finished|live)", re.I),
    re.compile(r"(?:did|has|was)\s+(phase\s+\d+(?:\.\d+)?)\s+ship", re.I),
    re.compile(r"(?:is|are)\s+(.{4,40}?)\s+(?:done|shipped|implemented)\??",
               re.I),
    # Schema version
    re.compile(r"(schema\s+(?:version\s+)?(?:is\s+)?v?\d+)", re.I),
    re.compile(r"(?:what(?:'s|\s+is)|current)\s+(schema\s+version)", re.I),
    # File / function existence
    re.compile(r"(?:does|is)\s+([\w./]+\.(?:py|ts|tsx))\s+(?:exist|present)",
               re.I),
    re.compile(r"(?:is|where)\s+(\w+\(\))\s+(?:at|in|located)", re.I),
]


MAX_CLAIMS_TO_SHOW = 3
MAX_LIVE_VERIFICATIONS = 4
DB_PATH = os.path.expanduser("~/.null/unified.db")
# Honor NULL_REPO_ROOT for users running from a non-default path.
# Falls back to the repo root inferred from this file's location. This hook
# now ships INSIDE the package (src/null_memory/hooks/<this>), so the repo
# root is three levels up (hooks -> null_memory -> src -> repo). When Null is
# installed (wheel/editable) the import below succeeds and this fallback path
# is never actually used; it only matters for a raw source checkout.
DEFAULT_REPO_ROOT = os.environ.get(
    "NULL_REPO_ROOT",
    os.path.abspath(os.path.join(
        os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)),
)

# Phase 2 — live verifier dispatch. Each entry pairs a quick prompt-shape
# probe with the doc_audit verifier function it triggers. Only verifiers
# whose probe matches the prompt run, capping latency on prompts that
# don't ask verifiable questions.
_LIVE_VERIFIER_PROBES = [
    ("ship_status",  re.compile(r"phase\s+\d", re.I)),
    ("schema_version", re.compile(r"schema(\s+version)?", re.I)),
    ("file_ref",     re.compile(r"[\w./~-]+\.(?:py|ts|tsx|md|json|sh|sql)\b", re.I)),
    ("function_ref", re.compile(r"\b[a-zA-Z_]\w{2,}\(\)", re.I)),
]


def _read_input() -> dict:
    try:
        return json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}


def _extract_hints(prompt: str) -> list[str]:
    """Return distinct snippets that triggered live-state patterns."""
    hints: list[str] = []
    for pat in LIVE_STATE_PATTERNS:
        for m in pat.finditer(prompt):
            # The first non-empty group is the meaningful snippet.
            hint = next((g for g in m.groups() if g), m.group(0))
            hint = hint.strip().strip("'\"`")
            if hint and hint.lower() not in [h.lower() for h in hints]:
                hints.append(hint)
    return hints


def _query_claims(hint: str) -> list[tuple[str, str, str, str | None]]:
    """Return matching doc_claims rows: (status, source_path, claim_text,
    refute_evidence). Empty list on any DB issue."""
    if not os.path.isfile(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT status, source_path, claim_text, refute_evidence
               FROM doc_claims
               WHERE claim_text LIKE ?
                 AND status IN ('refuted', 'verified', 'stale')
               ORDER BY
                 CASE status
                   WHEN 'refuted' THEN 0
                   WHEN 'stale' THEN 1
                   ELSE 2
                 END,
                 last_verified_at DESC
               LIMIT ?""",
            (f"%{hint[:50]}%", MAX_CLAIMS_TO_SHOW),
        ).fetchall()
        conn.close()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    except sqlite3.Error:
        return []


def _run_live_verifiers(prompt: str,
                         repo_root: str | None = None
                         ) -> list[tuple[str, str, str | None]]:
    """Phase 2 — run the live doc_audit verifiers against the prompt.

    Only fires verifiers whose probe pattern matches the prompt shape,
    so prompts that don't ask verifiable questions skip the work
    (latency stays near zero on conversational turns).

    Returns: list of (claim_type, verdict, evidence) for verifiers that
             returned 'verified' or 'refuted'. Skips 'unverified'.
    """
    # Resolve the repo root at CALL time, not def time. Binding
    # DEFAULT_REPO_ROOT as a default argument froze it at import, so tests
    # (and any caller) that reassign the module global were silently
    # ignored — the verifiers read the ambient repo instead of the intended
    # one. This surfaced as a green-on-dev / red-on-CI test: a dev checkout
    # has real phase history, an orphan release snapshot does not.
    if repo_root is None:
        repo_root = DEFAULT_REPO_ROOT
    if not any(probe.search(prompt) for _, probe in _LIVE_VERIFIER_PROBES):
        return []
    try:
        # When the hook runs out-of-tree (installed via pip), null_memory is
        # already importable. When running from the dev repo, src/ may not
        # be on sys.path — fall back to NULL_REPO_ROOT or the inferred root.
        try:
            from null_memory.doc_audit import (  # noqa: E402
                _verify_ship_status, _verify_schema_version,
                _verify_file_ref, _verify_function_ref,
            )
        except ImportError:
            src_path = os.path.join(DEFAULT_REPO_ROOT, "src")
            if os.path.isdir(src_path):
                sys.path.insert(0, src_path)
            from null_memory.doc_audit import (  # noqa: E402
                _verify_ship_status, _verify_schema_version,
                _verify_file_ref, _verify_function_ref,
            )
    except ImportError:
        return []
    fn_table = {
        "ship_status": _verify_ship_status,
        "schema_version": _verify_schema_version,
        "file_ref": _verify_file_ref,
        "function_ref": _verify_function_ref,
    }
    results: list[tuple[str, str, str | None]] = []
    for ctype, probe in _LIVE_VERIFIER_PROBES:
        if not probe.search(prompt):
            continue
        fn = fn_table.get(ctype)
        if fn is None:
            continue
        try:
            verdict, evidence = fn(prompt, repo_root)
        except Exception:
            continue
        if verdict in ("verified", "refuted"):
            results.append((ctype, verdict, evidence))
            if len(results) >= MAX_LIVE_VERIFICATIONS:
                break
    return results


def main() -> int:
    inp = _read_input()
    prompt = inp.get("prompt") or inp.get("user_prompt") or ""
    if not prompt:
        return 0

    hints = _extract_hints(prompt)

    # ── doc_claims cache check (existing behavior) ──────────────────
    matches: list[tuple[str, tuple[str, str, str, str | None]]] = []
    if hints:
        seen: set[str] = set()
        for h in hints:
            for row in _query_claims(h):
                key = (row[1] or "") + "|" + (row[2] or "")
                if key in seen:
                    continue
                seen.add(key)
                matches.append((h, row))
                if len(matches) >= MAX_CLAIMS_TO_SHOW:
                    break

    # ── Phase 2: live verifiers ─────────────────────────────────────
    live_results = _run_live_verifiers(prompt)

    if not matches and not live_results:
        return 0

    if matches:
        print("[Null] doc_claims relevant to this prompt:")
        for hint, (status, source_path, claim_text, evidence) in matches:
            src = (source_path or "?").rsplit("/", 1)[-1]
            marker = {"refuted": "✗", "verified": "✓", "stale": "·"}.get(
                status, "?",
            )
            print(f"  {marker} [{status}] {src}: \"{claim_text[:80]}\"")
            if status == "refuted" and evidence:
                print(f"      evidence: {evidence[:140]}")
        print(
            "[Null] Treat refuted (✗) and stale (·) claims as suspect — "
            "verify against live state (git log / file system) before "
            "asserting."
        )

    if live_results:
        print("[Null verify] live-system check:")
        for ctype, verdict, evidence in live_results:
            marker = "✓" if verdict == "verified" else "✗"
            if verdict == "refuted" and evidence:
                print(f"  {marker} {ctype}: {evidence[:160]}")
            else:
                print(f"  {marker} {ctype}: verified against live system")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never block the user's turn for any hook error.
        sys.exit(0)
