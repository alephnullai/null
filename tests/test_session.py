"""Tests for session lifecycle and git-backed memory storage."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch, MagicMock

import pytest

from null_memory.session import (
    Session, SessionManager, MemoryRepo, capture_git_state, _run_git,
)
from null_memory.agent import AgentMemory
from null_memory.mcp.handlers import NullHandlers


# ── Session dataclass ──


class TestSession:
    def test_auto_generates_id(self):
        s = Session()
        assert s.session_id
        assert len(s.session_id) == 36  # UUID4

    def test_auto_generates_timestamps(self):
        s = Session()
        assert s.started_at
        assert s.last_tool_call == s.started_at

    def test_to_dict_roundtrip(self):
        s = Session(project="aleph", status="active")
        d = s.to_dict()
        s2 = Session.from_dict(d)
        assert s2.session_id == s.session_id
        assert s2.project == "aleph"
        assert s2.status == "active"

    def test_from_dict_ignores_unknown_fields(self):
        d = {"session_id": "test", "unknown_field": "ignored"}
        s = Session.from_dict(d)
        assert s.session_id == "test"

    def test_touch_updates_timestamp(self):
        s = Session()
        old = s.last_tool_call
        import time; time.sleep(0.01)
        s.touch()
        assert s.last_tool_call >= old

    def test_add_checkpoint(self):
        s = Session()
        assert s.checkpoints == []
        s.add_checkpoint()
        assert len(s.checkpoints) == 1
        s.add_checkpoint()
        assert len(s.checkpoints) == 2


# ── MemoryRepo ──


class TestMemoryRepo:
    def test_init_creates_repo(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        assert not repo.is_repo()
        result = repo.init()
        assert result is True
        assert repo.is_repo()

    def test_init_idempotent(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        result = repo.init()
        assert result is False  # Already initialized

    def test_init_creates_gitignore(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert ".lock" in content
        assert "active_session.json" in content

    def test_commit(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        # Create a file
        (tmp_path / "test.txt").write_text("hello")
        result = repo.commit("test commit")
        assert result is True

    def test_commit_nothing_to_commit(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        result = repo.commit("empty")
        assert result is False

    def test_has_uncommitted_changes(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        assert not repo.has_uncommitted_changes()
        (tmp_path / "new_file.txt").write_text("data")
        assert repo.has_uncommitted_changes()

    def test_last_commit_time(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        t = repo.last_commit_time()
        assert t is not None

    def test_last_commit_message(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        msg = repo.last_commit_message()
        assert msg is not None
        assert "initialize" in msg

    def test_log(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        repo.init()
        (tmp_path / "a.txt").write_text("a")
        repo.commit("first")
        (tmp_path / "b.txt").write_text("b")
        repo.commit("second")
        entries = repo.log(limit=5)
        assert len(entries) >= 2
        assert entries[0]["message"] == "second"

    def test_not_a_repo(self, tmp_path):
        repo = MemoryRepo(str(tmp_path))
        assert not repo.is_repo()
        assert not repo.has_uncommitted_changes()
        assert repo.last_commit_time() is None
        assert repo.log() == []


# ── SessionManager ──


class TestSessionManager:
    def test_start_and_end_session(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        session = mgr.start_session(project="test")
        assert session.status == "active"
        assert session.project == "test"
        assert os.path.isfile(mgr._active_path)

        mgr.end_session(session, summary="test session")
        assert session.status == "completed"
        assert session.ended_at is not None
        assert not os.path.isfile(mgr._active_path)

    def test_crash_detection(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        session = mgr.start_session(project="test")

        # Real crash ≠ MCP restart. Age the session's last activity past the
        # 5-minute restart window so detect_crash treats it as a crash.
        from datetime import datetime, timedelta, timezone
        session.started_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        session.last_tool_call = session.started_at
        mgr.save_session(session)

        crashed = mgr.detect_crash()
        assert crashed is not None
        assert crashed.session_id == session.session_id

    def test_no_crash_after_clean_close(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        session = mgr.start_session(project="test")
        mgr.end_session(session)
        assert mgr.detect_crash() is None

    def test_mark_crashed(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        session = mgr.start_session(project="test")
        mgr.mark_crashed(session)
        assert session.status == "crashed"
        assert not os.path.isfile(mgr._active_path)

    def test_list_sessions(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        s1 = mgr.start_session(project="a")
        mgr.end_session(s1)
        s2 = mgr.start_session(project="b")
        mgr.end_session(s2)
        sessions = mgr.list_sessions()
        assert len(sessions) == 2
        # Newest first
        assert sessions[0].session_id == s2.session_id

    def test_last_completed_session(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        s1 = mgr.start_session(project="a")
        mgr.end_session(s1, summary="first")
        s2 = mgr.start_session(project="b")
        # s2 is still active
        last = mgr.last_completed_session()
        assert last is not None
        assert last.session_id == s1.session_id

    def test_detect_gaps(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        gaps = mgr.detect_gaps()
        assert "last_commit_age_hours" in gaps
        assert "prior_crash" in gaps

    def test_git_commit_on_end(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        session = mgr.start_session(project="test")
        # Write some data
        (tmp_path / "test_data.txt").write_text("data")
        committed = mgr.end_session(session, summary="test")
        assert committed is True
        # Verify commit exists
        assert not mgr.repo.has_uncommitted_changes()

    def test_checkpoint_commit(self, tmp_path):
        mgr = SessionManager(str(tmp_path))
        session = mgr.start_session(project="test")
        (tmp_path / "data.txt").write_text("checkpoint data")
        committed = mgr.checkpoint_commit(session, note="mid-session")
        assert committed is True
        assert len(session.checkpoints) == 1


# ── Agent integration ──


class TestAgentSessionIntegration:
    def test_agent_loads_session_manager(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        assert mem._session_manager is not None

    def test_agent_start_session(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        session = mem.start_session(project="test")
        assert session.status == "active"
        assert mem._current_session is session

    def test_learn_links_session(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        session = mem.start_session(project="test")
        entry = mem.learn("test fact", 0.9, project="test")
        assert entry.get("session_id") == session.session_id
        assert session.facts_created == 1

    def test_decide_links_session(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        session = mem.start_session(project="test")
        entry = mem.decide("test decision", "test reasoning")
        assert entry.get("session_id") == session.session_id
        assert session.decisions_created == 1

    def test_mistake_links_session(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        session = mem.start_session(project="test")
        entry = mem.mistake("oops", "bad idea")
        assert entry.get("session_id") == session.session_id
        assert session.mistakes_created == 1

    def test_close_commits_to_git(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        mem.learn("important fact", 0.9)
        result = mem.close(summary="test session", project="test")
        assert result["committed"] is True
        assert mem._current_session is None

    def test_close_with_debrief_and_reflect(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        result = mem.close(
            summary="did good work",
            went_well="shipped fast",
            missed="no tests",
            do_differently="write tests first",
            decisions_made=["use git for memory storage"],
            lessons=["git gives timestamps for free"],
            project="test",
        )
        assert result["reflected"] is True
        assert result["debrief"]["facts"] >= 1
        assert result["debrief"]["decisions"] >= 1

    def test_crash_detected_on_reload(self, tmp_path):
        # Start a session, don't close it
        mem1 = AgentMemory.load(str(tmp_path))
        session = mem1.start_session(project="test")
        mem1.learn("something", 0.9)

        # Age the session past the 5-minute MCP-restart window so the
        # reload is classified as a crash, not a silent restart.
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        session.started_at = stale
        session.last_tool_call = stale
        mem1._session_manager.save_session(session)

        # Reload (simulates new process)
        mem2 = AgentMemory.load(str(tmp_path))
        assert mem2._prior_crash is not None
        assert mem2._prior_crash.status == "active"

    def test_briefing_shows_crash_warning(self, tmp_path):
        # Start a session, don't close it
        mem1 = AgentMemory.load(str(tmp_path))
        session = mem1.start_session(project="test")

        # Age past the 5-minute restart window so this is classified as a
        # real crash rather than an MCP reload.
        from datetime import datetime, timedelta, timezone
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        session.started_at = stale
        session.last_tool_call = stale
        mem1._session_manager.save_session(session)

        # Reload and check briefing
        mem2 = AgentMemory.load(str(tmp_path))
        briefing = mem2.briefing()
        assert "CRASHED" in briefing or "crashed" in briefing.lower()


# ── Handler integration ──


class TestHandlerSessionIntegration:
    def test_handler_auto_starts_session(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        assert h._session_started is True
        assert h.memory._current_session is not None

    def test_handler_close(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        h.memory.learn("test fact", 0.9)
        result = h.handle_close(summary="test", project="test")
        assert "closed" in result.lower() or "signing off" in result.lower() or "committed" in result.lower()
        assert h._session_started is False

    def test_handler_checkpoint_commits(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        h.memory.learn("checkpoint test", 0.9)
        result = h.handle_checkpoint()
        assert "Checkpoint" in result


# ── capture_git_state ──


class TestCaptureGitState:
    def test_returns_none_for_non_repo(self, tmp_path):
        head, branch = capture_git_state(cwd=str(tmp_path))
        assert head is None
        assert branch is None

    def test_returns_none_for_none_cwd(self):
        head, branch = capture_git_state(cwd=None)
        assert head is None
        assert branch is None


class TestRunGitHeadlessHardening:
    """Issue #4: git must never go interactive in the headless server, and
    the timeout must hold even when a credential-helper grandchild inherits
    our pipes and outlives git itself (the Windows GCM hang)."""

    @staticmethod
    def _fake_git(tmp_path, script_body: str):
        """Install a fake `git` on PATH; returns the env-patcher value."""
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        git = bin_dir / "git"
        git.write_text("#!/bin/sh\n" + script_body)
        git.chmod(0o755)
        return str(bin_dir)

    @pytest.fixture(autouse=True)
    def _posix_only(self):
        if os.name == "nt":
            pytest.skip(
                "sh-based fake git; the Windows kill path (taskkill /T) "
                "is exercised in production, not simulatable here"
            )

    def test_git_env_is_noninteractive(self, tmp_path, monkeypatch):
        bin_dir = self._fake_git(
            tmp_path,
            'echo "prompt=$GIT_TERMINAL_PROMPT gcm=$GCM_INTERACTIVE"\n',
        )
        monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ["PATH"])
        result = _run_git(["status"], cwd=str(tmp_path))
        assert result.returncode == 0
        assert "prompt=0" in result.stdout
        assert "gcm=never" in result.stdout

    def test_stdin_is_closed(self, tmp_path, monkeypatch):
        # `cat` with an open stdin would block forever; DEVNULL = instant EOF.
        bin_dir = self._fake_git(tmp_path, "cat\necho done\n")
        monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ["PATH"])
        result = _run_git(["pull"], cwd=str(tmp_path), timeout=10)
        assert result.returncode == 0
        assert "done" in result.stdout

    def test_grandchild_holding_pipe_cannot_defeat_timeout(
        self, tmp_path, monkeypatch
    ):
        """The exact GCM failure shape: git exits, but a spawned grandchild
        keeps stdout open. subprocess.run() would block unbounded in its
        post-kill communicate(); _run_git must return promptly."""
        bin_dir = self._fake_git(tmp_path, "sleep 60 &\nexit 0\n")
        monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ["PATH"])
        start = time.monotonic()
        result = _run_git(["push"], cwd=str(tmp_path), timeout=2)
        elapsed = time.monotonic() - start
        assert elapsed < 15, f"hung {elapsed:.1f}s — pipe holder defeated timeout"
        assert result.returncode == 128
        assert "timed out" in result.stderr

    def test_hung_git_killed_at_timeout(self, tmp_path, monkeypatch):
        bin_dir = self._fake_git(tmp_path, "sleep 60\n")
        monkeypatch.setenv("PATH", bin_dir + os.pathsep + os.environ["PATH"])
        start = time.monotonic()
        result = _run_git(["fetch"], cwd=str(tmp_path), timeout=2)
        elapsed = time.monotonic() - start
        assert elapsed < 15
        assert result.returncode == 128
        assert "timed out" in result.stderr

    def test_normal_git_unaffected(self, tmp_path):
        # Real git, real repo — the hardening must not break the happy path.
        repo = tmp_path / "repo"
        repo.mkdir()
        assert _run_git(["init"], cwd=str(repo)).returncode == 0
        result = _run_git(["status", "--porcelain"], cwd=str(repo))
        assert result.returncode == 0


class TestWalCheckpointOnCommit:
    """Issue #28: commits must fold the WAL into the .db first, or a store
    whose .gitignore (correctly) excludes -wal/-shm pushes a db missing
    recent writes — silent data loss on clone (observed: athena's first 6
    facts existed only in the WAL while origin held a 0-fact store)."""

    def test_commit_includes_wal_contents(self, tmp_path):
        import sqlite3
        repo_dir = tmp_path / "seat"
        repo_dir.mkdir()
        (repo_dir / ".gitignore").write_text("*.db-wal\n*.db-shm\n")

        db = repo_dir / "memory.db"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE facts (id INTEGER, fact TEXT)")
        conn.commit()

        repo = MemoryRepo(str(repo_dir))
        repo.init()
        assert repo.commit("baseline")

        # Write facts that stay in the WAL (no checkpoint by us).
        conn.execute("INSERT INTO facts VALUES (1, 'athena fact one')")
        conn.execute("INSERT INTO facts VALUES (2, 'athena fact two')")
        conn.commit()
        assert (repo_dir / "memory.db-wal").stat().st_size > 0

        assert repo.commit("observe"), "WAL fold must produce a db diff"

        # A fresh clone must see the facts — the literal failure mode.
        import subprocess as _sp
        clone = tmp_path / "clone"
        _sp.run(["git", "clone", "-q", str(repo_dir), str(clone)],
                check=True, capture_output=True)
        check = sqlite3.connect(clone / "memory.db")
        try:
            n = check.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        finally:
            check.close()
        conn.close()
        assert n == 2, f"clone sees {n}/2 facts — WAL not checkpointed"

    def test_checkpoint_skips_nested_seat_repos(self, tmp_path):
        import sqlite3
        hub = tmp_path / "hub"
        seat = hub / "personalities" / "worker1"
        seat.mkdir(parents=True)
        (seat / ".git").mkdir()  # nested repo marker
        seat_db = seat / "memory.db"
        c = sqlite3.connect(seat_db)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE t (x)")
        c.execute("INSERT INTO t VALUES (1)")
        c.commit()
        wal_size = (seat / "memory.db-wal").stat().st_size
        assert wal_size > 0

        repo = MemoryRepo(str(hub))
        repo._checkpoint_wals()
        # Seat's WAL untouched — its own sync owns it.
        assert (seat / "memory.db-wal").stat().st_size == wal_size
        c.close()
