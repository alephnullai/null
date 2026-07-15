"""The foreground session-start path must do ZERO blocking git work.

Today's hang was traced through:

    handle_identity -> _ensure_session -> start_session -> ensure_repo
        -> init -> _run_git

Even a *fast* git is latency on the MCP hot path; a slow disk / large repo /
push must never block a tool response. These tests prove that:

  1. The foreground (AgentMemory.start_session) returns promptly even when
     every git call sleeps — i.e. no _run_git runs synchronously on the
     calling thread before start_session returns.
  2. Git still happens — just on a *different* thread, a moment later.
  3. End-to-end correctness holds: after a session start + a write + a sync
     flush, the repo exists and a commit was made.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import null_memory.session as session_mod
from null_memory.agent import AgentMemory
from null_memory.session import MemoryRepo, Session, SessionManager


def _patch_run_git_record(monkeypatch, record):
    """Wrap session._run_git so every call records (thread_ident, args)."""
    real = session_mod._run_git

    def spy(args, cwd, timeout=10):
        record.append((threading.get_ident(), tuple(args)))
        return real(args, cwd, timeout=timeout)

    monkeypatch.setattr(session_mod, "_run_git", spy)
    return real


class TestForegroundIsGitFree:
    def test_start_session_runs_no_git_on_calling_thread(self, tmp_path, monkeypatch):
        """Not a single _run_git call may execute on the foreground thread
        during start_session. (They are allowed on the background daemon.)"""
        record: list[tuple[int, tuple]] = []
        _patch_run_git_record(monkeypatch, record)

        mem = AgentMemory.load(str(tmp_path))
        # Clear anything load-time crash detection may have recorded; we only
        # care about what start_session does.
        record.clear()
        main_ident = threading.get_ident()

        session = mem.start_session(project="test")
        assert session is not None

        # Inspect immediately: nothing run on the foreground thread.
        foreground_git = [a for (ident, a) in record if ident == main_ident]
        assert foreground_git == [], (
            f"start_session ran git on the request thread: {foreground_git}"
        )

    def test_start_session_returns_fast_even_when_git_is_slow(self, tmp_path, monkeypatch):
        """With every git call sleeping 2s, the foreground must still return
        in a small fraction of that — proving git is off the hot path."""
        def slow_git(args, cwd, timeout=10):
            time.sleep(2.0)
            import subprocess
            return subprocess.CompletedProcess(args=["git"] + list(args),
                                               returncode=0, stdout="", stderr="")

        monkeypatch.setattr(session_mod, "_run_git", slow_git)

        mem = AgentMemory.load(str(tmp_path))

        start = time.monotonic()
        mem.start_session(project="test")
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, (
            f"start_session blocked {elapsed:.2f}s on slow git — git is still "
            "on the foreground request path"
        )

    def test_git_eventually_runs_on_a_background_thread(self, tmp_path, monkeypatch):
        """Git is deferred, not dropped: it runs, on a non-foreground thread."""
        record: list[tuple[int, tuple]] = []
        _patch_run_git_record(monkeypatch, record)

        mem = AgentMemory.load(str(tmp_path))
        record.clear()
        main_ident = threading.get_ident()

        mem.start_session(project="test")

        # Wait for the deferred git thread to do its work.
        t = mem._session_git_thread
        assert t is not None
        t.join(timeout=15)
        assert not t.is_alive()

        background_git = [a for (ident, a) in record if ident != main_ident]
        assert background_git, "deferred git never ran on a background thread"
        # The repo init (`git init`) is among the deferred calls.
        assert any(a and a[0] == "init" for (_ident, a) in record)


class TestEndToEndCorrectness:
    def test_repo_initialized_after_background_thread(self, tmp_path):
        """After start_session, the deferred thread brings the repo into being."""
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")

        # Repo need not exist the instant start_session returns...
        t = mem._session_git_thread
        assert t is not None
        t.join(timeout=15)

        # ...but it must exist once the deferred git thread finishes.
        assert MemoryRepo(str(tmp_path)).is_repo()

    def test_write_then_flush_commits(self, tmp_path):
        """End-to-end: session start + a write + a sync flush => a commit
        exists. The repo is lazily created on the (backgrounded) commit path
        even if the deferred session-start thread never beat us to it."""
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        mem.learn("Async git: session start writes the session record with no "
                  "git, deferring repo init to the background commit path", 0.9)

        # Drive the already-backgrounded sync flush synchronously.
        mem._sync_to_remote("observe")
        mem._flush_sync_now()

        repo = MemoryRepo(str(tmp_path))
        assert repo.is_repo(), "repo was never created by the commit path"
        log = repo.log(limit=20)
        assert log, "no commits were made"

    def test_close_commits_even_if_deferred_thread_lost_the_race(self, tmp_path):
        """A close() right after start_session must still commit — the commit
        path lazily inits the repo, so it does not depend on the deferred
        session-start git thread having finished first."""
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        mem.learn("important fact for close", 0.9)
        result = mem.close(summary="async git test", project="test")
        assert result["committed"] is True
        assert mem._current_session is None


class TestCrashPointerSurvivesDeferredWork:
    def test_new_session_pointer_survives_prior_crash_cleanup(self, tmp_path):
        """Regression: session A crashes; session B starts (pointer written);
        the deferred background work completes — B's active pointer must
        still exist. The old bug ran mark_crashed's pointer unlink on the
        background thread AFTER B's pointer was written, destroying live
        crash detection for every session that followed a crash."""
        # Session A: an "active" session last seen 30 minutes ago — squarely
        # inside the 5min–6h real-crash window.
        sm = SessionManager(str(tmp_path))
        past = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        session_a = Session(project="test", started_at=past, last_tool_call=past)
        sm.save_session(session_a)
        sm._atomic_write(sm._active_path, {
            "session_id": session_a.session_id,
            "started_at": session_a.started_at,
        })

        # Load detects the crash; start_session does the file half in the
        # foreground and defers only the git commit.
        mem = AgentMemory.load(str(tmp_path))
        assert mem._prior_crash is not None, "fixture must look like a crash"

        session_b = mem.start_session(project="test")

        # Let ALL deferred background work complete.
        t = mem._session_git_thread
        assert t is not None
        t.join(timeout=15)
        assert not t.is_alive()

        # B's active pointer survived the crash cleanup.
        active_path = os.path.join(str(tmp_path), "active_session.json")
        assert os.path.isfile(active_path), (
            "background crash cleanup destroyed the new session's pointer"
        )
        with open(active_path, encoding="utf-8") as f:
            pointer = json.load(f)
        assert pointer["session_id"] == session_b.session_id

        # And session A really was marked crashed.
        a_on_disk = sm.load_session(session_a.session_id)
        assert a_on_disk is not None
        assert a_on_disk.status == "crashed"


class TestFinishSessionGitTOCTOU:
    def test_concurrent_checkpoint_not_clobbered(self, tmp_path, monkeypatch):
        """finish_session_git must hold the write lock across its whole
        read-modify-write: a foreground save landing in a read/save gap used
        to get clobbered with stale fact counts (TOCTOU)."""
        sm = SessionManager(str(tmp_path))
        session = sm.start_session(project="test", defer_git=True)

        monkeypatch.setattr(
            session_mod, "capture_git_state",
            lambda cwd=None: ("abc123", "main"),
        )

        in_rmw = threading.Event()
        release = threading.Event()
        real_load = sm.load_session

        def slow_load(session_id):
            result = real_load(session_id)
            in_rmw.set()          # background is mid read-modify-write...
            release.wait(5)       # ...hold it there while foreground writes
            return result

        monkeypatch.setattr(sm, "load_session", slow_load)

        bg = threading.Thread(
            target=sm.finish_session_git, args=(session,),
            kwargs={"git_cwd": str(tmp_path)},
        )
        bg.start()
        assert in_rmw.wait(5), "finish_session_git never reached its RMW"

        # Foreground checkpoint-style save while the background sits inside
        # its read-modify-write. With the lock held across the RMW this
        # blocks until the background save lands, then writes the freshest
        # state (the live object the background also mutates).
        def foreground_save():
            session.facts_created = 7
            sm.save_session(session)

        fg = threading.Thread(target=foreground_save)
        fg.start()
        time.sleep(0.2)  # give the foreground its chance to slip into a gap
        release.set()
        bg.join(timeout=10)
        fg.join(timeout=10)
        assert not bg.is_alive() and not fg.is_alive()

        on_disk = real_load(session.session_id)
        assert on_disk is not None
        assert on_disk.facts_created == 7, (
            "foreground checkpoint was clobbered with stale fact counts"
        )
        assert on_disk.git_head == "abc123"


class TestRepoInitIdempotent:
    def test_concurrent_init_creates_one_repo(self, tmp_path):
        """ensure_repo/init must be safe to call from multiple threads (the
        deferred session-start thread + the commit path can race)."""
        repo = MemoryRepo(str(tmp_path))
        results: list[bool] = []
        threads = [
            threading.Thread(target=lambda: results.append(repo.init()))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert repo.is_repo()
        # Exactly one thread should report it performed the init.
        assert results.count(True) == 1, (
            f"init was not race-safe; True returns = {results.count(True)}"
        )
