"""Tests for persona schema validator."""

from __future__ import annotations

import json

import pytest

from null_memory.persona_schema import (
    NAME_PATTERN,
    REQUIRED_FIELDS,
    RESERVED_NAMES,
    VALID_ROLES,
    ValidationResult,
    validate,
    validate_file,
)


def _good_identity(**overrides) -> dict:
    base = {
        "version": "2.0",
        "name": "aria",
        "role": "worker",
        "focus": "personal finance coaching for new professionals",
        "description": "A patient finance coach.",
    }
    base.update(overrides)
    return base


class TestValidateBasics:
    def test_valid_minimal(self):
        r = validate(_good_identity())
        assert r.ok, r.report()

    def test_not_a_dict(self):
        r = validate("hello")
        assert not r.ok
        assert "must be a JSON object" in r.report()

    def test_missing_required(self):
        for field in REQUIRED_FIELDS:
            bad = _good_identity()
            del bad[field]
            r = validate(bad)
            assert not r.ok
            assert field in r.report()


class TestName:
    def test_valid_names(self):
        for name in ["aria", "scout", "max", "helix-2", "a_bot", "alpha9"]:
            r = validate(_good_identity(name=name))
            assert r.ok, f"{name}: {r.report()}"

    def test_reserved_names_rejected(self):
        for name in RESERVED_NAMES:
            r = validate(_good_identity(name=name))
            assert not r.ok
            assert "reserved" in r.report()

    def test_uppercase_rejected(self):
        r = validate(_good_identity(name="Aria"))
        assert not r.ok

    def test_starts_with_digit_rejected(self):
        r = validate(_good_identity(name="2cool"))
        assert not r.ok

    def test_too_short_rejected(self):
        r = validate(_good_identity(name="a"))
        assert not r.ok

    def test_too_long_rejected(self):
        r = validate(_good_identity(name="a" * 33))
        assert not r.ok

    def test_placeholder_rejected(self):
        r = validate(_good_identity(name="PLACEHOLDER"))
        assert not r.ok
        assert "PLACEHOLDER" in r.report()


class TestRole:
    def test_all_valid_roles(self):
        for role in VALID_ROLES:
            r = validate(_good_identity(role=role))
            assert r.ok, f"{role}: {r.report()}"

    def test_invalid_role(self):
        r = validate(_good_identity(role="god"))
        assert not r.ok


class TestFocus:
    def test_placeholder_rejected(self):
        r = validate(_good_identity(focus="PLACEHOLDER"))
        assert not r.ok

    def test_empty_focus_warns(self):
        r = validate(_good_identity(focus=""))
        assert r.ok  # warning, not error
        assert any("empty" in w for w in r.warnings)

    def test_very_long_focus_warns(self):
        r = validate(_good_identity(focus="x" * 250))
        assert r.ok
        assert any("under 200" in w for w in r.warnings)


class TestOptionalFields:
    def test_working_style_must_be_dict(self):
        r = validate(_good_identity(working_style="terse"))
        assert any("must be a dict" in w for w in r.warnings + r.errors)

    def test_anti_patterns_must_be_strings(self):
        r = validate(_good_identity(anti_patterns=[1, 2, 3]))
        assert not r.ok
        assert "anti_patterns" in r.report()

    def test_capabilities_must_be_strings(self):
        r = validate(_good_identity(capabilities=[{"x": 1}]))
        assert not r.ok

    def test_unknown_fields_warn(self):
        r = validate(_good_identity(quantum_flux="purple"))
        assert r.ok
        assert any("unknown field" in w for w in r.warnings)


class TestValidateFile:
    def test_missing_file(self, tmp_path):
        r = validate_file(str(tmp_path / "does_not_exist.json"))
        assert not r.ok
        assert "not found" in r.report()

    def test_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json")
        r = validate_file(str(bad))
        assert not r.ok
        assert "invalid JSON" in r.report()

    def test_valid_file(self, tmp_path):
        good = tmp_path / "good.json"
        good.write_text(json.dumps(_good_identity()))
        r = validate_file(str(good))
        assert r.ok


class TestValidationResultBehavior:
    def test_bool_is_ok(self):
        ok = ValidationResult(ok=True)
        bad = ValidationResult(ok=False, errors=["x"])
        assert bool(ok)
        assert not bool(bad)

    def test_report_no_issues(self):
        ok = ValidationResult(ok=True)
        assert "Valid" in ok.report()

    def test_report_with_errors(self):
        r = ValidationResult(ok=False, errors=["x missing"], warnings=["y odd"])
        report = r.report()
        assert "Errors:" in report
        assert "x missing" in report
        assert "Warnings:" in report
        assert "y odd" in report
