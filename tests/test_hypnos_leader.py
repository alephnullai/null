from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

from null_memory.hypnos_live import HypnosLiveWorker
from null_memory.migrate_v3 import init_unified_db


def _memory_for_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return SimpleNamespace(
        db=SimpleNamespace(
            unified=True,
            db_path=str(path),
            conn=conn,
        )
    )


def test_leader_election_concurrency(tmp_path):
    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    mem = _memory_for_db(db_path)
    w1 = HypnosLiveWorker(mem, cadence_seconds=30, dry_run=True)
    w2 = HypnosLiveWorker(mem, cadence_seconds=30, dry_run=True)

    results = []

    def claim(w):
        results.append(w._claim_or_refresh_leader())

    threads = [
        threading.Thread(target=claim, args=(w1,)),
        threading.Thread(target=claim, args=(w2,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 1


def test_leader_claim_migrates_legacy_plain_value(tmp_path):
    db_path = tmp_path / "unified.db"
    conn = init_unified_db(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES "
        "('hypnos_live_leader', 'legacy-worker')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES "
        "('hypnos_live_leader_at', '2000-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    mem = _memory_for_db(db_path)
    worker = HypnosLiveWorker(mem, cadence_seconds=30, dry_run=True)

    assert worker._claim_or_refresh_leader() is True
    row = mem.db.conn.execute(
        "SELECT value FROM meta WHERE key='hypnos_live_leader'"
    ).fetchone()
    leader = json.loads(row["value"])
    assert leader["id"] == worker.instance_id
    assert "at" in leader


def test_current_json_leader_refreshes_itself(tmp_path):
    db_path = tmp_path / "unified.db"
    init_unified_db(str(db_path)).close()
    mem = _memory_for_db(db_path)
    worker = HypnosLiveWorker(mem, cadence_seconds=30, dry_run=True)

    assert worker._claim_or_refresh_leader() is True
    assert worker._claim_or_refresh_leader() is True
