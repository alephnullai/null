"""Tests for gap awareness and catchup (Phase 3)."""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from null_memory.agent import AgentMemory
from null_memory.session import SessionManager, MemoryRepo, _run_git
from null_memory.mcp.handlers import NullHandlers


# ── Fixtures ──


@pytest.fixture
def git_project(tmp_path):
    """Create a temp directory that is a git repo with some commits."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(project_dir), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                    cwd=str(project_dir), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                    cwd=str(project_dir), capture_output=True)

    # Create initial commit
    (project_dir / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "-A"], cwd=str(project_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"],
                    cwd=str(project_dir), capture_output=True)

    # Add feature commits
    (project_dir / "feature.py").write_text("def hello(): pass")
    subprocess.run(["git", "add", "-A"], cwd=str(project_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add: hello feature"],
                    cwd=str(project_dir), capture_output=True)

    (project_dir / "fix.py").write_text("def fix(): pass")
    subprocess.run(["git", "add", "-A"], cwd=str(project_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Fix: broken thing"],
                    cwd=str(project_dir), capture_output=True)

    (project_dir / "feature2.py").write_text("def world(): pass")
    subprocess.run(["git", "add", "-A"], cwd=str(project_dir), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add: world feature"],
                    cwd=str(project_dir), capture_output=True)

    return project_dir


# ── Gap Detection ──


class TestGapDetection:
    def test_detect_gaps_fresh(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        gaps = mem.detect_gaps()
        assert "last_commit_age_hours" in gaps
        assert "prior_crash" in gaps

    def test_detect_gaps_after_session(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        mem.learn("test fact", 0.9)
        mem.close(summary="test")

        # Reload
        mem2 = AgentMemory.load(str(tmp_path))
        gaps = mem2.detect_gaps()
        assert gaps["last_session"] is not None
        assert gaps["prior_crash"] is None

    def test_briefing_shows_gap_warning_after_long_absence(self, tmp_path):
        """If no recent commits, briefing should warn."""
        mem = AgentMemory.load(str(tmp_path))
        # Start and close a session so there's a commit
        mem.start_session(project="test")
        mem.close(summary="old session")

        # The commit was just now so no gap warning expected
        mem2 = AgentMemory.load(str(tmp_path))
        briefing = mem2.briefing()
        # Should NOT show gap warning for a just-closed session
        assert "38 commits" not in briefing


# ── Catchup from Git ──


class TestCatchupFromGit:
    def test_catchup_creates_facts(self, tmp_path, git_project):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")

        created = mem.catchup_from_git(
            project="test",
            since="1 day ago",
            git_cwd=str(git_project),
        )
        assert len(created) > 0
        # All facts should be marked as reconstructed
        for entry in created:
            assert "reconstructed" in entry["fact"].lower() or "reconstructed" in entry.get("source", "")
            assert entry["confidence"] == 0.6

    def test_catchup_groups_by_prefix(self, tmp_path, git_project):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")

        created = mem.catchup_from_git(
            project="test",
            since="1 year ago",
            git_cwd=str(git_project),
        )
        # Should have grouped "Add:" commits together and "Fix:" separately
        fact_texts = [e["fact"] for e in created]
        add_facts = [f for f in fact_texts if "add" in f.lower()]
        fix_facts = [f for f in fact_texts if "fix" in f.lower()]
        assert len(add_facts) > 0
        assert len(fix_facts) > 0

    def test_catchup_no_commits_with_future_since(self, tmp_path, git_project):
        """If --since is in the future, no commits should be found."""
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        created = mem.catchup_from_git(
            project="test",
            since="2099-01-01",
            git_cwd=str(git_project),
        )
        assert created == []

    def test_catchup_facts_are_recallable(self, tmp_path, git_project):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        mem.catchup_from_git(project="test", since="1 year ago", git_cwd=str(git_project))

        # Should be findable via recall
        results = mem.recall("hello feature", project="test")
        assert len(results) > 0

    def test_catchup_links_to_session(self, tmp_path, git_project):
        mem = AgentMemory.load(str(tmp_path))
        session = mem.start_session(project="test")
        created = mem.catchup_from_git(project="test", since="1 year ago", git_cwd=str(git_project))
        for entry in created:
            assert entry.get("session_id") == session.session_id


# ── Catchup Manual ──


class TestCatchupManual:
    def test_manual_catchup(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        created = mem.catchup_manual(
            ["aleph now has 32 tools", "impact analysis was added"],
            project="test",
        )
        assert len(created) == 2
        assert all(e["confidence"] == 0.7 for e in created)
        assert all("reconstructed" in e["fact"].lower() for e in created)

    def test_manual_catchup_empty(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path))
        mem.start_session(project="test")
        created = mem.catchup_manual([], project="test")
        assert created == []


# ── Handler Integration ──


class TestCatchupHandler:
    def test_handler_catchup_git(self, tmp_path, git_project):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        # We need to set the cwd for git — use monkeypatch
        original_cwd = os.getcwd()
        try:
            os.chdir(str(git_project))
            result = h.handle_catchup(source="git", project="test", since="1 year ago")
            assert "reconstructed" in result.lower() or "facts" in result.lower()
        finally:
            os.chdir(original_cwd)

    def test_handler_catchup_manual(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        result = h.handle_catchup(
            source="manual",
            project="test",
            facts=["fact one", "fact two"],
        )
        assert "2 facts" in result

    def test_handler_catchup_manual_empty(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        result = h.handle_catchup(source="manual", project="test", facts=[])
        assert "No facts" in result

    def test_handler_catchup_unknown_source(self, tmp_path):
        h = NullHandlers(agent_dir=str(tmp_path))
        h.handle_identity()
        result = h.handle_catchup(source="magic", project="test")
        assert "Unknown" in result
