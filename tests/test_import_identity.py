"""import_from must never clobber a seat's identity with an empty one.

Scoped exports (the onboarding-packet flow, export --no-identity) carry
``"identity": {}`` by design — the sender's identity is deliberately
withheld. Importing such a packet previously overwrote the RECIPIENT's
identity.json with the empty dict. Live incident (2026-06-12): athena's
onboarded identity — working_style, escalation rules, anti_patterns,
color — was wiped by importing her own onboarding packet from Atlas;
recovered only because the seat store is git-tracked.
"""

from __future__ import annotations

import json
import os

from null_memory.agent import AgentMemory

SEAT_IDENTITY = {
    "version": "1.0",
    "name": "athena",
    "role": "worker",
    "focus": "windows platform",
    "description": "test seat",
    "working_style": {"pace": "deliberate and verified"},
    "anti_patterns": ["never claim untested work is done"],
    "color": "#d4af37",
}


def _knowledge_packet(identity=None):
    """A v2 scoped export: facts only, identity withheld unless given."""
    return {
        "version": "2.0",
        "identity": identity if identity is not None else {},
        "knowledge": [{
            "id": "abc123def456",
            "fact": "the olive tree beats the salt spring",
            "confidence": 0.9,
            "project": "hiwave",
        }],
        "decisions": [],
        "mistakes": [],
        "reflections": [],
    }


def _write_seat_identity(tmp_path):
    with open(tmp_path / "identity.json", "w", encoding="utf-8") as f:
        json.dump(SEAT_IDENTITY, f)


def test_empty_packet_identity_preserves_seat_identity(tmp_path):
    _write_seat_identity(tmp_path)

    mem = AgentMemory.import_from(_knowledge_packet(), agent_dir=str(tmp_path))

    # On disk: untouched.
    with open(tmp_path / "identity.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == SEAT_IDENTITY
    # In memory: the seat's identity, not the packet's empty one.
    assert mem.identity.get("name") == "athena"
    assert mem.identity.get("working_style") == {"pace": "deliberate and verified"}
    # The knowledge still imported.
    assert mem.last_import_counts["knowledge"]["imported"] == 1


def test_missing_identity_key_preserves_seat_identity(tmp_path):
    """Legacy exports may omit the key entirely — same contract."""
    _write_seat_identity(tmp_path)
    packet = _knowledge_packet()
    del packet["identity"]

    AgentMemory.import_from(packet, agent_dir=str(tmp_path))

    with open(tmp_path / "identity.json", encoding="utf-8") as f:
        assert json.load(f) == SEAT_IDENTITY


def test_nonempty_packet_identity_still_written(tmp_path):
    """Full exports (identity included) keep their restore semantics."""
    packet = _knowledge_packet(identity={"version": "1.0", "name": "restored"})

    mem = AgentMemory.import_from(packet, agent_dir=str(tmp_path))

    with open(tmp_path / "identity.json", encoding="utf-8") as f:
        assert json.load(f)["name"] == "restored"
    assert mem.identity["name"] == "restored"


def test_empty_identity_into_fresh_seat_creates_no_identity_file(tmp_path):
    """Nothing to preserve, nothing to write — don't invent an empty
    identity.json for the store to sync."""
    AgentMemory.import_from(_knowledge_packet(), agent_dir=str(tmp_path))
    assert not os.path.isfile(tmp_path / "identity.json")


def test_corrupt_existing_identity_does_not_break_import(tmp_path):
    (tmp_path / "identity.json").write_text("{ not json", encoding="utf-8")

    mem = AgentMemory.import_from(_knowledge_packet(), agent_dir=str(tmp_path))

    # Import succeeded; the corrupt file is left for the user, not replaced.
    assert mem.last_import_counts["knowledge"]["imported"] == 1
    assert (tmp_path / "identity.json").read_text(encoding="utf-8") == "{ not json"
