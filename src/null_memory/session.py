"""Session lifecycle and git-backed memory storage for Null Memory.

~/.null/ is a git repository. Every session close creates a commit.
Crash detection = uncommitted changes in the working tree.
Gap detection = age of the last commit.
Backup = git push to a remote.

Sessions are first-class objects stored in ~/.null/sessions/{id}.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

# Process-wide lock guarding git repo creation (init). ensure_repo/init may
# now be called from multiple threads/paths (the backgrounded sync flush, the
# deferred session-start git thread, CLI). Serializing init prevents two
# threads racing between the is_repo() check and `git init`.
_INIT_LOCK = threading.Lock()

# How long a commit will wait out a peer's .git/index.lock before giving up,
# and how many times it retries a raced git failure. Generous on purpose: the
# Windows CI runner is ~5x slower than a dev box, and the old budget (two
# 0.5s sleeps) was short enough that ordinary contention read as "the commit
# failed". Costs nothing when uncontended.
_COMMIT_LOCK_TIMEOUT = 10.0
_COMMIT_ATTEMPTS = 5


@dataclass
class Session:
    """A single agent session record."""
    session_id: str = ""
    started_at: str = ""
    ended_at: str | None = None
    status: str = "active"  # "active" | "completed" | "crashed"
    project: str = "global"
    personality: str = "atlas"
    git_head: str | None = None
    git_branch: str | None = None
    facts_created: int = 0
    decisions_created: int = 0
    mistakes_created: int = 0
    checkpoints: list[str] = field(default_factory=list)
    last_tool_call: str = ""

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        if not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        if not self.last_tool_call:
            self.last_tool_call = self.started_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)

    def touch(self) -> None:
        """Update last_tool_call timestamp."""
        self.last_tool_call = datetime.now(timezone.utc).isoformat()

    def add_checkpoint(self) -> None:
        """Record a checkpoint timestamp."""
        self.checkpoints.append(datetime.now(timezone.utc).isoformat())


# ── Git helpers ──


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a process and everything it spawned (best-effort)."""
    try:
        if os.name == "nt":
            # taskkill /T takes out the whole tree — including a
            # credential-helper grandchild git left behind.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_git(args: list[str], cwd: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a git command. Returns CompletedProcess (never raises on non-zero).

    Hardened for headless use (issue #4 — Windows MCP server hung ~9 min
    on sync): git must never go interactive, and the timeout must be
    authoritative even when a credential-helper grandchild inherits and
    holds our stdout/stderr pipes after git itself dies. subprocess.run's
    timeout is NOT authoritative there: it kills git, then blocks in a
    second, un-timed communicate() draining a pipe the grandchild still
    holds open.
    """
    env = {
        **os.environ,
        # Never prompt on the terminal (there is none) ...
        "GIT_TERMINAL_PROMPT": "0",
        # ... and never let Git Credential Manager pop interactive UI.
        "GCM_INTERACTIVE": "never",
    }
    popen_kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )
    # Own process group/tree so the timeout can kill ALL descendants.
    # CREATE_NO_WINDOW: git is a console app. When the parent process has no
    # console of its own — e.g. the daemon launched via pythonw or a
    # windowless Scheduled Task, or the MCP server — each git child would
    # otherwise allocate its OWN console window, flashing one on every fetch
    # (once per poke cycle / doorbell ring). The flag is inert when the
    # parent already owns a (possibly hidden) console, so it is safe to set
    # unconditionally on Windows.
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_kwargs["start_new_session"] = True

    # Stamp an explicit author/committer identity on our own commits so the
    # memory repo is self-sufficient on a machine with no global git identity
    # configured (fresh installs, and every CI runner). Without this, `git
    # commit` aborts with "Please tell me who you are" and every write silently
    # reports committed=False. This is null's private ~/.null repo, not the
    # user's project, so a fixed identity is correct (same pattern as
    # persona_wizard.py's init commit). `-c` overrides per-invocation only —
    # it never writes to the user's gitconfig.
    if args and args[0] == "commit":
        args = [
            "-c", "user.name=Null",
            "-c", "user.email=null@localhost",
        ] + args
    cmd = ["git"] + args
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except (FileNotFoundError, OSError) as e:
        return subprocess.CompletedProcess(
            args=cmd, returncode=128, stdout="", stderr=str(e),
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=cmd, returncode=proc.returncode,
            stdout=stdout, stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            # Tree is dead → pipes closed → this returns promptly.
            stdout, stderr = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            # Something still holds the pipes; abandon them rather than
            # hang — that is the whole point of this function.
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            stdout, stderr = "", ""
        return subprocess.CompletedProcess(
            args=cmd, returncode=128,
            stdout=stdout or "",
            stderr=(stderr or "") + f"\n[null] git timed out after {timeout}s; "
            "process tree killed (headless — no credential prompts possible)",
        )


def capture_git_state(cwd: str | None = None) -> tuple[str | None, str | None]:
    """Capture current git HEAD and branch from a project repo.

    This is for capturing the *project's* git state (not ~/.null's).
    Returns (head_sha, branch_name). Non-fatal.
    """
    if cwd is None:
        return None, None
    head = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    git_head = head.stdout.strip() if head.returncode == 0 else None
    git_branch = branch.stdout.strip() if branch.returncode == 0 else None
    return git_head, git_branch


# ── Memory Repository ──


class MemoryRepo:
    """Manages ~/.null/ as a git repository.

    Every session close = git commit. Crash detection = dirty tree.
    Gap detection = last commit age. Backup = git push.
    """

    def __init__(self, agent_dir: str) -> None:
        self.agent_dir = agent_dir
        self.repo_dir = self._find_repo_root(agent_dir)

    @staticmethod
    def _find_repo_root(start_dir: str) -> str:
        """Walk up from start_dir to find the directory containing .git.

        Post-migration, agent_dir may be ~/.null/atlas/ while .git lives
        at ~/.null/. Returns start_dir if no .git found (pre-init state).
        """
        d = os.path.abspath(start_dir)
        for _ in range(5):  # max 5 levels up
            if os.path.isdir(os.path.join(d, ".git")):
                return d
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        return start_dir  # fallback: no .git found yet (init will create it)

    def is_repo(self) -> bool:
        """Check if a git repo exists at the repo root."""
        return os.path.isdir(os.path.join(self.repo_dir, ".git"))

    def init(self) -> bool:
        """Initialize git repo if not already one. Returns True if initialized.

        Idempotent and thread-safe: callable from the foreground (rare) and
        from background sync/commit threads. The _INIT_LOCK closes the race
        where two threads both pass the is_repo() check and both run
        `git init` against the same dir.

        NB: callers that need the repo to be USABLE on return must use
        :meth:`ensure_repo`, not this — see its docstring.
        """
        if self.is_repo():
            return False

        with _INIT_LOCK:
            # Re-check under the lock — another thread may have just init'd.
            if self.is_repo():
                return False
            return self._init_locked()

    def _init_locked(self) -> bool:
        """The body of init(). Caller MUST hold _INIT_LOCK.

        Split out so ensure_repo() can run it while already holding the lock,
        keeping the whole check-then-init sequence atomic against peers.
        """
        os.makedirs(self.repo_dir, exist_ok=True)

        result = _run_git(["init"], cwd=self.repo_dir)
        if result.returncode != 0:
            return False

        # Create .gitignore for transient files
        gitignore_path = os.path.join(self.repo_dir, ".gitignore")
        if not os.path.isfile(gitignore_path):
            # encoding pinned: the header's em dash under the Windows
            # default (cp1252) crashed every strict-UTF-8 reader.
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write("# Null Memory — transient files\n")
                f.write(".lock\n")
                f.write("*.tmp\n")
                f.write("active_session.json\n")
                # org exchange clone — its own repo, never tracked
                # by the store (issue #20 Phase B)
                f.write("exchange/\n")

        # Initial commit
        _run_git(["add", "-A"], cwd=self.repo_dir)
        _run_git(
            ["commit", "-m", "null: initialize memory repo", "--allow-empty"],
            cwd=self.repo_dir,
        )
        return True

    def _checkpoint_wals(self) -> None:
        """Fold WAL contents into every committed .db under the repo root
        (non-recursive into seats with their own .git — those repos run
        their own sync). Best-effort per file; see commit() for rationale
        (issue #28)."""
        import sqlite3 as _sqlite3
        for root, dirs, files in os.walk(self.repo_dir):
            # Don't descend into .git or nested repos (worker seats have
            # their own .git — file or dir — and run their own sync).
            dirs[:] = [
                d for d in dirs
                if d != ".git"
                and not os.path.exists(os.path.join(root, d, ".git"))
            ]
            for f in files:
                if f.endswith(".db"):
                    path = os.path.join(root, f)
                    try:
                        conn = _sqlite3.connect(path, timeout=2)
                        try:
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        finally:
                            conn.close()
                    except _sqlite3.Error:
                        pass  # busy/corrupt db must not abort the commit

    def ensure_repo(self) -> bool:
        """Guarantee a fully-initialized repo before anyone touches it.

        Takes _INIT_LOCK across the CHECK as well as the init. That matters:
        ``git init`` creates the ``.git`` DIRECTORY before the repo is
        usable, and :meth:`is_repo` only stats that directory — so a thread
        that checked ``is_repo()`` outside the lock could walk straight into
        a half-built repo mid-init and get "fatal: not a git repository".
        Holding the lock here makes a racing caller WAIT for the initializing
        thread instead of observing its intermediate state.
        """
        with _INIT_LOCK:
            if self.is_repo():
                return True
            self._init_locked()
            return self.is_repo()

    def _wait_for_index_lock(self, deadline: float) -> bool:
        """Block while a peer holds .git/index.lock. True if it cleared.

        Never proceed while the lock is held: git would just fail with
        "Unable to create '.git/index.lock': File exists".
        """
        lock_path = os.path.join(self.repo_dir, ".git", "index.lock")
        while os.path.exists(lock_path):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def commit(self, message: str, allow_empty: bool = False) -> bool:
        """Stage all changes and commit. Returns True if a commit was created.

        Lazily initializes the repo if it does not exist yet: session start no
        longer inits the repo on the foreground request path, so the first
        commit is what brings the repo into being. The commit path therefore
        must NOT depend on the deferred session-start git thread having won
        the race (test_close_commits_even_if_deferred_thread_lost_the_race).

        Concurrency (all three of these were observed failing on Windows CI,
        where a slow runner widens every window):

          * ``git init`` half-done -> "fatal: not a git repository". Closed by
            ensure_repo(), which checks under the init lock.
          * a peer holds .git/index.lock -> "Unable to create index.lock".
            The old loop slept 0.5s twice and then BARGED AHEAD while the lock
            was still held, so any peer git command taking >1s in total made
            this return False. Now we wait the lock out against a deadline.
          * a transient raced failure (e.g. `add -A` losing to a concurrent
            index write) -> retried with backoff rather than given up on.

        The happy path is unchanged and pays nothing: no lock, no contention,
        commit on the first attempt.
        """
        if not self.ensure_repo():
            return False

        # Issue #28: recent writes live in the (correctly gitignored) WAL
        # until a checkpoint folds them into the .db file. Committing
        # without checkpointing pushes a store that a fresh clone reads as
        # missing those writes — silent data loss. Checkpoint every db in
        # the repo before staging. TRUNCATE is safe against live readers/
        # writers (brief block) and best-effort: a busy db must not abort
        # the commit (next sync catches up).
        self._checkpoint_wals()

        deadline = time.monotonic() + _COMMIT_LOCK_TIMEOUT
        for attempt in range(_COMMIT_ATTEMPTS):
            self._wait_for_index_lock(deadline)

            _run_git(["add", "-A"], cwd=self.repo_dir)

            if not allow_empty:
                status = _run_git(["status", "--porcelain"], cwd=self.repo_dir)
                if not status.stdout.strip():
                    return False  # Nothing to commit — a real answer, not a race

            cmd = ["commit", "-m", message]
            if allow_empty:
                cmd.append("--allow-empty")
            result = _run_git(cmd, cwd=self.repo_dir)
            if result.returncode == 0:
                return True

            # Anything else is a lost race with a peer git process; back off
            # and retry until the deadline rather than reporting "not
            # committed" for what is really "try again in a moment".
            if time.monotonic() >= deadline or attempt == _COMMIT_ATTEMPTS - 1:
                self._log_commit_failure(result, attempt)
                return False
            time.sleep(min(0.1 * (2 ** attempt), 1.0))

        return False

    @staticmethod
    def _log_commit_failure(result, attempt: int) -> None:
        """Say WHY a commit finally gave up, on stderr.

        commit() answers a bare bool, so a failure surfaces to callers (and to
        CI) as nothing more than `committed=False` — "assert False is True",
        with no cause attached. That opacity is the whole reason the git-race
        flakes took so long to pin down: every diagnosis had to be reconstructed
        from scratch. One line here turns the next occurrence into a fact
        instead of a hunt. It cannot change behaviour: we are already returning
        False on this path.
        """
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        why = detail[0][:200] if detail else f"rc={result.returncode}"
        print(
            f"[null] git commit failed after {attempt + 1} attempt(s) — "
            f"reporting committed=False. git said: {why}",
            file=sys.stderr,
        )

    def push(self, timeout: int = 15) -> bool:
        """Push to remote. Returns True if successful or no remote configured."""
        if not self.is_repo():
            return False
        # Check if remote exists
        remote = _run_git(["remote"], cwd=self.repo_dir)
        if not remote.stdout.strip():
            return True  # No remote, not an error
        result = _run_git(["push"], cwd=self.repo_dir, timeout=timeout)
        if result.returncode == 0:
            # Doorbell (issue #20 Phase B): after a real push, fire one
            # contentless UDP datagram at each configured peer so their
            # daemon fetches now instead of at the next poll. Silent
            # best-effort — the poll is the delivery guarantee.
            try:
                from null_memory.doorbell import ring_from_store
                ring_from_store(self.repo_dir)
            except Exception:
                pass
        return result.returncode == 0

    def pull(self, timeout: int = 15) -> bool:
        """Pull from remote. Returns True if successful or no remote configured."""
        if not self.is_repo():
            return False
        remote = _run_git(["remote"], cwd=self.repo_dir)
        if not remote.stdout.strip():
            return True
        result = _run_git(["pull", "--rebase"], cwd=self.repo_dir, timeout=timeout)
        return result.returncode == 0

    def commit_and_push(self, message: str) -> bool:
        """Commit all changes and push to remote in background thread."""
        import threading
        def _do():
            self.commit(message)
            self.push()
        t = threading.Thread(target=_do, daemon=True)
        t.start()
        return True

    def has_uncommitted_changes(self) -> bool:
        """Check for uncommitted changes (crash detection)."""
        if not self.is_repo():
            return False
        status = _run_git(["status", "--porcelain"], cwd=self.repo_dir)
        return bool(status.stdout.strip())

    def last_commit_time(self) -> datetime | None:
        """Get timestamp of the last commit."""
        if not self.is_repo():
            return None
        result = _run_git(
            ["log", "-1", "--format=%aI"],
            cwd=self.repo_dir,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return datetime.fromisoformat(result.stdout.strip())
        except ValueError:
            return None

    def last_commit_message(self) -> str | None:
        """Get the message of the last commit."""
        if not self.is_repo():
            return None
        result = _run_git(
            ["log", "-1", "--format=%s"],
            cwd=self.repo_dir,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def log(self, limit: int = 10) -> list[dict]:
        """Get recent commit log as list of {hash, message, timestamp}."""
        if not self.is_repo():
            return []
        result = _run_git(
            ["log", f"-{limit}", "--format=%H|%aI|%s"],
            cwd=self.repo_dir,
        )
        if result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append({
                    "hash": parts[0],
                    "timestamp": parts[1],
                    "message": parts[2],
                })
        return entries

    def diff_uncommitted(self) -> str:
        """Get a summary of uncommitted changes."""
        if not self.is_repo():
            return ""
        result = _run_git(["diff", "--stat"], cwd=self.repo_dir)
        untracked = _run_git(["ls-files", "--others", "--exclude-standard"], cwd=self.repo_dir)
        parts = []
        if result.stdout.strip():
            parts.append(result.stdout.strip())
        if untracked.stdout.strip():
            parts.append(f"Untracked: {untracked.stdout.strip()}")
        return "\n".join(parts)


# ── Session Manager ──


class SessionManager:
    """Manages session lifecycle with git-backed storage."""

    def __init__(self, agent_dir: str, personality: str = "atlas") -> None:
        self.agent_dir = agent_dir
        # The store's own personality — stamped onto every session record
        # this manager creates. The 'atlas' default is back-compat only;
        # AgentMemory always passes its real personality (init-path bleed
        # audit: a worker store's sessions/*.json must never say 'atlas').
        self.personality = personality
        self._sessions_dir = os.path.join(agent_dir, "sessions")
        self._active_path = os.path.join(agent_dir, "active_session.json")
        self.repo = MemoryRepo(agent_dir)
        # Serializes session-record writes. The deferred session-start git
        # thread back-fills git HEAD via save_session() at the same time the
        # foreground keeps mutating + saving the session (checkpoints, fact
        # counts). Two concurrent os.replace() onto the same target raise
        # PermissionError (WinError 5) on Windows; this lock makes the write
        # atomic across threads. Reentrant so finish_session_git can hold it
        # across its whole read-modify-write while save_session/_atomic_write
        # re-acquire it underneath.
        self._write_lock = threading.RLock()

    def _ensure_dirs(self) -> None:
        os.makedirs(self._sessions_dir, exist_ok=True)

    def _session_path(self, session_id: str) -> str:
        return os.path.join(self._sessions_dir, f"{session_id}.json")

    def _atomic_write(self, path: str, data: dict) -> None:
        """Write JSON atomically via tempfile + os.replace (thread-safe).

        Retries the os.replace on a transient Windows sharing violation
        (WinError 5 / WinError 32): now that a background git thread may run
        `git add -A`/`status` over the store while the foreground writes
        session records, git's working-tree scan can momentarily hold a
        handle on the target file, making os.replace fail. The _write_lock
        serializes our own writers; the retry rides out git's transient lock.
        """
        import time
        dir_name = os.path.dirname(path)
        os.makedirs(dir_name, exist_ok=True)
        with self._write_lock:
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                last_err: OSError | None = None
                for attempt in range(5):
                    try:
                        os.replace(tmp_path, path)
                        return
                    except PermissionError as e:  # WinError 5/32 — transient share lock
                        last_err = e
                        if os.name != "nt" or attempt == 4:
                            raise
                        time.sleep(0.1)
                if last_err is not None:
                    raise last_err
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def ensure_repo(self) -> None:
        """Initialize git repo if needed."""
        self.repo.init()

    def save_session(self, session: Session) -> None:
        """Persist a session record to disk (no commit yet)."""
        self._ensure_dirs()
        self._atomic_write(self._session_path(session.session_id), session.to_dict())

    def start_session(self, project: str = "global", git_cwd: str | None = None,
                      defer_git: bool = False) -> Session:
        """Create a new active session.

        Captures the *project's* git state for gap detection.
        Also checks for crashed previous sessions.

        When ``defer_git`` is True the foreground does ZERO git work: repo
        init, crash detection/commit, and project git-state capture are all
        skipped here and instead run later on a background thread (see
        ``finish_session_git``). The session record and active pointer (plain
        file writes, no git) are still written synchronously so the caller
        gets a usable Session immediately. This keeps git latency
        (slow disk / large repo / push) off the MCP request hot path.
        """
        if not defer_git:
            self.ensure_repo()

            # Check for crash first
            crashed = self.detect_crash()
            if crashed is not None:
                self.mark_crashed(crashed)

            git_head, git_branch = capture_git_state(cwd=git_cwd)
        else:
            git_head, git_branch = None, None

        session = Session(
            project=project,
            personality=self.personality,
            git_head=git_head,
            git_branch=git_branch,
        )

        self.save_session(session)

        # Write active session pointer (excluded from git via .gitignore)
        self._atomic_write(self._active_path, {
            "session_id": session.session_id,
            "started_at": session.started_at,
        })

        return session

    def finish_session_git(self, session: Session,
                           git_cwd: str | None = None) -> None:
        """Run the deferred git work for a session started with defer_git=True.

        Idempotent and safe to call from a background daemon thread. Ensures
        the repo exists and back-fills the project's git HEAD/branch onto the
        (already created) session record. Never raises — git problems must
        not surface on the thread that owns the request.

        Note: prior-session crash detection is intentionally NOT done here.
        By the time a new session starts, the active pointer already points
        at THIS session, so re-running detect_crash would mis-classify the
        live session. Crash detection runs once, up front, at load time
        (AgentMemory captures it into _prior_crash and commits the marker on
        this same background thread via mark_crashed).
        """
        try:
            self.ensure_repo()

            git_head, git_branch = capture_git_state(cwd=git_cwd)
            if git_head is not None or git_branch is not None:
                # Set on the live object for in-process readers...
                session.git_head = git_head
                session.git_branch = git_branch
                # ...but persist via a read-modify-write of the on-disk record
                # so we patch ONLY the git fields and never clobber foreground
                # mutations (checkpoints, fact counts) that landed meanwhile.
                # The (reentrant) lock is held across the WHOLE read-modify-
                # write: releasing between the read and the save let a
                # foreground checkpoint slip into the gap and get clobbered
                # with stale fact counts (TOCTOU).
                with self._write_lock:
                    on_disk = self.load_session(session.session_id) or session
                    on_disk.git_head = git_head
                    on_disk.git_branch = git_branch
                    self.save_session(on_disk)
        except Exception:
            pass

    def end_session(self, session: Session, summary: str = "") -> bool:
        """Mark session as completed, remove active pointer, git commit.

        Returns True if a commit was created.
        """
        session.status = "completed"
        session.ended_at = datetime.now(timezone.utc).isoformat()
        self.save_session(session)

        # Remove active session pointer
        try:
            os.unlink(self._active_path)
        except OSError:
            pass

        # Git commit all changes from this session (allow-empty: data may already
        # be committed by background sync threads from individual writes)
        msg = f"session {session.session_id[:8]}: {summary or session.project}"
        msg += f" | facts={session.facts_created} decisions={session.decisions_created}"
        return self.repo.commit(msg, allow_empty=True)

    def checkpoint_commit(self, session: Session, note: str = "") -> bool:
        """Mid-session checkpoint commit (for crash resilience)."""
        session.add_checkpoint()
        self.save_session(session)
        msg = f"checkpoint {session.session_id[:8]}: {note or 'mid-session save'}"
        return self.repo.commit(msg)

    def load_session(self, session_id: str) -> Session | None:
        """Load a session record from disk."""
        path = self._session_path(session_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            try:
                return Session.from_dict(json.load(f))
            except (json.JSONDecodeError, KeyError):
                return None

    def get_active_session(self) -> Session | None:
        """Check for an active (possibly crashed) session."""
        if not os.path.isfile(self._active_path):
            return None
        try:
            with open(self._active_path, "r", encoding="utf-8") as f:
                pointer = json.load(f)
            session_id = pointer.get("session_id")
            if not session_id:
                return None
            return self.load_session(session_id)
        except (json.JSONDecodeError, OSError):
            return None

    def detect_crash(self) -> Session | None:
        """Check if the previous session crashed.

        Two signals:
        1. active_session.json exists pointing to an "active" session
        2. Git working tree has uncommitted changes

        Distinguishes three timescales:
        - <5 minutes of recent activity → MCP restart (not a crash). Silently
          close the old session; no warning.
        - 5 minutes – 6 hours → real crash candidate. Surface the warning.
        - >6 hours → abandoned. Auto-complete, no crash warning.
        """
        session = self.get_active_session()
        if session is not None and session.status == "active":
            # Check how stale this session is
            try:
                last_activity = session.last_tool_call or session.started_at
                last_dt = datetime.fromisoformat(last_activity)
                now = datetime.now(timezone.utc)
                # Normalize: ensure both are tz-aware
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_seconds = (now - last_dt).total_seconds()
                age_hours = age_seconds / 3600

                # MCP restart — recent activity means the server died but was
                # replaced immediately. Silently close; no crash warning.
                if age_seconds < 300:  # <5 minutes
                    session.status = "completed"
                    session.ended_at = datetime.now(timezone.utc).isoformat()
                    self.save_session(session)
                    try:
                        os.unlink(self._active_path)
                    except OSError:
                        pass
                    try:
                        self.repo.commit(
                            f"session {session.session_id[:8]}: auto-closed "
                            f"(MCP restart, {age_seconds:.0f}s inactive)"
                        )
                    except Exception:
                        pass
                    return None  # Not a crash — just a restart

                if age_hours > 6:
                    # Stale session — mark as completed (abandoned), not crashed
                    session.status = "completed"
                    session.ended_at = datetime.now(timezone.utc).isoformat()
                    self.save_session(session)
                    try:
                        os.unlink(self._active_path)
                    except OSError:
                        pass
                    self.repo.commit(
                        f"session {session.session_id[:8]}: auto-completed "
                        f"(abandoned after {age_hours:.1f}h inactivity)"
                    )
                    return None  # Not a crash, just an abandoned session
            except (ValueError, TypeError):
                pass  # Can't parse timestamp — fall through to crash detection

            return session
        # Also check for dirty tree without active pointer (edge case)
        if self.repo.has_uncommitted_changes():
            # There are uncommitted changes but no active session pointer
            # This means something was written but never committed
            return None  # Can't reconstruct the session, but gap detection will catch it
        return None

    def mark_crashed_files(self, session: Session) -> None:
        """File-I/O half of crash marking: persist the crashed record and
        retire the active pointer. Cheap local writes — these MUST run
        synchronously in the foreground, BEFORE the new session's active
        pointer is written. (Deferring them to the background git thread
        unlinked the NEW session's pointer after the foreground had written
        it, destroying live crash detection for every session that followed
        a crash.) As a second guard, the pointer is only removed while it
        still names the crashed session."""
        session.status = "crashed"
        session.ended_at = datetime.now(timezone.utc).isoformat()
        self.save_session(session)
        try:
            with open(self._active_path, "r", encoding="utf-8") as f:
                pointer = json.load(f)
            if pointer.get("session_id") == session.session_id:
                os.unlink(self._active_path)
        except (json.JSONDecodeError, OSError):
            pass

    def commit_crash_marker(self, session: Session) -> bool:
        """Git half of crash marking — safe to defer to a background thread
        (the file half in mark_crashed_files must already have run)."""
        return self.repo.commit(
            f"session {session.session_id[:8]}: CRASHED"
            f" | started={session.started_at} facts={session.facts_created}"
        )

    def mark_crashed(self, session: Session) -> None:
        """Mark a session as crashed and commit the crash record."""
        self.mark_crashed_files(session)
        self.commit_crash_marker(session)

    def detect_gaps(self) -> dict:
        """Detect gaps in memory coverage.

        Returns: {
            "last_commit_age_hours": float | None,
            "last_commit_message": str | None,
            "has_uncommitted": bool,
            "prior_crash": Session | None,
            "last_session": Session | None,
        }
        """
        last_time = self.repo.last_commit_time()
        age_hours = None
        if last_time is not None:
            now = datetime.now(timezone.utc)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            age_hours = (now - last_time).total_seconds() / 3600

        crashed = self.detect_crash()
        last_session = self.last_completed_session()

        return {
            "last_commit_age_hours": round(age_hours, 1) if age_hours is not None else None,
            "last_commit_message": self.repo.last_commit_message(),
            "has_uncommitted": self.repo.has_uncommitted_changes(),
            "prior_crash": crashed,
            "last_session": last_session,
        }

    def list_sessions(self, limit: int = 20) -> list[Session]:
        """List recent sessions, newest first."""
        if not os.path.isdir(self._sessions_dir):
            return []
        sessions: list[Session] = []
        for fname in os.listdir(self._sessions_dir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._sessions_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    sessions.append(Session.from_dict(json.load(f)))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
        sessions.sort(key=lambda s: s.started_at, reverse=True)
        return sessions[:limit]

    def last_completed_session(self) -> Session | None:
        """Get the most recent completed session."""
        for s in self.list_sessions():
            if s.status == "completed":
                return s
        return None

    def cleanup_old_sessions(self, keep: int = 20, max_age_days: int = 90) -> int:
        """Remove old session files. Returns count removed."""
        sessions = self.list_sessions(limit=10000)
        if len(sessions) <= keep:
            return 0
        now = datetime.now(timezone.utc)
        removed = 0
        for session in sessions[keep:]:
            try:
                started = datetime.fromisoformat(session.started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if (now - started).days > max_age_days:
                    os.unlink(self._session_path(session.session_id))
                    removed += 1
            except (ValueError, OSError):
                continue
        return removed
