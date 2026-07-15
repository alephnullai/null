"""Cross-platform guards for two bugs that were invisible on POSIX.

Both shipped green on ubuntu+macOS and were red only on windows-latest, and
both were silent — one behind `rmtree(ignore_errors=True)`, one behind a
`except Exception: pass`. These tests pin the invariants on EVERY platform,
so a POSIX-only dev can't reintroduce them.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time

from null_memory.fsutil import force_rmtree


class TestForceRmtreeSurvivesReadOnlyGitObjects:
    """git writes loose objects read-only (444).

    Windows refuses to delete a read-only file (WinError 5), so plain
    shutil.rmtree cannot remove any tree containing .git/objects. POSIX
    unlink is governed by the parent dir's write bit, so it never noticed.
    Every Null store is a git repo -- selftest leaked its throwaway store
    into TEMP on every Windows run, and multiverse.delete(remove_files=True)
    raised PermissionError.
    """

    def _git_repo_with_an_object(self, tmp_path) -> str:
        repo = tmp_path / "store"
        repo.mkdir()
        run = lambda *a: subprocess.run(  # noqa: E731
            ["git", *a], cwd=repo, capture_output=True, check=True,
        )
        run("init", "-q")
        run("config", "user.email", "t@t.invalid")
        run("config", "user.name", "t")
        # Stop git's background maintenance/gc from spawning transient
        # .git/objects/*.lock files that appear and vanish mid-walk (the
        # os.walk below then races a FileNotFoundError). Deterministic repo.
        run("config", "maintenance.auto", "false")
        run("config", "gc.auto", "0")
        (repo / "f.txt").write_text("x", encoding="utf-8")
        run("add", "f.txt")
        run("commit", "-qm", "c")
        return str(repo)

    def test_git_objects_really_are_read_only(self, tmp_path):
        """Guard the guard: if git ever stops doing this, we want to know."""
        repo = self._git_repo_with_an_object(tmp_path)
        objects = [
            os.path.join(dirpath, f)
            for dirpath, _, files in os.walk(os.path.join(repo, ".git", "objects"))
            for f in files
        ]
        assert objects, "expected loose git objects"
        read_only = []
        for o in objects:
            try:
                read_only.append(not (os.stat(o).st_mode & stat.S_IWRITE))
            except FileNotFoundError:
                # A transient file (e.g. a lock) vanished between walk and
                # stat — not a loose object we care about; skip it.
                continue
        assert any(read_only), "expected at least one read-only loose object"

    def test_force_rmtree_removes_a_git_repo(self, tmp_path):
        repo = self._git_repo_with_an_object(tmp_path)
        assert force_rmtree(repo) is True
        assert not os.path.exists(repo)

    def test_rmtree_error_hook_survives_os_open_and_vanished_files(self):
        """The rmtree error hook must not crash on the funcs rmtree actually
        passes it. Regression: a PermissionError on .git/objects made rmtree's
        fd-walk fail in os.open, and the naive hook did `func(path)` — os.open
        needs a flags arg, so it raised TypeError and aborted the whole delete
        (seen on macOS CI). It must also treat an already-vanished file as
        success (git maintenance lock files race the walk)."""
        from null_memory.fsutil import _chmod_and_retry
        # Must not raise for os.open under either the 3.11 (exc_info tuple) or
        # 3.12 (exception) onerror/onexc signatures:
        _chmod_and_retry(os.open, "/tmp", PermissionError(13, "denied"))
        _chmod_and_retry(os.open, "/tmp", (PermissionError, PermissionError(13, "x"), None))
        # A vanished file is a no-op success, not a raise:
        _chmod_and_retry(os.unlink, "/tmp/does-not-exist", FileNotFoundError(2, "x"))

    def test_missing_path_is_ok(self, tmp_path):
        assert force_rmtree(str(tmp_path / "nope")) is True
        assert force_rmtree(str(tmp_path / "nope"), missing_ok=False) is False


class TestIntervalClockIsHighResolution:
    """The watchdog's elapsed measurement must resolve sub-millisecond calls.

    time.monotonic() on Windows is GetTickCount64() -- 15.625ms resolution --
    so a fast tool call measures as EXACTLY 0.0 elapsed. The watchdog then
    evaluates `elapsed > soft` as False and never records the soft-budget
    breadcrumb. perf_counter is the high-resolution monotonic clock.
    """

    def test_watchdog_uses_perf_counter_not_monotonic(self):
        import ast
        import inspect
        import textwrap

        from null_memory.mcp import server

        # Inspect the CALLS, not the text -- the explanatory comment in the
        # function names time.monotonic() and would fool a substring check.
        tree = ast.parse(textwrap.dedent(inspect.getsource(server._watchdog_call)))
        clocks = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "time"
        }
        assert "perf_counter" in clocks
        assert "monotonic" not in clocks, (
            "time.monotonic() has 15.625ms resolution on Windows; a fast call "
            "measures as 0.0 elapsed and the budget breadcrumb is lost"
        )

    def test_clock_resolves_a_sub_millisecond_interval(self):
        """The property the watchdog actually depends on, asserted live."""
        t0 = time.perf_counter()
        sum(range(1000))
        elapsed = time.perf_counter() - t0
        assert elapsed > 0.0, (
            "interval clock reported 0.0 for real work -- a soft-budget "
            "violation just under the tick size would go unrecorded"
        )


class TestSandboxSnapshotSeesSameSizeWrites:
    """The sandbox guard must catch a content change that keeps the size.

    A (size, mtime_ns) fingerprint cannot see one when both writes land in
    the same filesystem timestamp tick -- ~15.6ms on Windows, and coarser
    still on some CI disks. This is not academic: a git ref file is ALWAYS
    41 bytes, so a ref moving IS a same-size content change, and the guard's
    own tests rewrite exactly such files back-to-back. Measured on this
    seat: two rapid same-size writes shared an identical (size, mtime_ns)
    in 159/200 trials. The content hash removes the dependency on timestamp
    resolution entirely.
    """

    def test_same_size_change_is_detected(self, tmp_path):
        from tests.conftest import _snapshot_null_dir

        ref = tmp_path / "main"
        ref.write_text("a" * 40 + "\n")          # a git ref: 41 bytes
        before = _snapshot_null_dir(tmp_path, hash_content=True)
        ref.write_text("b" * 40 + "\n")          # same size, new content
        after = _snapshot_null_dir(tmp_path, hash_content=True)

        assert before != after, (
            "a same-size content change went undetected -- the snapshot is "
            "relying on mtime, which collides on a coarse-timestamp filesystem"
        )
