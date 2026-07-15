"""Phase 5.1 sandbox guard — diff classification (issue #5).

The guard must keep failing tests that leak writes into the real ~/.null,
while tolerating (warn, not fail) writes made by a live null session that
predates the test. These tests exercise the guard's own snapshot/classify
helpers against a tmp-path stand-in for ~/.null — they never touch the
real ~/.null.
"""

from __future__ import annotations

import os
import threading
import time

import subprocess

import tests.conftest as conftest_mod
from tests.conftest import (
    _check_null_sandbox,
    _detect_live_server,
    _live_agent_dirs,
)
from tests.conftest import _snapshot_null_dir as _snapshot_raw


def _snapshot_null_dir(root):
    """Exact (content-hashed) snapshot for the guard's own unit tests.

    These tests rewrite a file with SAME-SIZE content and snapshot it
    microseconds later — e.g. a git ref, which is always 41 bytes. The
    default (size, mtime_ns) fingerprint cannot see that when both writes
    land in one filesystem timestamp tick, and on Windows that tick is
    ~15.6ms, so they shared an mtime and the change vanished. That made
    these tests flaky on windows-latest while staying green on POSIX.

    Hashing is affordable here (the trees are a handful of files) and NOT
    on the autouse real-store path (~7s per snapshot) — see
    conftest._snapshot_null_dir.
    """
    return _snapshot_raw(root, hash_content=True)


def _make_live_store(root):
    """Build a ~/.null look-alike with a live session that predates the
    'test': an agent dir holding an active_session.json, its git metadata,
    and the top-level shared store files."""
    agent = root / "atlas"
    (agent / ".git").mkdir(parents=True)
    (agent / "active_session.json").write_text('{"session_id": "s1"}')
    (agent / "memory.db").write_text("db-v1")
    (agent / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (agent / ".git" / "index").write_text("idx-v1")
    (root / "unified.db").write_text("unified-v1")
    (root / "outreaches.log").write_text("line1\n")
    return agent


class TestLeakStillCaught:
    def test_leak_fails_with_no_live_session(self, tmp_path):
        root = tmp_path / "fake_null"
        root.mkdir()
        (root / "identity.json").write_text("{}")
        before = _snapshot_null_dir(root)
        (root / "outreaches.log").write_text("leaked\n")  # any write fails
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(before, after)
        assert failure is not None
        assert "outreaches.log" in failure
        assert external == []

    def test_leak_fails_even_during_live_session(self, tmp_path):
        """A genuine leak outside the live session's write surface must
        still fail, even while external writes are happening."""
        root = tmp_path / "fake_null"
        agent = _make_live_store(root)
        before = _snapshot_null_dir(root)
        # External live writer touches its own surface...
        (agent / "memory.db").write_text("db-v2-longer")
        # ...while the test leaks somewhere else entirely.
        (root / "agents").mkdir()
        (root / "agents" / "TestAgent.json").write_text("{}")
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(before, after)
        assert failure is not None
        leak_rel = str((root / "agents" / "TestAgent.json").relative_to(root))
        assert leak_rel in failure
        # The concurrent external write is reported as tolerated, not leaked.
        assert any("memory.db" in p for p in external)

    def test_session_marker_added_during_test_cannot_whitelist_itself(self, tmp_path):
        """A test that leaks a whole session (active_session.json + db)
        into the real store must not whitelist its own writes — only
        markers from the BEFORE snapshot count."""
        root = tmp_path / "fake_null"
        root.mkdir()
        before = _snapshot_null_dir(root)
        leak_dir = root / "TestAgent"
        leak_dir.mkdir()
        (leak_dir / "active_session.json").write_text('{"session_id": "x"}')
        (leak_dir / "memory.db").write_text("db")
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(before, after)
        assert failure is not None
        assert "active_session.json" in failure
        assert external == []

    def test_root_level_marker_does_not_whitelist_tree(self, tmp_path):
        """An active_session.json at the ROOT of ~/.null must not turn the
        entire tree into tolerated surface."""
        root = tmp_path / "fake_null"
        root.mkdir()
        (root / "active_session.json").write_text('{"session_id": "r"}')
        before = _snapshot_null_dir(root)
        assert _live_agent_dirs(before) == set()
        (root / "somefile.json").write_text("{}")
        after = _snapshot_null_dir(root)
        failure, _ = _check_null_sandbox(before, after)
        assert failure is not None


class TestExternalWriterTolerated:
    def test_live_session_writes_warn_not_fail(self, tmp_path):
        """Simulate the live Atlas writer mutating its store from another
        thread mid-test: db commit, git activity, shared store files. All
        changes sit on the pre-existing session's surface -> no failure."""
        root = tmp_path / "fake_null"
        agent = _make_live_store(root)
        before = _snapshot_null_dir(root)

        def live_writer():
            # memory commit + git sync + shared stores + dated outreach log
            (agent / "memory.db").write_text("db-v2-after-remember")
            (agent / ".git" / "index").write_text("idx-v2-changed")
            (agent / ".git" / "HEAD").write_text("ref: refs/heads/main \n")
            (agent / "sessions").mkdir(exist_ok=True)
            (agent / "sessions" / "s1.json").write_text("{}")
            (root / "unified.db").write_text("unified-v2-bigger")
            (root / "outreaches.log").write_text("line1\nline2\n")
            (root / "outreaches-2026-06-10.log").write_text("entry\n")

        t = threading.Thread(target=live_writer)
        t.start()
        t.join()
        # mtime_ns granularity: ensure the writes are visible to stat.
        time.sleep(0.01)

        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(before, after)
        assert failure is None
        assert external  # changes were seen and classified, not ignored
        assert any("memory.db" in p for p in external)
        assert any("unified.db" in p for p in external)

    def test_session_end_pointer_removal_tolerated(self, tmp_path):
        """The live session ending (active pointer removed, session record
        written) is external activity, not a leak."""
        root = tmp_path / "fake_null"
        agent = _make_live_store(root)
        before = _snapshot_null_dir(root)
        (agent / "active_session.json").unlink()
        (agent / "memory.db").write_text("db-final-state")
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(before, after)
        assert failure is None
        assert len(external) == 2

    def test_clean_run_no_diff_no_warning(self, tmp_path):
        root = tmp_path / "fake_null"
        _make_live_store(root)
        before = _snapshot_null_dir(root)
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(before, after)
        assert failure is None
        assert external == []


def _make_idle_live_store(root):
    """The REAL shape of Pete's machine (issue #5): a live null server
    process is running, but the store holds NO active_session.json — only
    agent-store files. The atlas agent dir, a personality dir, and the
    shared root files."""
    agent = root / "atlas"
    agent.mkdir(parents=True)
    (agent / "memory.db").write_text("db-v1")
    (agent / "state.json").write_text("{}")
    pers = root / "personalities" / "hermes"
    pers.mkdir(parents=True)
    (pers / "config.json").write_text("{}")
    (root / "unified.db").write_text("unified-v1")
    (root / "daemon.log").write_text("up\n")
    return agent, pers


class TestLiveServerProcessEvidence:
    """Pid-correlation evidence (server_running=True): the live MCP/daemon
    keeps no active_session.json while idle, yet writes its agent dir and
    the shared store throughout a suite run."""

    def test_server_writes_tolerated_without_session_marker(self, tmp_path):
        root = tmp_path / "fake_null"
        agent, pers = _make_idle_live_store(root)
        before = _snapshot_null_dir(root)
        assert _live_agent_dirs(before) == set()  # no marker evidence
        (agent / "memory.db").write_text("db-v2-after-remember")
        (agent / "state.json").write_text('{"written": "now"}')
        (pers / "config.json").write_text('{"tick": 1}')
        (root / "unified.db").write_text("unified-v2-bigger")
        (root / "daemon.log").write_text("up\nheartbeat\n")
        after = _snapshot_null_dir(root)
        # Without pid evidence: strict — every change is a leak.
        failure, _ = _check_null_sandbox(before, after, server_running=False)
        assert failure is not None
        # With pid evidence: external writer, warn not fail.
        failure, external = _check_null_sandbox(
            before, after, server_running=True)
        assert failure is None
        assert len(external) == 5

    def test_leak_still_fails_while_server_running(self, tmp_path):
        root = tmp_path / "fake_null"
        agent, _ = _make_idle_live_store(root)
        before = _snapshot_null_dir(root)
        (agent / "memory.db").write_text("db-v2")        # live writer...
        (root / "leaked.json").write_text("{}")           # ...and a leak
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(
            before, after, server_running=True)
        assert failure is not None
        assert "leaked.json" in failure
        assert any("memory.db" in p for p in external)

    def test_new_agent_dir_during_test_fails_even_with_server(self, tmp_path):
        """Agent-dir markers only count from the BEFORE snapshot: a test
        leaking a whole new agent store cannot whitelist itself, even
        while a server process is running."""
        root = tmp_path / "fake_null"
        _make_idle_live_store(root)
        before = _snapshot_null_dir(root)
        leak = root / "TestAgent"
        leak.mkdir()
        (leak / "memory.db").write_text("db")
        (leak / "identity.json").write_text("{}")
        after = _snapshot_null_dir(root)
        failure, _ = _check_null_sandbox(before, after, server_running=True)
        assert failure is not None
        assert "TestAgent" in failure

    def test_root_level_store_marker_does_not_whitelist_tree(self, tmp_path):
        """state.json etc. at the ROOT of ~/.null must not turn the whole
        tree into tolerated surface even with a server running."""
        root = tmp_path / "fake_null"
        root.mkdir()
        (root / "state.json").write_text("{}")
        before = _snapshot_null_dir(root)
        assert _live_agent_dirs(before, server_running=True) == set()
        (root / "somefile.json").write_text("{}")
        after = _snapshot_null_dir(root)
        failure, _ = _check_null_sandbox(before, after, server_running=True)
        assert failure is not None


class TestRootGitSurface:
    """The live writer syncs the whole store via a git repo at the ROOT of
    ~/.null — its commits mutate .git/refs and .git/logs mid-suite
    (observed live during run 2 of issue #5 verification)."""

    def _add_root_git(self, root):
        gitdir = root / ".git"
        (gitdir / "refs" / "heads").mkdir(parents=True)
        (gitdir / "logs").mkdir()
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
        (gitdir / "refs" / "heads" / "main").write_text("aaaa\n")
        (gitdir / "logs" / "HEAD").write_text("log-v1\n")
        return gitdir

    def test_preexisting_root_git_sync_tolerated(self, tmp_path):
        root = tmp_path / "fake_null"
        _make_idle_live_store(root)
        gitdir = self._add_root_git(root)
        before = _snapshot_null_dir(root)
        # live sync commit: ref moves, reflog grows
        (gitdir / "refs" / "heads" / "main").write_text("bbbb\n")
        (gitdir / "logs" / "HEAD").write_text("log-v1\nlog-v2\n")
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(
            before, after, server_running=True)
        assert failure is None
        assert len(external) == 2

    def test_root_git_changes_fail_without_live_evidence(self, tmp_path):
        root = tmp_path / "fake_null"
        root.mkdir()
        gitdir = self._add_root_git(root)
        before = _snapshot_null_dir(root)
        (gitdir / "refs" / "heads" / "main").write_text("bbbb\n")
        after = _snapshot_null_dir(root)
        failure, _ = _check_null_sandbox(before, after, server_running=False)
        assert failure is not None

    def test_git_init_leak_fails_even_with_server(self, tmp_path):
        """A test leaking `git init` into the real store creates a NEW
        root .git — no pre-existence, so it must fail."""
        root = tmp_path / "fake_null"
        _make_idle_live_store(root)
        before = _snapshot_null_dir(root)
        self._add_root_git(root)
        after = _snapshot_null_dir(root)
        failure, _ = _check_null_sandbox(before, after, server_running=True)
        assert failure is not None
        assert ".git" in failure


class TestExchangeCloneSurface:
    """The daemon's poke loop (sync Phase B) fetches the org-exchange
    clone at ~/.null/exchange/ every few minutes — its FETCH_HEAD and
    stream files mutate mid-suite, exactly like the root .git surface."""

    def _add_exchange_clone(self, root):
        ex = root / "exchange"
        (ex / ".git").mkdir(parents=True)
        (ex / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (ex / ".git" / "FETCH_HEAD").write_text("aaaa branch 'main'\n")
        (ex / "streams").mkdir()
        (ex / "streams" / "atlas.jsonl").write_text('{"k":1}\n')
        return ex

    def test_preexisting_exchange_fetch_tolerated(self, tmp_path):
        root = tmp_path / "fake_null"
        _make_idle_live_store(root)
        ex = self._add_exchange_clone(root)
        before = _snapshot_null_dir(root)
        # live poke cycle: fetch updates FETCH_HEAD, ingestion pulls a
        # new stream line.
        (ex / ".git" / "FETCH_HEAD").write_text("bbbb branch 'main'\n")
        (ex / "streams" / "atlas.jsonl").write_text('{"k":1}\n{"k":2}\n')
        after = _snapshot_null_dir(root)
        failure, external = _check_null_sandbox(
            before, after, server_running=True)
        assert failure is None
        assert len(external) == 2

    def test_exchange_changes_fail_without_live_evidence(self, tmp_path):
        root = tmp_path / "fake_null"
        root.mkdir()
        ex = self._add_exchange_clone(root)
        before = _snapshot_null_dir(root)
        (ex / ".git" / "FETCH_HEAD").write_text("bbbb branch 'main'\n")
        after = _snapshot_null_dir(root)
        failure, _ = _check_null_sandbox(before, after, server_running=False)
        assert failure is not None

    def test_exchange_clone_leak_fails_even_with_server(self, tmp_path):
        """A test leaking an exchange clone into the real store creates a
        NEW exchange/.git — no pre-existence, so it must fail."""
        root = tmp_path / "fake_null"
        _make_idle_live_store(root)
        before = _snapshot_null_dir(root)
        self._add_exchange_clone(root)
        after = _snapshot_null_dir(root)
        failure, _ = _check_null_sandbox(before, after, server_running=True)
        assert failure is not None
        assert "exchange" in failure


class TestLiveServerDetection:
    """_detect_live_server is pid-correlation via pgrep (POSIX) or CIM
    command-line listing (Windows, issue #42 — no pgrep there meant the
    guard ran permanently strict and blamed the always-on seat daemon's
    writes on innocent tests). Simulate the outcomes by stubbing
    subprocess.run — never spawn the real tools here."""

    # A command line that satisfies BOTH platform branches: POSIX pgrep
    # treats returncode 0 + non-empty stdout as a match; the Windows
    # branch regex-matches the command line itself.
    _SERVER_CMDLINE = "C:\\py\\python.exe -m null_memory.cli serve C:/store"

    def _stub(self, monkeypatch, *, returncode=0, stdout="", exc=None):
        def fake_run(*args, **kwargs):
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(
                args=args, returncode=returncode, stdout=stdout, stderr="")
        monkeypatch.setattr(conftest_mod.subprocess, "run", fake_run)

    def test_running_server_detected(self, monkeypatch):
        self._stub(monkeypatch, returncode=0, stdout=self._SERVER_CMDLINE)
        assert _detect_live_server() is True

    def test_no_match_means_strict(self, monkeypatch):
        self._stub(monkeypatch, returncode=1, stdout="")
        assert _detect_live_server() is False

    def test_unrelated_python_processes_stay_strict_on_windows(
            self, monkeypatch):
        """Windows branch only: CIM returns ALL python command lines; a
        machine running unrelated python must not unlock the guard."""
        if os.name != "nt":
            import pytest
            pytest.skip("Windows CIM branch")
        self._stub(monkeypatch, returncode=0,
                   stdout="C:\\py\\python.exe -m http.server 8000\n")
        assert _detect_live_server() is False

    def test_pgrep_unavailable_means_strict(self, monkeypatch):
        self._stub(monkeypatch, exc=FileNotFoundError("pgrep"))
        assert _detect_live_server() is False
