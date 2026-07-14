"""PostToolUse hook — observe file modifications made by the agent.

Fires after Write/Edit/MultiEdit tools. Records a lightweight observation
to Null so:
  - Future briefings know what files were touched this session
  - Cross-instance working memory surfaces "Atlas changed X" to other instances
  - Hypnos can correlate code changes with decisions and outcomes

Reads tool input JSON from stdin (Claude Code hook contract).

Disable: NULL_FILE_OBSERVE=0 env var.
Latency budget: <100ms. Best-effort — silent on any exception.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/.null/unified.db")
WATCHED_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Skip noisy paths
SKIP_PATH_PREFIXES = (
    "/tmp/",
    "/private/tmp/",
    "/var/folders/",
)
SKIP_PATH_SUFFIXES = (
    ".pyc",
    ".log",
    ".cache",
    ".db-wal",
    ".db-shm",
)


def _should_skip(path: str) -> bool:
    if not path:
        return True
    for prefix in SKIP_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    for suffix in SKIP_PATH_SUFFIXES:
        if path.endswith(suffix):
            return True
    return False


def _extract_paths(payload: dict) -> list[tuple[str, str]]:
    """Return list of (tool_name, file_path) from hook input."""
    tool = payload.get("tool_name") or payload.get("toolName") or ""
    if tool not in WATCHED_TOOLS:
        return []

    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    paths: list[tuple[str, str]] = []

    # Write / Edit / NotebookEdit: single file_path
    if "file_path" in tool_input:
        paths.append((tool, str(tool_input["file_path"])))
    # MultiEdit: edits list with file_path each
    elif "edits" in tool_input and isinstance(tool_input["edits"], list):
        for edit in tool_input["edits"]:
            if isinstance(edit, dict) and "file_path" in edit:
                paths.append((tool, str(edit["file_path"])))

    return [(t, p) for t, p in paths if not _should_skip(p)]


def _record(conn: sqlite3.Connection, tool: str, path: str) -> None:
    """Write a low-weight observation."""
    # Derive project from path (best effort)
    project = "global"
    repos_marker = "/Repos/"
    if repos_marker in path:
        after = path.split(repos_marker, 1)[1]
        project = after.split("/", 1)[0].lower() if after else "global"

    now = datetime.now(timezone.utc).isoformat()
    fact_id = str(uuid.uuid4())
    short_path = path.replace(os.path.expanduser("~"), "~")
    fact_text = f"[file-change] {tool}: {short_path}"

    # Direct insert — bypass full observe pipeline for speed
    try:
        conn.execute(
            """INSERT INTO facts
               (id, fact, source, project, tier, confidence, impact,
                created_at, last_accessed, access_count, archived,
                forgotten, provenance)
               VALUES (?, ?, 'hook', ?, 'ephemeral', 0.4, 0.2,
                       ?, ?, 0, 0, 0, 'file_change_hook')""",
            (fact_id, fact_text, project, now, now),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Schema mismatch — silent. Don't block the agent.
        pass


def main() -> None:
    if os.environ.get("NULL_FILE_OBSERVE", "1") == "0":
        return
    if not os.path.exists(DB_PATH):
        return

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    paths = _extract_paths(payload)
    if not paths:
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=1.0)
        for tool, path in paths:
            _record(conn, tool, path)
        conn.close()
    except sqlite3.Error:
        return


if __name__ == "__main__":
    start = time.perf_counter()
    try:
        main()
    except Exception:
        # Hook MUST NOT crash the agent. Swallow everything.
        pass
    # Optional debug timing
    if os.environ.get("NULL_HOOK_DEBUG"):
        elapsed_ms = (time.perf_counter() - start) * 1000
        print(f"[null-file-change-hook] {elapsed_ms:.1f}ms", file=sys.stderr)
