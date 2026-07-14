"""Hypnos Stage 8 — worktree doc audit.

Scans markdown docs (CLAUDE.md, ATLAS_HANDOFF*.md, HANDOFF*.md) and
extracts claims about live system state. Each claim is upserted into
doc_claims and, where possible, auto-verified against the live system:

  • file_ref       — "src/X.py exists" / "function at path:line"
  • function_ref   — "function foo() in module bar"
  • ship_status    — "Phase X is shipped" / "Phase Y hasn't shipped"
  • schema_version — "schema version is N"
  • other          — no auto-verifier, marked unverified

The claim's status flips to 'verified', 'refuted', or 'stale' based on
the live check. `refute_evidence` is populated when refuted so the
briefing can show the receipt ("doc says Phase 7.3 TODO, but git log
shows commit 7ebb06e").

Pure function shape (audit_doc) — LLM is dependency-injected so the
test suite can run without API calls.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


MAX_DOC_SIZE_BYTES = 50_000  # skip pathological files
MAX_CLAIMS_PER_DOC = 30      # ceiling per single audit pass

CLAIM_TYPES = (
    "file_ref", "function_ref", "ship_status",
    "schema_version", "other",
)

EXTRACT_PROMPT = """\
You are auditing a project documentation file for claims about live
system state — claims that would become wrong if code changes after
the doc was written. Extract each such claim along with its TYPE.

Types:
  • file_ref       — references a specific file path (e.g. "src/X.py:42")
  • function_ref   — references a function or class by name
  • ship_status    — claims a feature/phase is shipped, planned, or TODO
  • schema_version — claims about schema or version numbers
  • other          — any other live-state claim worth tracking

DO NOT extract:
  • generic prose ("the system supports X")
  • opinions ("this approach is cleaner")
  • forward-looking aspirations without a concrete reference

Return JSON ONLY:
{"claims": [{"text": "<verbatim from doc>", "type": "<one of the types>"}]}

DOCUMENT (%s):
%s
"""


def audit_doc(
    path: str,
    conn,
    llm_call: Callable[[str], str],
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Extract claims from `path`, upsert into doc_claims, verify each.

    Returns counts dict:
        {extracted, verified, refuted, stale, unverified, skipped_reason}

    repo_root: passed to the verifier for git/file resolution. Defaults
    to the parent directory of `path`'s containing repo (best-effort
    walk up looking for .git).
    """
    counts: dict[str, Any] = {
        "extracted": 0, "verified": 0, "refuted": 0,
        "stale": 0, "unverified": 0, "skipped_reason": None,
    }

    try:
        size = os.path.getsize(path)
    except OSError:
        counts["skipped_reason"] = "file not found"
        return counts
    if size > MAX_DOC_SIZE_BYTES:
        counts["skipped_reason"] = f"file too large ({size}B)"
        return counts

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            doc_text = f.read()
    except OSError as exc:
        counts["skipped_reason"] = f"read failed: {exc}"
        return counts

    if not doc_text.strip():
        counts["skipped_reason"] = "empty file"
        return counts

    if repo_root is None:
        repo_root = _find_repo_root(path)

    # 1. Extract via LLM
    try:
        raw = llm_call(EXTRACT_PROMPT % (os.path.basename(path), doc_text))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[doc_audit] LLM call failed for %s: %s", path, exc)
        counts["skipped_reason"] = f"llm failed: {exc}"
        return counts

    claims = _parse_claims(raw)
    if claims is None:
        counts["skipped_reason"] = "llm output unparseable"
        return counts

    counts["extracted"] = len(claims)
    if len(claims) > MAX_CLAIMS_PER_DOC:
        claims = claims[:MAX_CLAIMS_PER_DOC]

    now = datetime.now(timezone.utc).isoformat()

    for claim in claims:
        text = (claim.get("text") or "").strip()
        ctype = claim.get("type") or "other"
        if not text or ctype not in CLAIM_TYPES:
            continue

        # Upsert (UNIQUE on path + text)
        conn.execute(
            """INSERT INTO doc_claims
               (source_path, claim_text, claim_type, extracted_at,
                last_seen_at, status)
               VALUES (?, ?, ?, ?, ?, 'unverified')
               ON CONFLICT(source_path, claim_text) DO UPDATE SET
                 last_seen_at = excluded.last_seen_at,
                 claim_type = excluded.claim_type""",
            (path, text, ctype, now, now),
        )

        # Verify
        verdict, evidence = _verify_claim(text, ctype, repo_root)
        if verdict in ("verified", "refuted"):
            conn.execute(
                """UPDATE doc_claims
                   SET status = ?, last_verified_at = ?,
                       refute_evidence = ?
                   WHERE source_path = ? AND claim_text = ?""",
                (verdict, now, evidence if verdict == "refuted" else None,
                 path, text),
            )
        counts[verdict if verdict in counts else "unverified"] += 1

    conn.commit()

    # Mark claims that were in the table but didn't appear in this scan
    # as stale (last_seen_at older than this run's `now`).
    cur = conn.execute(
        """SELECT COUNT(*) FROM doc_claims
           WHERE source_path = ? AND last_seen_at < ?""",
        (path, now),
    )
    counts["stale"] = cur.fetchone()[0]
    conn.execute(
        """UPDATE doc_claims SET status = 'stale'
           WHERE source_path = ? AND last_seen_at < ? AND status != 'refuted'""",
        (path, now),
    )
    conn.commit()

    return counts


def _parse_claims(raw: str) -> list[dict[str, str]] | None:
    raw = raw.strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None
    items = obj.get("claims")
    if not isinstance(items, list):
        return None
    out: list[dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = it.get("text")
        ctype = it.get("type")
        if isinstance(text, str) and isinstance(ctype, str):
            out.append({"text": text.strip(), "type": ctype.strip()})
    return out


def _find_repo_root(path: str) -> str:
    """Walk up from path looking for a .git directory."""
    cur = os.path.dirname(os.path.abspath(path))
    for _ in range(10):
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.dirname(os.path.abspath(path))


# ── Verifiers ────────────────────────────────────────────────────────


def _verify_claim(text: str, ctype: str,
                  repo_root: str) -> tuple[str, str | None]:
    """Return (verdict, evidence_if_refuted).

    verdict ∈ {'verified', 'refuted', 'unverified'}
    """
    try:
        if ctype == "file_ref":
            return _verify_file_ref(text, repo_root)
        if ctype == "function_ref":
            return _verify_function_ref(text, repo_root)
        if ctype == "ship_status":
            return _verify_ship_status(text, repo_root)
        if ctype == "schema_version":
            return _verify_schema_version(text, repo_root)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[doc_audit] verifier crashed (%s): %s", ctype, exc)
    return "unverified", None


_PATH_RE = re.compile(r"([~\w\-./]+\.(?:py|ts|tsx|md|json|sh|sql))(?::(\d+))?")


def _verify_file_ref(text: str, repo_root: str) -> tuple[str, str | None]:
    """Verify by extracting file paths from claim text and checking
    existence relative to repo_root."""
    matches = _PATH_RE.findall(text)
    if not matches:
        return "unverified", None
    missing: list[str] = []
    for path_str, _line in matches:
        # Strip leading prose punctuation
        candidate = path_str.lstrip("`'\"")
        # Expand ~ — claims like "~/.claude.json" should resolve against
        # the user home, not get joined onto repo_root.
        if candidate.startswith("~"):
            full = os.path.expanduser(candidate)
        elif os.path.isabs(candidate):
            full = candidate
        else:
            full = os.path.join(repo_root, candidate)
        if not os.path.exists(full):
            missing.append(candidate)
    if missing:
        return "refuted", f"missing files: {', '.join(missing)}"
    return "verified", None


_FUNC_RE = re.compile(r"\b([a-zA-Z_][\w]*)\s*\(")


def _verify_function_ref(text: str, repo_root: str) -> tuple[str, str | None]:
    """Verify by grepping for the function name in repo_root.
    Conservative: only refutes if the name appears nowhere.
    """
    candidates = _FUNC_RE.findall(text)
    if not candidates:
        return "unverified", None
    for name in candidates:
        # Skip noise words.
        if name.lower() in {"the", "a", "an", "if", "for", "in", "is"}:
            continue
        try:
            r = subprocess.run(
                ["grep", "-r", "-l", name, repo_root, "--include=*.py",
                 "--include=*.ts", "--include=*.tsx"],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip():
                return "verified", None
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
    return "refuted", f"no source file mentions: {', '.join(candidates)}"


_PHASE_RE = re.compile(r"phase[\s_-]*(\d+(?:\.\d+)?)", re.I)


def _verify_ship_status(text: str, repo_root: str) -> tuple[str, str | None]:
    """Try to infer Phase number + ship/unship intent. If the doc
    claims something is unshipped/TODO but git log mentions it, refute."""
    phase_m = _PHASE_RE.search(text)
    if not phase_m:
        return "unverified", None
    phase = phase_m.group(1)
    lower = text.lower()
    claims_unshipped = any(
        kw in lower for kw in
        ("hasn't shipped", "not shipped", "todo", "pending",
         "to do", "next session", "future work", "didn't")
    )
    try:
        r = subprocess.run(
            ["git", "-C", repo_root, "log", "--all", "--oneline", "--grep",
             f"phase {phase}", "-i"],
            capture_output=True, text=True, timeout=10,
        )
        log = r.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unverified", None

    if claims_unshipped and log:
        first_line = log.splitlines()[0]
        return "refuted", (
            f"doc claims phase {phase} unshipped, but git log shows: {first_line}"
        )
    if not claims_unshipped and log:
        return "verified", None
    return "unverified", None


# Match "schema [version]" followed (within ~30 chars of natural prose:
# "is", "=", ":", "of", etc.) by an optional 'v' and the version number.
_SCHEMA_RE = re.compile(
    r"schema(?:[\s_-]*version)?[\s:=is]{1,30}?v?(\d+)", re.I,
)


def _verify_schema_version(text: str, repo_root: str) -> tuple[str, str | None]:
    m = _SCHEMA_RE.search(text)
    if not m:
        return "unverified", None
    claimed = int(m.group(1))
    # Read live UNIFIED_SCHEMA_VERSION from migrate_v3.py
    migrate_path = os.path.join(
        repo_root, "src", "null_memory", "migrate_v3.py",
    )
    if not os.path.isfile(migrate_path):
        return "unverified", None
    try:
        with open(migrate_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("UNIFIED_SCHEMA_VERSION"):
                    actual = int(line.split("=")[1].strip())
                    if actual == claimed:
                        return "verified", None
                    return "refuted", (
                        f"doc says schema v{claimed}, live is v{actual}"
                    )
    except (OSError, ValueError):
        pass
    return "unverified", None


def default_llm_call(prompt: str) -> str:
    """Default LLM dispatcher — same Anthropic + key-loading pattern as
    crystallize.default_llm_call. Returns "" on any failure."""
    try:
        from null_memory.crystallize import _load_api_key
        import anthropic
        api_key = _load_api_key()
        if not api_key:
            return ""
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[doc_audit] default LLM call failed: %s", exc)
        return ""
