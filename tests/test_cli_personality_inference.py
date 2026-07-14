"""CLI entry points must speak AS the seat, not as 'atlas'.

A bare ``AgentMemory.load()`` defaults to personality='atlas'. The MCP
server infers the personality from the store path; the exchange CLI and
daemon did not — so a worker seat's org-exchange stream was named
``<machine>.atlas`` and its posts/ingests attributed to atlas. Observed
live on the athena seat's first `null exchange status` (2026-06-12).
"""

from __future__ import annotations

import json
import os

from null_memory.personality import infer_personality


class TestInferPersonality:
    def test_personalities_subdir(self, tmp_path):
        seat = tmp_path / "personalities" / "steve"
        seat.mkdir(parents=True)
        assert infer_personality(str(seat)) == "steve"

    def test_atlas_dir(self, tmp_path):
        d = tmp_path / "atlas"
        d.mkdir()
        assert infer_personality(str(d)) == "atlas"

    def test_legacy_flat_store(self, tmp_path):
        assert infer_personality(str(tmp_path)) == "atlas"

    def test_env_override_wins(self, tmp_path, monkeypatch):
        seat = tmp_path / "personalities" / "steve"
        seat.mkdir(parents=True)
        monkeypatch.setenv("NULL_PERSONALITY", "hermes")
        assert infer_personality(str(seat)) == "hermes"

    def test_mcp_handler_delegates(self, tmp_path):
        """The MCP path and the shared resolver must be the same logic."""
        from null_memory.mcp.handlers import NullHandlers
        seat = tmp_path / "personalities" / "steve"
        seat.mkdir(parents=True)
        assert NullHandlers._infer_personality(str(seat)) == "steve"


class TestHubLayoutResolution:
    def test_migrated_hub_resolves_atlas_subdir(self, tmp_path, monkeypatch):
        """Hub layout (atlas/ subdir + unified.db): _load_seat_memory must
        land on the atlas/ subdir via load(agent_dir=None) — passing the
        raw hub root would mint a fresh identity.json there (PR #37
        review fix)."""
        from null_memory.cli import _load_seat_memory
        from tests.conftest import quiesce_mem

        hub = tmp_path
        atlas_dir = hub / "atlas"
        atlas_dir.mkdir()
        with open(atlas_dir / "identity.json", "w", encoding="utf-8") as f:
            json.dump({"version": "1.0", "name": "Atlas"}, f)
        (hub / "unified.db").touch()
        monkeypatch.setenv("NULL_DIR", str(hub))
        monkeypatch.delenv("NULL_PERSONALITY", raising=False)

        assert infer_personality(str(hub)) == "atlas"

        mem = _load_seat_memory()
        try:
            assert os.path.realpath(mem.agent_dir) == os.path.realpath(
                str(atlas_dir))
            assert mem.personality == "atlas"
            assert mem.identity.get("name") == "Atlas"
            # The regression: a store rooted at the hub mints identity.json
            # at the hub root. The migrated-hub fallback must prevent it.
            assert not (hub / "identity.json").exists()
        finally:
            quiesce_mem(mem)

    def test_shared_default_dir_resolver_matches_hub_layout(
            self, tmp_path, monkeypatch):
        """default_agent_dir() (the resolver create_server/serve now share)
        must pick the atlas/ subdir on a migrated hub and the flat root
        otherwise."""
        from null_memory.personality import default_agent_dir

        monkeypatch.setenv("NULL_DIR", str(tmp_path))
        assert default_agent_dir() == str(tmp_path)
        atlas_dir = tmp_path / "atlas"
        atlas_dir.mkdir()
        assert default_agent_dir() == str(atlas_dir)


class TestExchangeCliSpeaksAsSeat:
    def test_own_stream_uses_seat_personality(self, tmp_path):
        """`null exchange status` on a worker seat must report the seat's
        own stream as <machine_id>.<seat>, never <machine_id>.atlas."""
        from tests.conftest import run_null

        seat = tmp_path / "personalities" / "steve"
        seat.mkdir(parents=True)
        with open(seat / "config.json", "w", encoding="utf-8") as f:
            json.dump({
                "machine_id": "testbox-abc123",
                "exchange": {
                    "url": str(tmp_path / "exchange-remote.git"),
                    "subscribe": ["hub-mac-ffffff.atlas"],
                },
            }, f)

        rc, out, _ = run_null("exchange", "status", tmp_path=seat)
        assert rc == 0
        assert "own stream: testbox-abc123.steve" in out
        assert "testbox-abc123.atlas" not in out
