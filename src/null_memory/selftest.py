"""`null selftest` — the RESPONSIVENESS CONTRACT release gate.

Integration smoke test: spawns a FRESH null MCP server subprocess on a
throwaway store, speaks newline-delimited JSON-RPC over its stdio, and
exercises EVERY tool on the 15-tool surface against a per-tool time
budget. Catches the class of bug unit tests can't: a real 9-minute
null_identity hang once shipped while 1172 unit tests passed.

Contract (the release gate — no release ships with this red):
  * every tool gets a budget: 10s default, 20s for the heavy boot-path
    tools (null_identity, null_briefing); scaled by --budget and by the
    NULL_SELFTEST_BUDGET_MULT env var (e.g. 3 on slow CI).
  * statuses: OK (under budget), SLOW (over budget but answered before
    the kill deadline), FAIL (error result / dead server), TIMEOUT (no
    answer by the kill deadline — the server is killed and a fresh one
    is spawned so the remaining tools still get probed).
  * any non-OK row → nonzero exit (TIMEOUT is the hang class the product
    mandate forbids; FAIL/SLOW are regressions on the same contract).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Any

from null_memory.fsutil import force_rmtree

# Per-tool budgets (seconds). Base applies to every tool not listed in
# _HEAVY_FACTOR; the heavy boot-path tools get 2x. NULL_SELFTEST_BUDGET_MULT
# multiplies everything (slow CI knob) without weakening the relative
# contract between tools.
SELFTEST_BASE_BUDGET = 10.0
SELFTEST_BUDGET_MULT_ENV = "NULL_SELFTEST_BUDGET_MULT"
_HEAVY_FACTOR = {
    "null_identity": 2.0,  # historical 9-minute hang lives here
    "null_briefing": 2.0,  # builds the largest payload on the surface
}
# A probe that hasn't answered by budget * KILL_FACTOR is declared hung:
# TIMEOUT status, server killed, fresh server spawned for the remainder.
_KILL_FACTOR = 3.0

# The 15-tool surface, every tool exercised at least once. Probe order is
# load-bearing: earlier probes seed the store (a fact, a decision) so the
# later read/mutate probes exercise their real paths, and null_close runs
# last because it closes the session.
SELFTEST_PROBES: list[tuple[str, dict]] = [
    ("null_status", {}),
    ("null_identity", {}),
    ("null_briefing", {}),
    ("null_remember", {"kind": "observe",
                       "text": "null selftest probe fact"}),
    ("null_remember", {"kind": "decide",
                       "text": "selftest decision probe",
                       "why": "exercise the decision/outcome loop"}),
    ("null_recall", {"query": "selftest probe"}),
    ("null_verify", {"mode": "fact", "query": "selftest probe fact"}),
    ("null_context", {"project": "global"}),
    ("null_catchup", {"source": "manual",
                      "facts": ["selftest manual catchup fact"]}),
    ("null_outcome", {"decision_query": "selftest decision probe",
                      "outcome": "selftest outcome recorded",
                      "success": "true"}),
    ("null_anchor", {"query": "selftest probe fact",
                     "anchor_type": "origin", "note": "selftest"}),
    ("null_exemplar", {"action": "search", "query": "selftest"}),
    ("null_multiverse", {"action": "list"}),
    ("null_forget", {"query": "selftest manual catchup fact"}),
    ("null_checkpoint", {}),
    ("null_close", {"summary": "selftest session close"}),
]

# The canonical surface — kept in (test-enforced) sync with the server's
# registered tools so a new tool can't ship without a selftest probe.
SELFTEST_TOOL_SURFACE = frozenset(name for name, _ in SELFTEST_PROBES)


def budget_multiplier() -> float:
    """NULL_SELFTEST_BUDGET_MULT as a float ≥ 0; garbage/unset → 1.0."""
    raw = os.environ.get(SELFTEST_BUDGET_MULT_ENV, "").strip()
    if not raw:
        return 1.0
    try:
        mult = float(raw)
    except ValueError:
        return 1.0
    return mult if mult > 0 else 1.0


def budget_for(tool: str, base: float | None = None,
               mult: float | None = None) -> float:
    """Per-tool budget in seconds: base x heavy-factor x multiplier."""
    if base is None:
        base = SELFTEST_BASE_BUDGET
    if mult is None:
        mult = budget_multiplier()
    return base * _HEAVY_FACTOR.get(tool, 1.0) * mult


def run_selftest(store: str | None = None, budget: float | None = None,
                 probes: list[tuple[str, dict]] | None = None,
                 extra_env: dict[str, str] | None = None) -> dict:
    """Drive a fresh MCP server over stdio and time every surface tool.

    Args:
        store: store dir to test against (default: fresh throwaway temp dir).
        budget: base per-tool budget override in seconds (default 10.0;
            heavy tools get 2x base; NULL_SELFTEST_BUDGET_MULT scales all).
        probes: probe list override (tests only) — default: the full
            15-tool surface.
        extra_env: extra env vars for the spawned server (tests only).

    Returns a structured report::

        {
          "results": [{"tool", "seconds", "budget", "status"[, "detail"]}],
          "ok": bool,           # True iff every probe is OK
          "base_budget": float,
          "multiplier": float,
          "store": str,         # the store dir actually used
        }

    status per probe: OK / SLOW / FAIL / TIMEOUT (see module docstring).
    A TIMEOUT kills the (hung) server and spawns a fresh one against the
    same store so the remaining probes still run. The server is always
    terminated and any throwaway store removed before returning.
    """
    base = SELFTEST_BASE_BUDGET if budget is None else float(budget)
    mult = budget_multiplier()
    probe_list = SELFTEST_PROBES if probes is None else probes

    own_store = store is None
    store_dir = store or tempfile.mkdtemp(prefix="null-selftest-")

    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        **(extra_env or {}),
    }

    # ── Server harness ─────────────────────────────────────────────────
    # Encapsulated so a TIMEOUT can kill the hung server and spawn a
    # fresh one mid-run. Each harness owns its proc + reader/drainer
    # threads + response index.

    def _shutdown(srv: dict) -> None:
        """Terminate→kill the server and REAP it; never blocks more than ~10s.

        The reap is load-bearing on Windows. The server holds the store's
        sqlite db open, and Windows refuses to delete a file that any process
        still has a handle to — so if we rmtree the throwaway store while the
        child is merely *signalled* and not yet dead, the delete fails and the
        store leaks. proc.kill() is asynchronous: without the wait() after it,
        we raced the child's death. Closing our pipe ends matters for the same
        reason: no stray handles into the tree we're about to delete.
        """
        proc = srv["proc"]
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)  # kill() only signals — reap it
                except subprocess.TimeoutExpired:
                    pass
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

        # The drain threads exit on EOF once the child is gone; give them a
        # beat, then close our ends so no handle outlives the store.
        for t in srv.get("threads", ()):
            try:
                t.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _spawn_server() -> dict:
        """Spawn a fresh MCP server subprocess + drain threads.

        This is the one subprocess.Popen outside the session._run_git
        hardened wrapper (subprocess-hygiene allowlist): every wait on it
        is deadline-bounded (_wait_for), stdout/stderr are continuously
        drained by daemon threads (a full 64KB pipe buffer once
        deadlocked the server AND the selftest), and shutdown escalates
        terminate→kill. Raises RuntimeError if the handshake fails.
        """
        proc = subprocess.Popen(
            [sys.executable, "-m", "null_memory.cli", "serve", store_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        responses: dict[int, dict] = {}
        responses_lock = threading.Lock()
        stderr_tail: deque[str] = deque(maxlen=50)

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mid = msg.get("id")
                if mid is not None:
                    with responses_lock:
                        responses[mid] = msg

        def _stderr_drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_tail.append(line.rstrip("\n"))

        reader_t = threading.Thread(target=_reader, daemon=True)
        drain_t = threading.Thread(target=_stderr_drain, daemon=True)
        reader_t.start()
        drain_t.start()

        srv = {
            "proc": proc,
            "responses": responses,
            "lock": responses_lock,
            "stderr_tail": stderr_tail,
            # _shutdown joins these before closing the pipes (see its docstring).
            "threads": (reader_t, drain_t),
        }

        def _send(obj: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        def _wait_for(mid: int, timeout: float) -> dict | None:
            deadline = time.perf_counter() + timeout
            while time.perf_counter() < deadline:
                with responses_lock:
                    if mid in responses:
                        return responses[mid]
                if proc.poll() is not None:
                    # Server died — give the reader a beat to drain.
                    time.sleep(0.05)
                    with responses_lock:
                        return responses.get(mid)
                time.sleep(0.02)
            return None

        srv["send"] = _send
        srv["wait_for"] = _wait_for

        # Handshake — bounded, with stderr tail on failure.
        try:
            _send({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "selftest", "version": "1"},
                },
            })
        except BrokenPipeError:
            _shutdown(srv)
            raise RuntimeError(
                "MCP server died before initialize (broken pipe)"
                + _tail_block(stderr_tail)
            ) from None
        init = _wait_for(1, timeout=20.0)
        if init is None:
            _shutdown(srv)
            raise RuntimeError(
                "MCP server did not respond to initialize within 20s "
                "(server may have failed to boot)" + _tail_block(stderr_tail)
            )
        _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return srv

    def _tail_block(tail: deque) -> str:
        joined = "\n".join(tail)
        return f"\n--- server stderr (tail) ---\n{joined}" if joined else ""

    results: list[dict] = []
    srv = None
    try:
        srv = _spawn_server()
        next_id = 2
        for name, arguments in probe_list:
            tool_budget = budget_for(name, base=base, mult=mult)
            kill_deadline = tool_budget * _KILL_FACTOR
            call_id = next_id
            next_id += 1

            if srv is None:
                # A previous probe killed the server and respawn failed —
                # every remaining tool is unreachable, not hung.
                results.append({
                    "tool": name, "seconds": 0.0, "budget": tool_budget,
                    "status": "FAIL",
                    "detail": "server unavailable (respawn after TIMEOUT failed)",
                })
                continue

            t0 = time.perf_counter()
            try:
                srv["send"]({
                    "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                })
            except BrokenPipeError:
                # Server died mid-run — record a FAIL, respawn, continue.
                results.append({
                    "tool": name,
                    "seconds": time.perf_counter() - t0,
                    "budget": tool_budget,
                    "status": "FAIL",
                    "detail": "server died (broken pipe)"
                              + _tail_block(srv["stderr_tail"]),
                })
                _shutdown(srv)
                try:
                    srv = _spawn_server()
                except RuntimeError:
                    srv = None
                continue

            resp = srv["wait_for"](call_id, timeout=kill_deadline)
            elapsed = time.perf_counter() - t0

            if resp is None:
                if srv["proc"].poll() is None:
                    # Hung: no answer by the kill deadline and the server
                    # is still alive. THE contract violation — kill it and
                    # continue on a fresh server so the rest of the surface
                    # still gets probed.
                    status = "TIMEOUT"
                    detail = (
                        f"no response within {kill_deadline:.1f}s "
                        f"(budget {tool_budget:.1f}s x kill factor "
                        f"{_KILL_FACTOR:.0f}) — server killed"
                        + _tail_block(srv["stderr_tail"])
                    )
                else:
                    status = "FAIL"
                    detail = ("server died mid-call"
                              + _tail_block(srv["stderr_tail"]))
                results.append({
                    "tool": name, "seconds": elapsed,
                    "budget": tool_budget, "status": status,
                    "detail": detail,
                })
                _shutdown(srv)
                try:
                    srv = _spawn_server()
                except RuntimeError:
                    srv = None
                continue

            if "error" in resp:
                status = "FAIL"
            elif isinstance(resp.get("result"), dict) and resp["result"].get("isError"):
                status = "FAIL"
            elif elapsed > tool_budget:
                status = "SLOW"
            else:
                status = "OK"

            result = {
                "tool": name, "seconds": elapsed,
                "budget": tool_budget, "status": status,
            }
            if status == "FAIL":
                tail = _tail_block(srv["stderr_tail"])
                result["detail"] = (
                    "error result: "
                    + json.dumps(resp.get("error") or resp.get("result"))[:500]
                    + tail
                )
            results.append(result)
    finally:
        if srv is not None:
            _shutdown(srv)
        if own_store:
            # force_rmtree, not rmtree(ignore_errors=True): the store is a
            # git repo, and git's read-only loose objects make Windows
            # refuse the delete (WinError 5). ignore_errors swallowed that,
            # so every Windows selftest silently leaked its store into TEMP.
            force_rmtree(store_dir)

    ok = bool(results) and all(r["status"] == "OK" for r in results)
    return {
        "results": results,
        "ok": ok,
        "base_budget": base,
        "multiplier": mult,
        "store": store_dir,
    }


def format_report(report: dict) -> list[str]:
    """Render the budget table + summary as printable lines."""
    results = report["results"]
    lines = [
        f"[Null] selftest — responsiveness contract "
        f"(base budget {report['base_budget']:.1f}s, "
        f"multiplier x{report['multiplier']:.1f})",
        f"  {'tool':22} {'elapsed':>9} {'budget':>8}  status",
        f"  {'-' * 22} {'-' * 9} {'-' * 8}  {'-' * 7}",
    ]
    for r in results:
        lines.append(
            f"  {r['tool']:22} {r['seconds']:9.3f} {r['budget']:8.1f}  "
            f"{r['status']}"
        )
        if r["status"] in ("FAIL", "TIMEOUT") and r.get("detail"):
            lines.extend(f"      {ln}" for ln in r["detail"].splitlines())

    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("OK", "SLOW", "FAIL", "TIMEOUT")}
    lines.append(
        f"  Summary: {counts['OK']} OK, {counts['SLOW']} SLOW, "
        f"{counts['FAIL']} FAIL, {counts['TIMEOUT']} TIMEOUT "
        f"of {len(results)} probes"
    )
    if not report["ok"]:
        lines.append(
            "  RELEASE GATE: RED — every probe must be OK to ship. "
            "TIMEOUT means a tool HUNG (the server was killed to continue)."
        )
    return lines


def handle_selftest(args: Any) -> int:
    """Handle `null selftest` — print the budget table, return exit code."""
    report = run_selftest(store=args.store, budget=args.budget)
    for line in format_report(report):
        print(line)
    return 0 if report["ok"] else 1
