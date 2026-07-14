"""Shared fixtures for Null tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import warnings
import pytest
from datetime import datetime, timezone
from pathlib import Path

from null_memory.agent import AgentMemory


# ── Cross-platform home redirection ──────────────────────────────────────
# POSIX expanduser reads $HOME; Windows (ntpath, Python 3.8+) reads
# $USERPROFILE (falling back to HOMEDRIVE+HOMEPATH) and ignores $HOME
# entirely. Tests that only monkeypatch HOME silently hit the user's real
# profile on Windows (issue #2). Set every variable so ~ resolves to the
# sandbox on all platforms.

def set_fake_home(monkeypatch, path) -> None:
    home = str(path)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    drive, tail = os.path.splitdrive(home)
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail or os.sep)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect the user's home directory to tmp_path, cross-platform."""
    set_fake_home(monkeypatch, tmp_path)
    return tmp_path


# ── CLI subprocess helper ────────────────────────────────────────────────

class CLIReturnCode(int):
    """int returncode whose repr carries the process output, so a bare
    ``assert rc == 0`` failure shows WHY the CLI failed. Without this,
    subprocess failures surface as an undiagnosable ``assert 1 == 0``
    (the Windows rc=1 failures in issue #2 were invisible)."""

    out: str
    err: str

    def __new__(cls, rc: int, out: str, err: str):
        self = super().__new__(cls, rc)
        self.out = out
        self.err = err
        return self

    def __repr__(self) -> str:  # shown by pytest on assertion failure
        return (f"{int(self)} (stdout: {self.out[-800:]!r}, "
                f"stderr: {self.err[-800:]!r})")


def run_null(*args, env_override=None, tmp_path=None):
    """Run the null CLI; return (returncode, stdout, stderr).

    The returncode is a CLIReturnCode whose repr includes stdout/stderr,
    so ``assert rc == 0`` failures are diagnosable. Output is decoded as
    UTF-8 (the CLI reconfigures its stdio to UTF-8) — never the locale
    code page, which would mojibake/crash on Windows.
    """
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    if tmp_path is not None:
        env["NULL_DIR"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "null_memory.cli", *args],
        capture_output=True, encoding="utf-8", errors="replace", env=env,
    )
    rc = CLIReturnCode(result.returncode, result.stdout, result.stderr)
    return rc, result.stdout, result.stderr


# ── Sandbox guard ────────────────────────────────────────────────────────
# Phase 5.1 — no test may write to the real ~/.null. The outreaches.log
# leak (tests force-firing the log channel onto the user's real log file)
# was caught only after weeks of accumulation. This guard makes the class
# of bug impossible: every test gets NULL_DIR=tmp_path, and the real dir
# is snapshotted before/after; modification fails the offending test
# by name.
#
# Live-session refinement (issue #5): a live Atlas MCP session on this
# machine writes to ~/.null (memory commits, debounced git sync) DURING
# suite runs, and the original any-change-fails guard blamed whichever
# test happened to be executing — different victims every run, all green
# in isolation. The guard still snapshots the whole tree (narrowing the
# watched surface would miss new leak classes), but the diff is now
# CLASSIFIED before failing:
#
# Live-writer EVIDENCE (two independent sources, both predating the test):
#   1. pid correlation: a `null_memory.cli serve` / `... daemon` process
#      is running on this machine (pgrep, checked once per pytest process,
#      cached). This is how the live Atlas MCP session actually manifests
#      — it keeps no active_session.json pointer while idle, but its
#      server/daemon processes are alive for the whole suite run.
#   2. session marker: a directory that already contained an
#      active_session.json in the BEFORE snapshot.
#
#   * external (warn, don't fail): paths owned by the live writer —
#     marker dirs (evidence 2) always; with a running server process
#     (evidence 1), also any pre-existing dir holding agent-store files
#     (memory.db / identity.json / state.json / config.json — e.g.
#     ~/.null/atlas/** including its .git, ~/.null/personalities/hermes/**)
#     — plus the top-level shared store files the live server writes
#     (unified.db, multiverse.db, nebula_umap.pkl, daemon.log,
#     outreaches*.log), plus the PRE-EXISTING git repo at the root of
#     ~/.null (the live writer's whole-store sync commits mutate
#     .git/refs and .git/logs mid-suite — observed live during
#     verification of this very fix).
#   * leaked (fail, exactly as before): everything else — including any
#     marker or agent dir that APPEARS during the test. Only evidence from
#     the before snapshot counts, so a test leaking a session into the
#     real store can never whitelist itself.
#
# On machines with no live server process and no pre-existing
# active_session.json (CI), every change fails — full original strictness.

_REAL_NULL_DIR = Path(os.path.expanduser("~/.null"))


_IGNORED_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")

# Top-level shared store files a live null server mutates outside its
# agent dir (plus the dated outreaches*.log channel files).
_LIVE_SHARED_FILES = frozenset(
    {"unified.db", "multiverse.db", "nebula_umap.pkl", "daemon.log"})

# Files that identify their containing directory as a live agent /
# personality store when (and only when) a null server process is running
# (evidence source 1 in the module comment).
_AGENT_DIR_MARKERS = frozenset(
    {"memory.db", "identity.json", "state.json", "config.json"})

# Matches the MCP server ("python -m null_memory.cli serve") and the
# background daemon ("python -m null_memory.cli daemon run"). pgrep -f
# matches the full command line as an extended regex. No test subprocess
# matches this: run_null() never invokes `serve` or `daemon`.
_LIVE_SERVER_PGREP_PATTERN = r"null_memory\.cli (serve|daemon)"


def _detect_live_server() -> bool:
    """True iff a live null server/daemon process is running right now
    (pid correlation, issue #5). Any failure — tool missing, timeout, no
    match — means False: the guard stays fully strict.

    Windows has no pgrep (issue #42): the guard ran permanently strict
    there, so a seat's always-on daemon (the normal Phase B deployment)
    got its store writes blamed on whichever test happened to be running
    — the exact issue #5 failure mode, back for Windows. There we ask
    CIM for python command lines instead."""
    try:
        if os.name == "nt":
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_Process -Filter "
                 "\"Name LIKE 'python%'\" | "
                 "Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=15,
            )
        else:
            res = subprocess.run(
                ["pgrep", "-f", _LIVE_SERVER_PGREP_PATTERN],
                capture_output=True, text=True, timeout=5,
            )
        if res.returncode != 0 or not (res.stdout or "").strip():
            return False
        # POSIX pgrep already matched the pattern (output = pids); on
        # Windows we got raw command lines and must match here. Applying
        # the regex to pgrep's pid output is harmless-by-construction:
        # returncode 0 means a match existed, so check stdout content
        # only on Windows.
        if os.name == "nt":
            import re
            return re.search(
                _LIVE_SERVER_PGREP_PATTERN, res.stdout) is not None
        return True
    except Exception:
        return False


_live_server_cache: bool | None = None


def _live_server_running() -> bool:
    """Cached once per pytest process: the live server either runs for the
    whole suite or not at all. Caching keeps it to one pgrep total and
    prevents a test's own short-lived run_null subprocess from ever
    influencing the answer mid-suite."""
    global _live_server_cache
    if _live_server_cache is None:
        _live_server_cache = _detect_live_server()
    return _live_server_cache


def _snapshot_null_dir(root: Path | None = None) -> dict[str, tuple[int, int]]:
    """Return {relpath: (size, mtime_ns)} for every file under ``root``
    (default: the real ~/.null).

    Excludes SQLite transient files (WAL/SHM/journal) because the live
    MCP servers connected to the user's Claude Code session write those
    independently of pytest — they'd produce false positives. We still
    catch writes to the main .db files (committed state) and to logs,
    json, and any new file tests create."""
    if root is None:
        root = _REAL_NULL_DIR
    if not root.exists():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.endswith(_IGNORED_SUFFIXES):
            continue
        try:
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return out


def _live_agent_dirs(
    before: dict[str, tuple[int, int]],
    server_running: bool = False,
) -> set[str]:
    """Relative dirs owned by a live null writer that predates the test.

    Evidence (BEFORE snapshot only): a dir holding an active_session.json
    always counts; when a live server process was detected
    (``server_running``), a dir holding any agent-store marker file
    (memory.db etc.) counts too — the live Atlas store keeps no
    active_session.json pointer while idle, but the server writes its
    agent dir throughout the run. Only markers inside a subdirectory
    count: a root-level marker would whitelist the entire tree and neuter
    the guard."""
    out: set[str] = set()
    for rel in before:
        head, tail = os.path.split(rel)
        if not head:
            continue
        if tail == "active_session.json":
            out.add(head)
        elif server_running and tail in _AGENT_DIR_MARKERS:
            out.add(head)
    return out


def _root_git_preexists(before: dict[str, tuple[int, int]]) -> bool:
    """True if a git repo already existed at the ROOT of the store before
    the test. The live writer syncs the whole store via this repo, so its
    commits mutate .git/refs and .git/logs mid-suite. Pre-existence is
    required: a test leaking a `git init` into the real store is still
    caught (its .git files are all ADDED, with none in the before
    snapshot)."""
    prefix = ".git" + os.sep
    return any(rel.startswith(prefix) for rel in before)


def _exchange_clone_preexists(before: dict[str, tuple[int, int]]) -> bool:
    """True if the org-exchange clone (~/.null/exchange/, sync Phase B)
    already existed before the test. The live daemon's poke loop fetches
    and ingests it every few minutes, mutating exchange/.git/FETCH_HEAD
    and stream files mid-suite (observed live: 2 victim tests blamed per
    9-minute run). Same live-writer surface as the root .git, same
    pre-existence requirement: a test leaking a clone into the real store
    only ADDS files, so it still fails."""
    prefix = os.path.join("exchange", ".git") + os.sep
    return any(rel.startswith(prefix) for rel in before)


def _attributable_to_live_writer(
    rel: str, live_dirs: set[str], server_running: bool = False,
) -> bool:
    """True if a change at ``rel`` is plausibly the live writer's own
    write: inside a pre-existing live agent dir, or one of the top-level
    shared store files the server maintains. Nothing is attributable when
    no live writer predates the test (no marker dirs AND no server
    process) — without a live writer, every change is a leak."""
    if not live_dirs and not server_running:
        return False
    if os.sep not in rel and (
            rel in _LIVE_SHARED_FILES or rel.startswith("outreaches")):
        return True
    return any(rel == d or rel.startswith(d + os.sep) for d in live_dirs)


def _check_null_sandbox(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    server_running: bool = False,
) -> tuple[str | None, list[str]]:
    """Classify a before/after snapshot diff.

    Returns ``(failure_message_or_None, external_paths)``. A failure
    message is produced iff at least one change is NOT attributable to a
    live writer that predates the test (see module comment).
    ``server_running`` is the pid-correlation evidence — pass
    ``_live_server_running()`` in production; tests inject it."""
    if before == after:
        return None, []
    live_dirs = _live_agent_dirs(before, server_running)
    # The root .git is live-writer surface only when there is live
    # evidence AND the repo predates the test (see _root_git_preexists).
    if (live_dirs or server_running) and _root_git_preexists(before):
        live_dirs = live_dirs | {".git"}
    # Same rule for the org-exchange clone the daemon's poke loop fetches.
    if (live_dirs or server_running) and _exchange_clone_preexists(before):
        live_dirs = live_dirs | {"exchange"}
    external: list[str] = []
    leaked_parts: list[str] = []
    for kind, paths in (
        ("added", sorted(set(after) - set(before))),
        ("removed", sorted(set(before) - set(after))),
        ("changed", sorted(k for k in set(before) & set(after)
                           if before[k] != after[k])),
    ):
        leaked = [p for p in paths
                  if not _attributable_to_live_writer(
                      p, live_dirs, server_running)]
        external.extend(p for p in paths if p not in leaked)
        if leaked:
            # Join paths verbatim — repr() would escape Windows
            # backslashes ('agents\\TestAgent.json'), making the message
            # un-greppable for the literal relpath.
            leaked_parts.append(f"{kind}: [{', '.join(leaked)}]")
    if not leaked_parts:
        return None, external
    msg = (
        "Test wrote to real ~/.null (Phase 5.1 sandbox guard). "
        + "; ".join(leaked_parts)
        + (f"; concurrent external live-session writes (tolerated): "
           f"[{', '.join(external)}]" if external else "")
        + ". The test must sandbox any path it writes (use NULL_DIR env "
        "or pass an explicit tmp path to the constructor)."
    )
    return msg, external


@pytest.fixture(autouse=True)
def _null_dir_sandbox(tmp_path, monkeypatch):
    """Autouse guard: every test runs with NULL_DIR=<tmp> and is verified
    to have not mutated the real ~/.null directory. Failing-loudly is the
    whole point — silent pollution is the exact bug we're preventing.
    Changes attributable to a live null session that predates the test
    are warned about instead of failed (issue #5)."""
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    server_running = _live_server_running()
    before = _snapshot_null_dir()
    yield
    after = _snapshot_null_dir()
    failure, external = _check_null_sandbox(before, after, server_running)
    if failure is not None:
        pytest.fail(failure)
    if external:
        warnings.warn(
            "~/.null changed during this test, but every change sits on "
            "the write surface of a live null session that predates the "
            f"test (external writer, not a test leak): {external}",
            UserWarning,
        )


# Semantically distinct facts for tests that need N unique entries.
# These are different enough that embedding-based dedup won't merge them.
DISTINCT_FACTS = [
    "Python uses indentation for code blocks and variable scoping",
    "Rust has zero-cost abstractions with compile-time borrow checking",
    "PostgreSQL supports JSONB columns for semi-structured document storage",
    "tree-sitter parses abstract syntax trees incrementally for editors",
    "Redis provides in-memory key-value data structures with persistence",
    "Docker containers isolate applications using Linux kernel namespaces",
    "WebAssembly enables near-native performance for browser applications",
    "GraphQL allows clients to request exactly the data fields they need",
    "Kubernetes orchestrates container deployments across clusters of machines",
    "SQLite stores the entire database in a single cross-platform file",
    "TensorFlow performs automatic differentiation for gradient computation",
    "Nginx handles reverse proxy load balancing with event-driven architecture",
    "Git tracks content changes using directed acyclic graph of commit objects",
    "OAuth2 delegates authorization through token exchange between services",
    "Prometheus collects time-series metrics via pull-based HTTP scraping",
    "Elasticsearch indexes documents for full-text search using inverted indices",
    "RabbitMQ routes messages between producers and consumers via exchanges",
    "Terraform provisions cloud infrastructure using declarative configuration files",
    "gRPC uses protocol buffers for efficient binary serialization of messages",
    "Apache Kafka provides distributed event streaming with log-based partitions",
]


def insert_n_facts(mem: AgentMemory, n: int, confidence: float = 0.9,
                   project: str = "global") -> None:
    """Insert N semantically distinct facts directly into DB, bypassing dedup."""
    now = datetime.now(timezone.utc).isoformat()
    for i in range(n):
        fact_text = DISTINCT_FACTS[i % len(DISTINCT_FACTS)]
        if i >= len(DISTINCT_FACTS):
            fact_text = f"{fact_text} (variant {i})"
        fid = hashlib.sha256(f"{fact_text}:{project}".encode()).hexdigest()[:16]
        mem.db.insert_fact({
            "id": fid,
            "fact": fact_text,
            "confidence": confidence,
            "base_confidence": confidence,
            "project": project,
            "source": "test",
            "created_at": now,
        })
    mem.db.conn.commit()


def quiesce_mem(mem: AgentMemory, timeout: float = 5.0) -> None:
    """Deterministically stop/join any background sync work an AgentMemory
    spawned (debounce timer, immediate-flush threads, deferred session-git
    thread). Issue #5: without this, a late flush thread from one test
    landed commits inside a later test's FakeRepo assertions under load."""
    try:
        mem._join_sync_threads(timeout)
    except Exception:
        pass


@pytest.fixture
def mem(tmp_path):
    """Fresh AgentMemory with isolated temp directory. Teardown joins any
    background sync threads the test started, so nothing it spawned can
    outlive the test."""
    m = AgentMemory.load(str(tmp_path))
    yield m
    quiesce_mem(m)


@pytest.fixture
def populated_mem(tmp_path):
    """AgentMemory pre-loaded with sample data."""
    mem = AgentMemory.load(str(tmp_path))
    mem.set_name("TestAgent")

    mem.learn("Python uses indentation for blocks", confidence=0.9, project="global")
    mem.learn("Rust has zero-cost abstractions", confidence=0.95, project="hiwave")
    mem.learn("Postgres supports JSONB columns", confidence=0.85, project="website")
    mem.learn("tree-sitter parses ASTs incrementally", confidence=0.9, project="aleph")
    mem.learn("Polymarket uses fixed-6 micro units", confidence=0.8, project="arbe4")

    mem.decide("Use JSONL for storage", "human-readable, append-only, git-friendly", project="null")
    mem.decide("Remove generic parser fallback", "extracts wrong identifiers in Java/C#", project="aleph")

    yield mem
    quiesce_mem(mem)
