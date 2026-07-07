"""Tests for the SessionStart hook's zero-dependency identity loading.

The resilience bridge writes <agent_dir>/IDENTITY.md; the SessionStart hook
must print it into context using stdlib only, so identity survives the Null
MCP server being down, hung, or even uninstallable. Three paths:

  · snapshot present  — printed with an age header
  · snapshot missing  — gentle sync-anchors suggestion, exit 0, no traceback
  · resilience        — the snapshot-print path uses stdlib only (no
                        null_memory import anywhere in the script)

NOTE: the snapshot never contains the plaintext code word — the writer
(identity_payload.render_identity_markdown) emits only a 12-char SHA-256
fingerprint. Fixtures here mirror that format using an obviously-dummy
string; the real code word must never appear in this repo.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

# Obviously-dummy stand-in — NOT the real code word. Only its fingerprint
# appears in the fixture, matching what identity_payload's writer produces.
DUMMY_CODE_WORD = "dummy-code-word-for-tests-only"
DUMMY_FINGERPRINT = hashlib.sha256(
    DUMMY_CODE_WORD.encode("utf-8")
).hexdigest()[:12]
FINGERPRINT_LINE = (
    f"**Verification fingerprint (SHA-256 prefix):** `{DUMMY_FINGERPRINT}`"
)
SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "null-session-hook.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("null_session_hook", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hook(tmp_path):
    """Run the hook end-to-end as Claude Code would: JSON on stdin."""
    env = {**os.environ, "NULL_DIR": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=json.dumps({"hook_event_name": "SessionStart"}),
        capture_output=True, text=True, timeout=30, env=env,
    )


def test_snapshot_printed_when_present(tmp_path):
    (tmp_path / "IDENTITY.md").write_text(
        "# Atlas — Identity Snapshot\n\n## Code Word\n\n"
        f"{FINGERPRINT_LINE}\n",
        encoding="utf-8",
    )
    result = _run_hook(tmp_path)
    assert result.returncode == 0
    assert DUMMY_FINGERPRINT in result.stdout
    assert "identity snapshot (age:" in result.stdout
    assert "Traceback" not in result.stderr


def test_missing_snapshot_suggests_sync_anchors(tmp_path):
    result = _run_hook(tmp_path)
    assert result.returncode == 0
    assert "sync-anchors" in result.stdout
    assert "Traceback" not in result.stderr


def test_snapshot_truncated_at_line_cap(tmp_path):
    lines = ["# Atlas — Identity Snapshot", FINGERPRINT_LINE]
    lines += [f"filler line {i}" for i in range(500)]
    (tmp_path / "IDENTITY.md").write_text("\n".join(lines), encoding="utf-8")
    result = _run_hook(tmp_path)
    assert result.returncode == 0
    assert DUMMY_FINGERPRINT in result.stdout
    assert "(snapshot truncated)" in result.stdout
    # cap honored: nothing close to 500 filler lines made it through
    assert "filler line 400" not in result.stdout


def test_snapshot_truncated_at_byte_cap(tmp_path):
    """A file over the byte cap but under the line cap must still get the
    truncation marker — the capped read drops content mid-line."""
    lines = ["# Atlas — Identity Snapshot", FINGERPRINT_LINE]
    # ~10 long lines blow past 6KB while staying far under 120 lines.
    lines += [f"long anchor {i}: " + ("x" * 1024) for i in range(10)]
    (tmp_path / "IDENTITY.md").write_text("\n".join(lines), encoding="utf-8")
    result = _run_hook(tmp_path)
    assert result.returncode == 0
    assert DUMMY_FINGERPRINT in result.stdout
    assert "(snapshot truncated)" in result.stdout


def test_stdlib_only_no_null_memory_import():
    """The whole script must work without null_memory installed — assert no
    import of it exists anywhere (static check, stronger than runtime)."""
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("null_memory") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("null_memory")


def test_print_identity_snapshot_unreadable_never_raises(tmp_path, monkeypatch):
    """Even an unreadable snapshot must not break the hook."""
    mod = _load_module()
    snap = tmp_path / "IDENTITY.md"
    snap.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "builtins.open",
        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = mod.print_identity_snapshot(str(tmp_path))
    assert ok is False
    assert "unreadable" in buf.getvalue()
