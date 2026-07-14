"""Nebula API auth — per-launch bearer token on every /nebula/* route.

The backend serves full private memory contents on localhost; without a
token, any webpage running in the browser could fetch it. Every data and
mutation route must 401 without the launch token (header or ?token= form).
"""

from __future__ import annotations

import sqlite3

import pytest

# Experimental nebula extra — these tests only run when it's installed
# (the main CI lanes test the product surface without it).
pytest.importorskip("fastapi", reason="nebula extra not installed")
from fastapi.testclient import TestClient

from null_memory.migrate_v3 import init_unified_db
from null_memory.nebula.server import create_app


@pytest.fixture
def app_and_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    monkeypatch.delenv("NULL_NEBULA_NO_AUTH", raising=False)
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    app = create_app(unified_path=str(unified))
    return app, TestClient(app), str(unified)


def _seed_trigger(unified: str) -> int:
    conn = sqlite3.connect(unified)
    cur = conn.execute(
        """INSERT INTO outreach_triggers
           (name, kind, payload, enabled, cooldown_hours, urgency, created_at)
           VALUES ('t-auth', 'session_gap', '{}', 0, 6, 0.5, '2026-01-01')"""
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


def test_token_generated_per_launch(app_and_client):
    app, _, _ = app_and_client
    assert app.state.auth_token
    assert len(app.state.auth_token) >= 32


def test_data_routes_401_without_token(app_and_client):
    _, client, _ = app_and_client
    for path in ("/nebula/snapshot", "/nebula/identity", "/nebula/meta",
                 "/nebula/recent-events", "/nebula/fact/abc123",
                 "/nebula/triggers", "/nebula/outreaches"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} should require auth"


def test_data_routes_401_with_wrong_token(app_and_client):
    _, client, _ = app_and_client
    r = client.get("/nebula/snapshot",
                   headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
    r = client.get("/nebula/snapshot?token=wrong-token")
    assert r.status_code == 401


def test_data_routes_200_with_bearer_header(app_and_client):
    app, client, _ = app_and_client
    r = client.get(
        "/nebula/snapshot",
        headers={"Authorization": f"Bearer {app.state.auth_token}"},
    )
    assert r.status_code == 200
    assert "points" in r.json()


def test_data_routes_200_with_query_token(app_and_client):
    app, client, _ = app_and_client
    r = client.get(f"/nebula/snapshot?token={app.state.auth_token}")
    assert r.status_code == 200
    r = client.get(f"/nebula/meta?token={app.state.auth_token}")
    assert r.status_code == 200


def test_mutation_routes_401_without_token(app_and_client):
    _, client, unified = app_and_client
    trigger_id = _seed_trigger(unified)
    r = client.patch(f"/nebula/triggers/{trigger_id}", json={"enabled": 1})
    assert r.status_code == 401
    r = client.post("/nebula/outreaches/1/acknowledge")
    assert r.status_code == 401


def test_mutation_routes_work_with_token(app_and_client):
    app, client, unified = app_and_client
    trigger_id = _seed_trigger(unified)
    r = client.patch(
        f"/nebula/triggers/{trigger_id}",
        json={"enabled": 1},
        headers={"Authorization": f"Bearer {app.state.auth_token}"},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] == 1


def test_websocket_rejects_without_token(app_and_client):
    _, client, _ = app_and_client
    with pytest.raises(Exception):
        with client.websocket_connect("/nebula/events"):
            pass


def test_websocket_accepts_query_token(app_and_client):
    app, client, _ = app_and_client
    with client.websocket_connect(
        f"/nebula/events?token={app.state.auth_token}"
    ):
        pass  # connection accepted is the assertion


def test_no_auth_escape_hatch(tmp_path, monkeypatch):
    """NULL_NEBULA_NO_AUTH=1 disables auth for dev workflows."""
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    monkeypatch.setenv("NULL_NEBULA_NO_AUTH", "1")
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    app = create_app(unified_path=str(unified))
    assert app.state.auth_token is None
    client = TestClient(app)
    assert client.get("/nebula/meta").status_code == 200
