"""Tests for Phase 7.3 + 7.2 endpoints — triggers panel + ack."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

# Experimental nebula extra — these tests only run when it's installed
# (the main CI lanes test the product surface without it).
pytest.importorskip("fastapi", reason="nebula extra not installed")
from fastapi.testclient import TestClient

from null_memory.migrate_v3 import init_unified_db
from null_memory.nebula.server import create_app


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    app = create_app(unified_path=str(unified))
    client = TestClient(
        app,
        headers={"Authorization": f"Bearer {app.state.auth_token}"},
    )
    return client, str(unified)


def _seed_trigger(unified, name="t1", enabled=0, last_fired_at=None):
    conn = sqlite3.connect(unified)
    conn.execute(
        """INSERT INTO outreach_triggers
           (name, kind, payload, enabled, cooldown_hours, urgency,
            last_fired_at, created_at)
           VALUES (?, 'session_gap', '{"days":3}', ?, 6, 0.5, ?, '2026-01-01')""",
        (name, enabled, last_fired_at),
    )
    conn.commit()
    new_id = conn.execute(
        "SELECT id FROM outreach_triggers WHERE name=?", (name,)
    ).fetchone()[0]
    conn.close()
    return new_id


def _seed_outreach(unified, trigger_id=None, ack=False, sent_at=None):
    conn = sqlite3.connect(unified)
    sent_at = sent_at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO outreaches
           (trigger_id, personality, channel, subject, body, urgency,
            delivered, sent_at, acknowledged_at)
           VALUES (?, 'atlas', 'log', 'subj', 'body', 0.5, 1, ?, ?)""",
        (trigger_id, sent_at,
         datetime.now(timezone.utc).isoformat() if ack else None),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


# ── /nebula/triggers ───────────────────────────────────────────────────


def test_get_triggers_empty(app_client):
    client, _ = app_client
    r = client.get("/nebula/triggers")
    assert r.status_code == 200
    assert r.json() == {"triggers": []}


def test_get_triggers_returns_seeded(app_client):
    client, unified = app_client
    _seed_trigger(unified, name="alpha", enabled=1)
    _seed_trigger(unified, name="beta", enabled=0)
    r = client.get("/nebula/triggers")
    body = r.json()
    assert {t["name"] for t in body["triggers"]} == {"alpha", "beta"}
    enabled = [t for t in body["triggers"] if t["enabled"]][0]
    assert enabled["state"] == "ready"  # never fired, enabled
    disabled = [t for t in body["triggers"] if not t["enabled"]][0]
    assert disabled["state"] == "disabled"


def test_trigger_state_cooling_when_within_cooldown(app_client):
    client, unified = app_client
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _seed_trigger(unified, name="hot", enabled=1, last_fired_at=recent)
    r = client.get("/nebula/triggers").json()
    t = r["triggers"][0]
    assert t["state"] == "cooling"
    assert t["next_eligible_at"] is not None


# ── PATCH toggle ───────────────────────────────────────────────────────


def test_patch_trigger_toggles_enabled(app_client):
    client, unified = app_client
    tid = _seed_trigger(unified, name="x", enabled=0)
    r = client.patch(f"/nebula/triggers/{tid}", json={"enabled": 1})
    assert r.status_code == 200
    assert r.json()["enabled"] == 1
    # Verify it persisted
    r2 = client.get("/nebula/triggers").json()
    assert r2["triggers"][0]["enabled"] == 1


def test_patch_unknown_trigger_404(app_client):
    client, _ = app_client
    r = client.patch("/nebula/triggers/9999", json={"enabled": 1})
    assert r.status_code == 404


# ── /nebula/outreaches ─────────────────────────────────────────────────


def test_get_outreaches_returns_recent(app_client):
    client, unified = app_client
    tid = _seed_trigger(unified, name="x", enabled=1)
    _seed_outreach(unified, trigger_id=tid)
    _seed_outreach(unified, trigger_id=tid, ack=True)
    r = client.get("/nebula/outreaches").json()
    assert len(r["outreaches"]) == 2


def test_get_outreaches_since_filter(app_client):
    client, unified = app_client
    tid = _seed_trigger(unified, name="x", enabled=1)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    _seed_outreach(unified, trigger_id=tid, sent_at=old)
    _seed_outreach(unified, trigger_id=tid)  # default = now
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = client.get(f"/nebula/outreaches?since={cutoff}").json()
    assert len(r["outreaches"]) == 1


# ── POST acknowledge (Phase 7.2 v1) ────────────────────────────────────


def test_acknowledge_sets_acknowledged_at(app_client):
    client, unified = app_client
    tid = _seed_trigger(unified, name="x", enabled=1)
    oid = _seed_outreach(unified, trigger_id=tid)
    r = client.post(f"/nebula/outreaches/{oid}/acknowledge")
    assert r.status_code == 200
    # Verify field set
    conn = sqlite3.connect(unified)
    val = conn.execute(
        "SELECT acknowledged_at FROM outreaches WHERE id=?", (oid,)
    ).fetchone()[0]
    conn.close()
    assert val is not None


def test_acknowledge_unknown_404(app_client):
    client, _ = app_client
    r = client.post("/nebula/outreaches/9999/acknowledge")
    assert r.status_code == 404
