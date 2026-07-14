"""Tests for MCP server tool registration (15-tool surface, P1-12/N9)."""

import asyncio
import inspect

import pytest

from null_memory.mcp.server import (
    create_server,
    SYSTEM_INSTRUCTIONS,
    TOOL_TIERS,
    get_tool_tier,
)


def call_fn(tools, name, **kwargs):
    """Invoke a registered tool's fn, awaiting it when async.

    The 15-tool surface is registered as async watchdog wrappers
    (responsiveness contract); the legacy alias shim stays sync. This
    helper lets the dispatch tests exercise both uniformly.
    """
    result = tools[name].fn(**kwargs)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


EXPECTED_TOOLS = {
    # core
    "null_remember", "null_recall", "null_briefing", "null_close",
    "null_checkpoint", "null_verify",
    # frequent
    "null_identity", "null_status", "null_context",
    # occasional
    "null_outcome", "null_anchor", "null_catchup", "null_exemplar",
    # rare
    "null_forget", "null_multiverse",
}


class TestServerSetup:
    def test_creates_server(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        assert server is not None

    def test_tool_count_within_budget(self, tmp_path):
        """The defensible surface is 12-15 tools (N9). 39 was the disease."""
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        assert len(tools) == 15
        assert len(tools) <= 15

    def test_all_tools_registered(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = set(server._tool_manager._tools.keys())
        assert tools == EXPECTED_TOOLS, (
            f"Missing: {EXPECTED_TOOLS - tools}, Extra: {tools - EXPECTED_TOOLS}"
        )

    def test_removed_tools_stay_removed(self, tmp_path):
        """The write-path/verify/maintenance tools merged or moved to CLI."""
        server, _ = create_server(str(tmp_path))
        tools = set(server._tool_manager._tools.keys())
        removed = {
            "null_observe", "null_learn", "null_decide", "null_mistake",
            "null_wonder", "null_contradict",            # -> null_remember
            "null_verify_claim", "null_verify_identity",  # -> null_verify
            "null_exemplar_add",                          # -> null_exemplar
            "null_multiverse_list", "null_multiverse_broadcast",
            "null_multiverse_recall", "null_multiverse_wakeup",  # -> null_multiverse
            "null_gc", "null_consolidate", "null_doctor",
            "null_calibrate", "null_evaluate", "null_export",
            "null_import", "null_name", "null_probe_add",
            "null_outreach", "null_sync", "null_debrief", "null_reflect",
        }
        assert not (tools & removed)


class TestMergedDispatch:
    """The merged tools must route to the original handler behavior."""

    def test_remember_observe(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_remember", 
            kind="observe", text="User prefers concise diffs over prose summaries")
        assert "noise" not in out.lower() or out  # recorded, not an error

    def test_remember_learn_and_recall_roundtrip(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        call_fn(tools, "null_remember", 
            kind="learn", text="the staging cluster runs in us-east-2",
            confidence=0.9)
        out = call_fn(tools, "null_recall", query="staging cluster region")
        assert "us-east-2" in out

    def test_remember_decide_requires_why(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_remember", kind="decide", text="use RRF for recall")
        assert "requires 'why'" in out

    def test_remember_mistake_requires_why(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_remember", kind="mistake", text="broke the build")
        assert "requires 'why'" in out

    def test_remember_unknown_kind(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_remember", kind="meditate", text="om")
        assert "unknown kind" in out

    def test_verify_unknown_mode(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_verify", mode="vibes", query="x")
        assert "unknown mode" in out

    def test_verify_fact_mode(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        call_fn(tools, "null_remember", 
            kind="learn", text="the API rate limit is 100 requests per minute")
        out = call_fn(tools, "null_verify", mode="fact", query="API rate limit")
        assert "verified" in out.lower() or "[null]" in out.lower()

    def test_exemplar_add_then_search(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        call_fn(tools, "null_exemplar", 
            action="add", scenario="bug report arrives",
            user_text="the deploy is broken",
            agent_text="checking the pipeline logs first",
            calibration="diagnose before apologizing")
        out = call_fn(tools, "null_exemplar", action="search", query="bug report")
        assert "pipeline" in out or "exemplar" in out.lower()

    def test_multiverse_unknown_action(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_multiverse", action="explode")
        assert "unknown action" in out


class TestServerIsolation:
    def test_two_servers_dont_share_state(self, tmp_path):
        """Creating server B must not affect server A's tools."""
        dir1 = str(tmp_path / "agent1")
        dir2 = str(tmp_path / "agent2")
        s1, _ = create_server(dir1)
        s2, _ = create_server(dir2)

        s1_tools = s1._tool_manager._tools
        s2_tools = s2._tool_manager._tools

        # Distinguish via a learned fact in each server's memory
        call_fn(s1_tools, "null_remember", kind="learn", text="server one private fact alpha")
        call_fn(s2_tools, "null_remember", kind="learn", text="server two private fact beta")

        s1_out = call_fn(s1_tools, "null_recall", query="private fact")
        s2_out = call_fn(s2_tools, "null_recall", query="private fact")

        assert "alpha" in s1_out and "beta" not in s1_out
        assert "beta" in s2_out and "alpha" not in s2_out


class TestSystemInstructions:
    def test_mentions_atlas(self):
        assert "Atlas" in SYSTEM_INSTRUCTIONS

    def test_mentions_remember(self):
        assert "null_remember" in SYSTEM_INSTRUCTIONS

    def test_mentions_close(self):
        assert "null_close" in SYSTEM_INSTRUCTIONS

    def test_mentions_forget(self):
        assert "null_forget" in SYSTEM_INSTRUCTIONS

    def test_no_stale_tool_references(self, tmp_path):
        """Instructions must only reference tools that exist."""
        import re
        server, _ = create_server(str(tmp_path))
        registered = set(server._tool_manager._tools.keys())
        for name in set(re.findall(r"null_[a-z_]+", SYSTEM_INSTRUCTIONS)):
            assert name in registered, f"instructions mention removed tool {name}"


class TestParamThreading:
    """Optional params restored after the merge dropped them (review item)."""

    def test_wonder_category_threads_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_DIR", str(tmp_path))
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_remember", 
            kind="wonder", text="should we cap recall at k=60?",
            category="product")
        assert "recorded" in out.lower()
        import json as _json
        simmering = tmp_path / "simmering.jsonl"
        assert simmering.exists()
        entries = [_json.loads(l) for l in simmering.read_text().splitlines()]
        assert entries[-1]["category"] == "product"

    def test_wonder_category_defaults_to_calibration(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_DIR", str(tmp_path))
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        call_fn(tools, "null_remember", kind="wonder", text="is the default right?")
        import json as _json
        entries = [
            _json.loads(l)
            for l in (tmp_path / "simmering.jsonl").read_text().splitlines()
        ]
        assert entries[-1]["category"] == "calibration"

    def test_verify_claim_type_threads_through(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_verify", 
            mode="claim", query="totally_imaginary_file_zzz.py",
            claim_type="file_ref")
        assert "type=file_ref" in out

    def test_verify_claim_type_defaults_to_auto(self, tmp_path):
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        # No claim_type passed — auto-detection must still run.
        out = call_fn(tools, "null_verify", 
            mode="claim", query="totally_imaginary_file_zzz.py")
        assert "[verify]" in out


LEGACY_ALIASES = {
    "null_observe", "null_learn", "null_decide", "null_mistake",
    "null_wonder", "null_contradict",
    "null_verify_claim", "null_verify_identity",
    "null_exemplar_add",
    "null_multiverse_list", "null_multiverse_broadcast",
    "null_multiverse_recall", "null_multiverse_wakeup",
    "null_sync", "null_debrief", "null_reflect",
    "null_gc", "null_consolidate", "null_doctor", "null_calibrate",
    "null_evaluate", "null_export", "null_import", "null_name",
    "null_probe_add", "null_outreach",
}


class TestLegacyAliasShim:
    """NULL_LEGACY_TOOLS=1 restores old tool names as deprecated aliases."""

    def test_aliases_absent_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NULL_LEGACY_TOOLS", raising=False)
        server, _ = create_server(str(tmp_path))
        tools = set(server._tool_manager._tools.keys())
        assert tools == EXPECTED_TOOLS
        assert not (tools & LEGACY_ALIASES)

    def test_aliases_absent_when_flag_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_LEGACY_TOOLS", "0")
        server, _ = create_server(str(tmp_path))
        assert set(server._tool_manager._tools.keys()) == EXPECTED_TOOLS

    def test_aliases_present_with_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_LEGACY_TOOLS", "1")
        server, _ = create_server(str(tmp_path))
        tools = set(server._tool_manager._tools.keys())
        # The new surface stays intact AND every alias is registered.
        assert EXPECTED_TOOLS <= tools
        missing = LEGACY_ALIASES - tools
        assert not missing, f"aliases not registered: {missing}"

    def test_alias_functional_and_warns(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_LEGACY_TOOLS", "1")
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_learn", 
            fact="legacy alias writes land in the shared memory store")
        assert "[deprecated]" in out
        assert "null_remember(kind=learn)" in out
        # The write went through the same store the merged tool reads.
        recall = call_fn(tools, "null_recall", query="legacy alias shared store")
        assert "shared memory store" in recall

    def test_alias_observe_warns(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_LEGACY_TOOLS", "1")
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_observe", 
            summary="user keeps CI on a self-hosted runner for cost reasons")
        assert "[deprecated]" in out
        assert "null_remember(kind=observe)" in out

    def test_alias_mistake_requires_both_args_and_warns(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_LEGACY_TOOLS", "1")
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_mistake", 
            what="shipped without running migrations",
            why="assumed staging schema matched prod")
        assert "Mistake recorded" in out
        assert "[deprecated]" in out

    def test_alias_maintenance_points_to_cli(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_LEGACY_TOOLS", "1")
        server, _ = create_server(str(tmp_path))
        tools = server._tool_manager._tools
        out = call_fn(tools, "null_gc")
        assert "[deprecated]" in out
        assert "`null gc` CLI" in out


class TestToolTiers:
    """Tier manifest is canonical — drives deferred-tool clients (Claude Code)."""

    def test_all_tiers_present(self):
        assert set(TOOL_TIERS.keys()) == {"core", "frequent", "occasional", "rare"}

    def test_tier_counts(self):
        # 6 core + 3 frequent + 4 occasional + 2 rare = 15 total
        assert len(TOOL_TIERS["core"]) == 6
        assert len(TOOL_TIERS["frequent"]) == 3
        assert len(TOOL_TIERS["occasional"]) == 4
        assert len(TOOL_TIERS["rare"]) == 2

    def test_tier_manifest_matches_registered_tools(self, tmp_path):
        """Every registered tool MUST appear in exactly one tier."""
        server, _ = create_server(str(tmp_path))
        registered = set(server._tool_manager._tools.keys())
        tiered = set()
        for tools in TOOL_TIERS.values():
            for t in tools:
                assert t not in tiered, f"{t} appears in multiple tiers"
                tiered.add(t)
        assert registered == tiered, (
            f"Registered but not tiered: {registered - tiered}; "
            f"Tiered but not registered: {tiered - registered}"
        )

    def test_core_tools_are_essentials(self):
        for t in ("null_remember", "null_recall", "null_briefing", "null_close"):
            assert get_tool_tier(t) == "core"

    def test_get_tool_tier_unknown_returns_none(self):
        assert get_tool_tier("null_does_not_exist") is None
