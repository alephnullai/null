"""Subprocess hygiene lint — enforced responsiveness contract, class B.

Hang root-cause class B: a child process spawned without an authoritative
timeout. A git credential prompt, a wedged launchctl, an interpreter probe
on a dead path — any of them turns into an indefinite hang on a path that
must respond (the 9-minute Windows MCP sync hang in issue #4 was exactly
this class). This lint AST-parses every file under src/ and FAILS on:

  * subprocess.run / call / check_call / check_output without a `timeout`
    keyword (or with an explicit `timeout=None`),
  * subprocess.Popen anywhere outside the approved hardened wrappers
    (Popen has no timeout of its own — every wait on the handle must be
    deadline-bounded, which only an audited wrapper can guarantee),
  * os.system / os.popen anywhere (no timeout support at all — banned,
    never allowlistable).

New subprocess use either passes a timeout or earns an allowlist entry by
implementing the hardened pattern (deadline-bounded waits, pipes drained,
terminate→kill escalation) — and documenting WHY next to the entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# ── Approved hardened wrappers ───────────────────────────────────────────
# Keyed by (path relative to src/, enclosing function name). Every entry
# must explain why its subprocess use is safe WITHOUT a plain timeout kw.
# An entry that no longer matches a real call site fails the stale-entry
# test below, so the allowlist can't rot.
ALLOWLIST: dict[tuple[str, str], str] = {
    ("null_memory/session.py", "_run_git"):
        # The canonical hardened git wrapper (issue #4). Popen + its own
        # process group/tree, communicate(timeout=...) with a kill of the
        # WHOLE tree on expiry — authoritative even when a credential-
        # helper grandchild inherits and holds the pipes after git dies
        # (subprocess.run's timeout is not authoritative there).
        "hardened git wrapper: process-group Popen, communicate(timeout), "
        "tree-kill on expiry",
    ("null_memory/selftest.py", "_spawn_server"):
        # The selftest's MCP-server-under-test. Long-lived BY DESIGN (it
        # serves every probe), so no single timeout applies: every wait on
        # it is deadline-bounded (_wait_for), stdout/stderr are drained by
        # daemon threads (a full 64KB pipe buffer once deadlocked server
        # AND selftest), and shutdown escalates terminate→kill.
        "selftest server harness: deadline-bounded waits, drained pipes, "
        "terminate-then-kill shutdown",
}

# subprocess functions that accept (and must be given) a timeout.
_TIMEOUT_REQUIRED = {"run", "call", "check_call", "check_output"}
# subprocess functions with no timeout of their own — wrapper-only.
_WRAPPER_ONLY = {"Popen"}
# os functions that are banned outright (no timeout support at all).
_BANNED_OS = {"system", "popen"}


class Violation:
    def __init__(self, path: str, lineno: int, func: str, call: str,
                 reason: str):
        self.path = path
        self.lineno = lineno
        self.func = func          # enclosing function name ("<module>" at top level)
        self.call = call          # e.g. "subprocess.run"
        self.reason = reason

    def __repr__(self) -> str:
        return (f"{self.path}:{self.lineno} in {self.func}: "
                f"{self.call} — {self.reason}")


def _has_real_timeout(node: ast.Call) -> bool:
    """True iff the call passes a timeout kw that isn't the literal None."""
    for kw in node.keywords:
        if kw.arg == "timeout":
            return not (isinstance(kw.value, ast.Constant)
                        and kw.value.value is None)
        if kw.arg is None:
            # **kwargs splat — can't prove a timeout is inside, but the
            # existing hardened call sites never splat; treat as missing
            # so a splat can't be used to dodge the lint.
            continue
    return False


def scan_source(text: str, rel_path: str,
                allowlist: dict[tuple[str, str], str] | None = None,
                ) -> tuple[list[Violation], set[tuple[str, str]]]:
    """Lint one file's source. Returns (violations, allowlist keys used)."""
    if allowlist is None:
        allowlist = ALLOWLIST
    tree = ast.parse(text, filename=rel_path)

    # Resolve module aliases (`import subprocess as sp`) and from-imports
    # (`from subprocess import run as r`) so neither dodges the lint.
    sub_aliases: set[str] = set()      # names bound to the subprocess module
    os_aliases: set[str] = set()       # names bound to the os module
    from_subprocess: dict[str, str] = {}  # local name -> subprocess attr
    from_os: dict[str, str] = {}          # local name -> os attr
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    sub_aliases.add(alias.asname or "subprocess")
                elif alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    from_subprocess[alias.asname or alias.name] = alias.name
            elif node.module == "os":
                for alias in node.names:
                    from_os[alias.asname or alias.name] = alias.name

    def _resolve(node: ast.Call) -> tuple[str, str] | None:
        """Return ("subprocess"|"os", attr) for a lint-relevant call."""
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id in sub_aliases:
                return ("subprocess", f.attr)
            if f.value.id in os_aliases:
                return ("os", f.attr)
        elif isinstance(f, ast.Name):
            if f.id in from_subprocess:
                return ("subprocess", from_subprocess[f.id])
            if f.id in from_os:
                return ("os", from_os[f.id])
        return None

    violations: list[Violation] = []
    used_allowlist: set[tuple[str, str]] = set()

    def _visit(node: ast.AST, func_name: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name
        if isinstance(node, ast.Call):
            resolved = _resolve(node)
            if resolved is not None:
                mod, attr = resolved
                call_repr = f"{mod}.{attr}"
                key = (rel_path, func_name)
                if mod == "os" and attr in _BANNED_OS:
                    violations.append(Violation(
                        rel_path, node.lineno, func_name, call_repr,
                        "banned — no timeout support; use subprocess.run "
                        "with timeout",
                    ))
                elif mod == "subprocess" and attr in _WRAPPER_ONLY:
                    if key in allowlist:
                        used_allowlist.add(key)
                    else:
                        violations.append(Violation(
                            rel_path, node.lineno, func_name, call_repr,
                            "Popen outside an approved hardened wrapper — "
                            "use session._run_git for git, or earn an "
                            "allowlist entry (deadline-bounded waits, "
                            "drained pipes, terminate→kill)",
                        ))
                elif (mod == "subprocess" and attr in _TIMEOUT_REQUIRED
                        and not _has_real_timeout(node)):
                    if key in allowlist:
                        used_allowlist.add(key)
                    else:
                        violations.append(Violation(
                            rel_path, node.lineno, func_name, call_repr,
                            "missing authoritative timeout= (hang class B)",
                        ))
        for child in ast.iter_child_nodes(node):
            _visit(child, func_name)

    _visit(tree, "<module>")
    return violations, used_allowlist


def scan_tree(root: Path) -> tuple[list[Violation], set[tuple[str, str]]]:
    """Lint every .py file under root. Paths reported relative to root."""
    violations: list[Violation] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        v, u = scan_source(path.read_text(encoding="utf-8"), rel)
        violations.extend(v)
        used.update(u)
    return violations, used


# ── THE enforcement test ─────────────────────────────────────────────────

class TestSrcTreeHygiene:
    def test_src_tree_has_no_unhardened_subprocess_calls(self):
        violations, _ = scan_tree(SRC_ROOT)
        assert not violations, (
            "Subprocess hygiene violations (hang root-cause class B — "
            "every child process needs an authoritative timeout or an "
            "audited hardened wrapper):\n  "
            + "\n  ".join(repr(v) for v in violations)
        )

    def test_allowlist_has_no_stale_entries(self):
        """Every allowlist entry must shield a REAL call site.

        A stale entry is a loaded gun: it pre-approves unhardened
        subprocess use in a function that no longer earns it."""
        _, used = scan_tree(SRC_ROOT)
        stale = set(ALLOWLIST) - used
        assert not stale, f"stale allowlist entries: {sorted(stale)}"


# ── Lint self-tests (fixture sources prove the lint actually bites) ─────

class TestLintSelfTest:
    def _scan(self, src: str, allowlist=None):
        violations, _ = scan_source(src, "fixture.py",
                                    allowlist=allowlist or {})
        return violations

    def test_run_without_timeout_flagged(self):
        v = self._scan(
            "import subprocess\n"
            "subprocess.run(['git', 'status'], capture_output=True)\n"
        )
        assert len(v) == 1
        assert v[0].call == "subprocess.run"
        assert "timeout" in v[0].reason

    def test_run_with_timeout_clean(self):
        v = self._scan(
            "import subprocess\n"
            "subprocess.run(['git', 'status'], timeout=10)\n"
        )
        assert v == []

    def test_run_with_timeout_none_flagged(self):
        v = self._scan(
            "import subprocess\n"
            "subprocess.run(['x'], timeout=None)\n"
        )
        assert len(v) == 1

    def test_run_with_variable_timeout_clean(self):
        v = self._scan(
            "import subprocess\n"
            "def f(t):\n"
            "    subprocess.run(['x'], timeout=t)\n"
        )
        assert v == []

    def test_popen_flagged_outside_allowlist(self):
        v = self._scan(
            "import subprocess\n"
            "def spawn():\n"
            "    return subprocess.Popen(['sleep', '99'])\n"
        )
        assert len(v) == 1
        assert v[0].call == "subprocess.Popen"
        assert v[0].func == "spawn"

    def test_popen_allowlisted_function_clean(self):
        src = (
            "import subprocess\n"
            "def hardened():\n"
            "    return subprocess.Popen(['x'])\n"
        )
        v, used = scan_source(
            src, "fixture.py",
            allowlist={("fixture.py", "hardened"): "test entry"},
        )
        assert v == []
        assert used == {("fixture.py", "hardened")}

    def test_check_output_without_timeout_flagged(self):
        v = self._scan(
            "import subprocess\n"
            "subprocess.check_output(['uname'])\n"
        )
        assert len(v) == 1

    def test_os_system_banned(self):
        v = self._scan("import os\nos.system('rm -rf /tmp/x')\n")
        assert len(v) == 1
        assert "banned" in v[0].reason

    def test_os_popen_banned_even_when_allowlisted(self):
        # os.system/os.popen are never allowlistable.
        src = "import os\ndef f():\n    os.popen('ls')\n"
        v, _ = scan_source(src, "fixture.py",
                           allowlist={("fixture.py", "f"): "nice try"})
        assert len(v) == 1

    def test_module_alias_does_not_dodge_lint(self):
        v = self._scan(
            "import subprocess as sp\n"
            "sp.run(['x'])\n"
        )
        assert len(v) == 1

    def test_from_import_does_not_dodge_lint(self):
        v = self._scan(
            "from subprocess import run as launch\n"
            "launch(['x'])\n"
        )
        assert len(v) == 1

    def test_unrelated_calls_ignored(self):
        v = self._scan(
            "import subprocess\n"
            "def run(x):\n"
            "    return x\n"
            "run(1)\n"            # local fn named run — not subprocess.run
            "print('os.system')\n"  # string mention, not a call
        )
        assert v == []
