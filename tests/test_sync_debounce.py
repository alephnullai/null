"""P0-5 (N8) — debounced remote sync.

Per-write callers must only mark a dirty flag; the actual git commit+push
happens once per debounce window or at lifecycle boundaries
(checkpoint / debrief / close), serialized by a process-wide lock.

Flake hardening (issue #5): FakeRepo used to keep class-level lists, so a
late background sync thread from an EARLIER test could flush commits into
a later test's assertions under full-suite load (observed: assert 13 == 1).
The recorder is now a per-test instance, scoped to this test's sandbox dir,
and the ``mem`` fixture joins all sync threads at teardown.
"""

from __future__ import annotations

import threading

import pytest

import null_memory.session as session_mod


class FakeRepo:
    """Per-test recorder standing in for the MemoryRepo *class*.

    One instance per test (instance attributes — never class state). The
    instance is callable so it can be monkeypatched in where the class is
    expected: ``MemoryRepo(repo_dir)`` returns a handle that records into
    this instance. Only writes whose repo_dir lives inside this test's
    sandbox are recorded, so a stray flush from another test's AgentMemory
    (different tmp dir) is structurally invisible even if a thread somehow
    outlives its test.
    """

    def __init__(self, sandbox) -> None:
        self.sandbox = str(sandbox)
        self.commits: list[str] = []
        self.pushes: int = 0

    def __call__(self, repo_dir) -> "_FakeRepoHandle":
        return _FakeRepoHandle(self, repo_dir)


class _FakeRepoHandle:
    """What ``MemoryRepo(repo_dir)`` yields under the monkeypatch."""

    def __init__(self, recorder: FakeRepo, repo_dir) -> None:
        self._recorder = recorder
        self.repo_dir = repo_dir

    def _in_sandbox(self) -> bool:
        return str(self.repo_dir).startswith(self._recorder.sandbox)

    def commit(self, message) -> bool:
        if self._in_sandbox():
            self._recorder.commits.append(message)
        return True

    def push(self) -> bool:
        if self._in_sandbox():
            self._recorder.pushes += 1
        return True


@pytest.fixture
def fake_repo(monkeypatch, tmp_path):
    recorder = FakeRepo(sandbox=tmp_path)
    monkeypatch.setattr(session_mod, "MemoryRepo", recorder)
    # Long debounce so timers never fire during a test run.
    monkeypatch.setenv("NULL_SYNC_DEBOUNCE", "3600")
    return recorder


class TestDebounce:
    def test_write_marks_dirty_without_pushing(self, mem, fake_repo):
        mem._sync_to_remote("observe")
        assert mem._sync_dirty is True
        assert mem._sync_timer is not None
        assert fake_repo.commits == []
        assert fake_repo.pushes == 0

    def test_multiple_writes_share_one_timer(self, mem, fake_repo):
        mem._sync_to_remote("observe")
        timer1 = mem._sync_timer
        mem._sync_to_remote("decide")
        mem._sync_to_remote("mistake")
        assert mem._sync_timer is timer1
        assert mem._sync_triggers == ["observe", "decide", "mistake"]

    def test_flush_commits_once_for_all_writes(self, mem, fake_repo):
        mem._sync_to_remote("observe")
        mem._sync_to_remote("decide")
        mem._flush_sync_now()
        assert len(fake_repo.commits) == 1
        assert fake_repo.pushes == 1
        assert "observe+decide" in fake_repo.commits[0]
        assert mem._sync_dirty is False
        assert mem._sync_timer is None

    def test_flush_when_clean_is_noop(self, mem, fake_repo):
        mem._flush_sync_now()
        assert fake_repo.commits == []
        assert fake_repo.pushes == 0

    def test_observe_does_not_spawn_sync_thread(self, mem, fake_repo):
        before = {t.ident for t in threading.enumerate()}
        mem.observe("User prefers single commits over many small pushes today")
        # observe must arm the debounce timer, never an immediate-flush
        # thread. Match flush threads by their name ("null-sync-flush")
        # rather than "any new thread": under full-suite load, unrelated
        # lazy worker threads (embeddings etc.) can appear here and are
        # not failures of THIS contract.
        flushers = [t for t in threading.enumerate()
                    if t.name == "null-sync-flush" and t.ident not in before]
        assert flushers == []
        assert isinstance(mem._sync_timer, threading.Timer)
        assert fake_repo.commits == []
        assert fake_repo.pushes == 0


class TestLifecycleFlush:
    def test_checkpoint_flushes_immediately(self, mem, fake_repo, monkeypatch):
        calls = []
        monkeypatch.setattr(
            mem, "_sync_to_remote",
            lambda trigger="write", immediate=False: calls.append((trigger, immediate)),
        )
        mem.start_session(project="test")
        mem.checkpoint("midpoint")
        assert ("checkpoint", True) in calls

    def test_debrief_flushes_immediately(self, mem, fake_repo, monkeypatch):
        calls = []
        monkeypatch.setattr(
            mem, "_sync_to_remote",
            lambda trigger="write", immediate=False: calls.append((trigger, immediate)),
        )
        mem.debrief("session summary text")
        assert ("debrief", True) in calls

    def test_close_flushes_immediately(self, mem, fake_repo, monkeypatch):
        calls = []
        monkeypatch.setattr(
            mem, "_sync_to_remote",
            lambda trigger="write", immediate=False: calls.append((trigger, immediate)),
        )
        mem.close(summary="done")
        assert ("close", True) in calls

    def test_immediate_flush_runs_sync(self, mem, fake_repo):
        from pathlib import Path
        mem._sync_to_remote("hypnos", immediate=True)
        # Immediate flush runs on a worker thread — join it deterministically
        # instead of polling with sleeps (load-robust).
        mem._join_sync_threads(timeout=10)
        if fake_repo.pushes == 0:
            # Surface WHY the flush never landed: _flush_sync_now swallows
            # exceptions into sync_errors.log, which made this failure
            # undiagnosable on Windows (issue #2).
            log = Path(mem.agent_dir) / "sync_errors.log"
            detail = (log.read_text(encoding="utf-8")
                      if log.exists() else "<no sync_errors.log written>")
            pytest.fail(
                "immediate flush did not push after join "
                f"(dirty={mem._sync_dirty!r}, commits={fake_repo.commits!r}); "
                f"sync_errors.log: {detail}"
            )
        assert len(fake_repo.commits) == 1
        assert fake_repo.pushes == 1


class TestProcessWideLock:
    def test_concurrent_flushes_serialize(self, mem, fake_repo):
        """Two simultaneous flushes must produce at most one commit
        (second sees the dirty flag already cleared)."""
        mem._sync_to_remote("observe")
        threads = [threading.Thread(target=mem._flush_sync_now) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(fake_repo.commits) == 1
        assert fake_repo.pushes == 1
