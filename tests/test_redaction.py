"""P2-17 — multiverse broadcast redaction and classifier veto hook."""

import json
import os

import pytest

from null_memory.agent import AgentMemory
from null_memory.multiverse import MultiverseManager
from null_memory.redaction import (
    REDACTED,
    redact,
    set_broadcast_classifier,
    should_block_broadcast,
)


class TestRedact:
    def test_plain_text_untouched(self):
        text = "Market crashed today, rebalancing the portfolio tomorrow"
        clean, labels = redact(text)
        assert clean == text
        assert labels == []

    def test_api_tokens(self):
        clean, labels = redact(
            "deploy used ghp_abcdefghijklmnopqrstuvwx123456 for auth")
        assert "ghp_" not in clean
        assert REDACTED in clean
        assert "api_token" in labels

    def test_openai_style_key(self):
        clean, labels = redact("set OPENAI key sk-proj1234567890abcdefgh ok")
        assert "sk-proj" not in clean
        assert "api_token" in labels

    def test_aws_access_key(self):
        clean, labels = redact("creds: AKIAIOSFODNN7EXAMPLE in the env")
        assert "AKIA" not in clean
        assert "api_token" in labels

    def test_credential_assignment(self):
        clean, labels = redact("the db password = hunter2hunter2 rotated")
        assert "hunter2" not in clean
        assert "credential_assignment" in labels

    def test_connection_string_password(self):
        clean, labels = redact(
            "use postgres://admin:s3cr3tpw@db.internal:5432/prod")
        assert "s3cr3tpw" not in clean
        assert "connection_string" in labels

    def test_private_key_block(self):
        text = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n"
                "-----END RSA PRIVATE KEY-----")
        clean, labels = redact(text)
        assert "MIIEow" not in clean
        assert "private_key" in labels

    def test_jwt(self):
        clean, labels = redact(
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c expired")
        assert "eyJ" not in clean
        assert "jwt" in labels

    def test_identity_core_terms(self):
        clean, labels = redact(
            "remember the magic banana phrase for verification",
            identity_terms={"core_terms": ["magic banana"]})
        assert "magic banana" not in clean.lower()
        assert "identity_term" in labels


class TestClassifierHook:
    def teardown_method(self):
        set_broadcast_classifier(None)

    def test_default_never_blocks(self):
        assert should_block_broadcast("anything at all") is False

    def test_custom_classifier_blocks(self):
        set_broadcast_classifier(lambda t: "salary" in t)
        assert should_block_broadcast("the salary spreadsheet leaked") is True
        assert should_block_broadcast("the weather is nice") is False

    def test_broken_classifier_fails_closed(self):
        def boom(_):
            raise RuntimeError("classifier down")
        set_broadcast_classifier(boom)
        assert should_block_broadcast("anything") is True


@pytest.fixture
def base_dir(tmp_path):
    d = str(tmp_path / "null")
    os.makedirs(d)
    return d


@pytest.fixture
def mv(base_dir):
    manager = MultiverseManager(base_dir=base_dir)
    yield manager
    manager.close()


def _setup_atlas(mv, base_dir, identity_terms=None):
    atlas_dir = os.path.join(base_dir, "atlas")
    os.makedirs(atlas_dir, exist_ok=True)
    mem = AgentMemory.load(atlas_dir)
    if identity_terms:
        identity_path = os.path.join(atlas_dir, "identity.json")
        with open(identity_path) as f:
            identity = json.load(f)
        identity["identity_terms"] = identity_terms
        with open(identity_path, "w") as f:
            json.dump(identity, f)
    mem.db.close()
    mv.register("atlas", role="manager", directory=atlas_dir)
    mv.create("logos", role="worker")


class TestBroadcastRedaction:
    def teardown_method(self):
        set_broadcast_classifier(None)

    def test_secrets_stripped_before_replication(self, mv, base_dir):
        _setup_atlas(mv, base_dir)
        result = mv.broadcast(
            "rotated the deploy token ghp_abcdefghijklmnopqrstuvwx123456 today",
            source="atlas")
        assert "ghp_" not in result["event"]
        assert "api_token" in result["redacted"]

        # No target database may contain the raw secret
        for name, fact_id in result["fact_ids"].items():
            mem = mv.get_personality(name)
            fact = mem.db.get_fact_by_id(fact_id)
            assert "ghp_" not in fact["fact"]

        # Broadcast log is clean too
        row = mv.db.conn.execute("SELECT event FROM broadcasts").fetchone()
        assert "ghp_" not in row["event"]

    def test_clean_event_passes_through(self, mv, base_dir):
        _setup_atlas(mv, base_dir)
        result = mv.broadcast("Market crashed today", source="atlas")
        assert result["event"] == "Market crashed today"
        assert "redacted" not in result

    def test_classifier_veto_blocks_broadcast(self, mv, base_dir):
        _setup_atlas(mv, base_dir)
        set_broadcast_classifier(lambda t: "do not share" in t)
        result = mv.broadcast("do not share this anywhere", source="atlas")
        assert result.get("blocked") is True
        assert result["xref_id"] is None
        assert mv.db.conn.execute(
            "SELECT COUNT(*) FROM broadcasts").fetchone()[0] == 0
