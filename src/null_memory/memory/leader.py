"""Single leader-election implementation for Null's maintenance engines.

Which engine runs when
======================
Null has three maintenance schedulers, all of which must coordinate so
that at most ONE mutator of a given kind is active per unified DB:

  * ``hypnos.Hypnos``            — batch sleep stages. Invoked explicitly
    (cron nightly, ``null hypnos``, wakeup hook). Single-shot; no leader
    needed — it runs in the foreground of whoever invoked it.
  * ``hypnos_live.HypnosLiveWorker`` — continuous 60s ticks. Started by
    the MCP server AND by the daemon, so several instances may coexist
    in different processes. Leader key: ``hypnos_live_leader``.
  * ``daemon.DaemonRunner``      — 15-minute outer loop (outreach +
    personality ticks); also hosts its own HypnosLiveWorker. Leader key:
    ``null_daemon_leader``.

Only-one-live-worker-per-DB invariant
=====================================
Every HypnosLiveWorker — whether embedded in an MCP server or in the
daemon — claims the SAME ``hypnos_live_leader`` key through this module.
A worker that fails the claim idles as a hot standby (its ticks are
skipped and counted in ``skipped_not_leader``), so starting N workers is
safe: exactly one performs actions until its heartbeat goes stale
(TTL expiry), at which point a standby takes over atomically.

Mechanism
=========
A JSON heartbeat ``{"id": instance_id, "at": iso_timestamp}`` lives in
the ``meta`` table under the engine's key. Claim/refresh is a single
conditional UPDATE — atomic in SQLite without an explicit transaction,
which matters because the engines run on shared connections that may
already be inside another transaction. The WHERE clause matches when:

  * the row is empty (never claimed), or
  * we already hold it (refresh), or
  * the current heartbeat is older than the TTL (stale → takeover), or
  * a legacy plain-text value is present (pre-JSON schema) and either
    matches our id or its ``<key>_at`` companion timestamp is stale.

Each LeaderLock opens its own SQLite connection (WAL, autocommit) so the
claim never races in-flight transactions on the engine's primary
connection.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LeaderLock:
    """DB-backed leader election with TTL heartbeat. See module docstring."""

    def __init__(self, db_path: str, key: str, instance_id: str):
        self.db_path = db_path
        self.key = key
        self.instance_id = instance_id
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        """Dedicated autocommit connection for claim writes (and other
        small meta reads/writes the owning engine needs off the shared
        connection)."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path, check_same_thread=False,
                isolation_level=None,  # autocommit
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=2000")
        return self._conn

    def claim_or_refresh(self, ttl_seconds: float) -> bool:
        """Claim or refresh leadership. Returns True iff we now lead.

        A lone UPDATE is atomic in SQLite — no BEGIN IMMEDIATE needed.
        The WHERE clause enforces mutual exclusion: only one concurrent
        claimant can match and apply it.
        """
        conn = self.conn
        # Ensure the row exists so UPDATE has something to match.
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, '')",
            (self.key,),
        )
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        ).isoformat()
        new_val = json.dumps({"id": self.instance_id, "at": _now_iso()})
        cur = conn.execute(
            """UPDATE meta SET value=?
               WHERE key=?
                 AND CASE
                     WHEN value='' THEN 1
                     WHEN json_valid(value)=1 THEN
                         json_extract(value, '$.id')=?
                         OR json_extract(value, '$.at') < ?
                     ELSE
                         value=?
                         OR COALESCE(
                             (SELECT value FROM meta WHERE key=?),
                             ''
                         ) < ?
                 END""",
            (
                new_val,
                self.key,
                self.instance_id, cutoff,
                self.instance_id, f"{self.key}_at", cutoff,
            ),
        )
        claimed = cur.rowcount == 1
        conn.commit()
        return claimed

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
