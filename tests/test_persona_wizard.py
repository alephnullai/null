"""Tests for persona creation wizard.

Uses tmp paths and monkeypatching to avoid touching the real ~/.null/.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from null_memory.persona_wizard import (
    TemplateInfo,
    create_from_template,
    get_template,
    hydrate_identity,
    list_templates,
    templates_dir,
)


class TestTemplateDiscovery:
    def test_templates_dir_exists(self):
        td = templates_dir()
        assert td.is_dir(), f"Expected templates at {td}"

    def test_list_templates_has_five(self):
        templates = list_templates()
        ids = {t.id for t in templates}
        expected = {
            "warm-coach", "terse-engineer", "creative-collaborator",
            "business-analyst", "twitter-growth",
        }
        assert expected.issubset(ids), f"Missing: {expected - ids}"

    def test_each_template_has_identity(self):
        for t in list_templates():
            assert isinstance(t.identity, dict)
            assert t.identity.get("template_id") == t.id

    def test_each_template_has_description(self):
        for t in list_templates():
            assert t.description, f"{t.id} missing description"
            assert len(t.description) > 10

    def test_get_template_known(self):
        t = get_template("warm-coach")
        assert t is not None
        assert t.id == "warm-coach"

    def test_get_template_unknown(self):
        assert get_template("does-not-exist") is None


class TestHydrateIdentity:
    def test_replaces_name_and_focus(self):
        template = get_template("warm-coach")
        identity = hydrate_identity(template, name="aria", focus="finance")
        assert identity["name"] == "aria"
        assert identity["focus"] == "finance"

    def test_preserves_other_fields(self):
        template = get_template("terse-engineer")
        identity = hydrate_identity(template, name="max", focus="rust")
        assert identity["role"] == template.identity["role"]
        assert "anti_patterns" in identity

    def test_extra_style_merged(self):
        template = get_template("creative-collaborator")
        identity = hydrate_identity(
            template, name="muse", focus="writing",
            extra_style={"pace": "very slow"},
        )
        assert identity["working_style"]["pace"] == "very slow"

    def test_does_not_mutate_template(self):
        template = get_template("warm-coach")
        original_name = template.identity["name"]
        hydrate_identity(template, name="aria", focus="x")
        assert template.identity["name"] == original_name


class TestCreateFromTemplate:
    """End-to-end create — uses tmp HOME to avoid real ~/.null/."""

    @pytest.fixture(autouse=True)
    def _isolate(self, fake_home):
        # MultiverseManager + bootstrap both use os.path.expanduser;
        # fake_home redirects ~ cross-platform (HOME on POSIX,
        # USERPROFILE/HOMEDRIVE+HOMEPATH on Windows) to isolate state.
        yield

    def test_unknown_template_raises(self):
        with pytest.raises(ValueError, match="Template"):
            create_from_template(
                name="aria", template_id="not-real", focus="x",
            )

    def test_creates_persona_directory(self):
        result = create_from_template(
            name="testbot", template_id="warm-coach",
            focus="testing the wizard",
            skip_bootstrap=True,
        )
        assert Path(result["dir"]).is_dir()
        identity_file = Path(result["dir"]) / "identity.json"
        assert identity_file.is_file()

    def test_identity_has_user_values(self):
        result = create_from_template(
            name="testbot", template_id="terse-engineer",
            focus="rust dev",
            skip_bootstrap=True,
        )
        with open(Path(result["dir"]) / "identity.json") as f:
            identity = json.load(f)
        assert identity["name"] == "testbot"
        assert identity["focus"] == "rust dev"

    def test_returns_mcp_config(self):
        result = create_from_template(
            name="testbot", template_id="warm-coach", focus="x",
            skip_bootstrap=True,
        )
        assert "mcp_config" in result
        config = json.loads(result["mcp_config"])
        assert "testbot" in config
        assert "command" in config["testbot"]
        assert "args" in config["testbot"]

    def test_invalid_name_rejected_before_creation(self):
        # Reserved name
        with pytest.raises(ValueError):
            create_from_template(
                name="atlas", template_id="warm-coach", focus="x",
                skip_bootstrap=True,
            )

    def test_bootstrap_returns_zero_when_db_missing(self):
        """Bootstrap silently returns 0s when ~/.null/unified.db doesn't exist (isolated test env).

        Real-env verification: the function correctly seeds facts/exemplars/anchors
        when unified.db is present — verified manually and locked in via the
        end-to-end create script during development. This test ensures graceful
        degradation when DB is missing.
        """
        result = create_from_template(
            name="testbot", template_id="terse-engineer",
            focus="rust dev",
            answers={
                "user_context": "Senior Rust dev",
                "persona_purpose": "Pair programming",
                "success_signal": "Faster reviews",
            },
        )
        # In isolated tmp HOME, unified.db doesn't exist → 0s (no crash)
        assert "facts_added" in result
        assert "exemplars_added" in result
        assert "anchors_set" in result
        assert result["facts_added"] == 0
        assert result["exemplars_added"] == 0
        assert result["anchors_set"] == 0
