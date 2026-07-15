"""Schema validation for persona identity.json files.

A persona's identity.json drives every interaction. A malformed one breaks
boot, identity injection, and briefing. This module validates structure
before the file lands on disk and on every load.

Validation is intentionally permissive: required fields are tight, optional
fields are wide-open. The goal is to catch obvious breakage (missing name,
wrong tier value), not enforce style.

Public API:
    validate(identity: dict) -> ValidationResult
    validate_file(path: str) -> ValidationResult
    REQUIRED_FIELDS — the canonical list (also used by wizard)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── Constants ──

REQUIRED_FIELDS: tuple[str, ...] = (
    "version",
    "name",
    "role",
    "focus",
    "description",
)

OPTIONAL_FIELDS: tuple[str, ...] = (
    "template_id",
    "template_version",
    "working_style",
    "user_preferences",
    "capabilities",
    "anti_patterns",
    "session_lifecycle",
    "code_word",
    "who_i_am",
    "who_pete_is",
    "products_i_built",
    "hard_won_lessons",
    "created_at",
    "updated_at",
    "bootstrapped_from",
)

VALID_ROLES: tuple[str, ...] = ("atlas", "manager", "worker", "executor")

# Names must be filesystem-safe and CLI-safe.
# Allow lowercase letters, digits, hyphen, underscore. 2-32 chars.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

# Reserved names — already used by built-in personas or system.
RESERVED_NAMES: frozenset[str] = frozenset({
    "atlas",      # Pete's persona (showcase, never copied)
    "system",
    "null",
    "aleph",
    "default",
    "test",
    "_template",
})


# ── Result type ──

@dataclass
class ValidationResult:
    """Result of validating a persona identity dict."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    def report(self) -> str:
        lines: list[str] = []
        if self.errors:
            lines.append("Errors:")
            for e in self.errors:
                lines.append(f"  ✗ {e}")
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  ! {w}")
        if not lines:
            return "Valid persona identity."
        return "\n".join(lines)


# ── Validators ──

def _check_name(name: Any) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(name, str):
        errors.append("name must be a string")
        return errors, warnings
    if name in RESERVED_NAMES:
        errors.append(f"name '{name}' is reserved")
    if not NAME_PATTERN.match(name):
        errors.append(
            "name must be lowercase, start with a letter, "
            "2-32 chars, only [a-z0-9_-]"
        )
    if name == "PLACEHOLDER":
        errors.append("name is still PLACEHOLDER — set it before use")
    return errors, warnings


def _check_role(role: Any) -> list[str]:
    if not isinstance(role, str):
        return ["role must be a string"]
    if role not in VALID_ROLES:
        return [f"role must be one of {VALID_ROLES}, got {role!r}"]
    return []


def _check_focus(focus: Any) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(focus, str):
        errors.append("focus must be a string")
        return errors, warnings
    if focus == "PLACEHOLDER":
        errors.append("focus is still PLACEHOLDER — set it before use")
    if not focus.strip():
        warnings.append("focus is empty — persona will have no scope")
    elif len(focus) > 200:
        warnings.append(f"focus is {len(focus)} chars — keep it under 200 for clarity")
    return errors, warnings


def _check_working_style(ws: Any) -> list[str]:
    warnings: list[str] = []
    if ws is None:
        return ["working_style is empty — add at least pace, pushback, communication"]
    if not isinstance(ws, dict):
        return ["working_style must be a dict"]
    if not ws:
        warnings.append("working_style is empty — add at least pace, pushback, communication")
    return warnings


def _check_anti_patterns(ap: Any) -> list[str]:
    warnings: list[str] = []
    if ap is None:
        return warnings
    if not isinstance(ap, list):
        return ["anti_patterns must be a list of strings"]
    for i, item in enumerate(ap):
        if not isinstance(item, str):
            return [f"anti_patterns[{i}] is not a string"]
    return warnings


def _check_capabilities(caps: Any) -> list[str]:
    warnings: list[str] = []
    if caps is None:
        return warnings
    if not isinstance(caps, list):
        return ["capabilities must be a list of strings"]
    for i, item in enumerate(caps):
        if not isinstance(item, str):
            return [f"capabilities[{i}] is not a string"]
    return warnings


def validate(identity: Any) -> ValidationResult:
    """Validate a persona identity dict.

    Returns a ValidationResult with ok=True if no errors, plus any warnings.
    Warnings don't block — they're surfaced to the user but the persona loads.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(identity, dict):
        return ValidationResult(ok=False, errors=["identity must be a JSON object"])

    # Required fields presence
    for field_name in REQUIRED_FIELDS:
        if field_name not in identity:
            errors.append(f"missing required field: {field_name!r}")

    # If required fields are missing, no point validating their values
    if errors:
        return ValidationResult(ok=False, errors=errors)

    # Field-specific validation
    name_errs, name_warns = _check_name(identity.get("name"))
    errors.extend(name_errs)
    warnings.extend(name_warns)

    errors.extend(_check_role(identity.get("role")))

    focus_errs, focus_warns = _check_focus(identity.get("focus"))
    errors.extend(focus_errs)
    warnings.extend(focus_warns)

    desc = identity.get("description")
    if not isinstance(desc, str) or not desc.strip():
        warnings.append("description is empty — explain what this persona does")

    # Optional but typed fields
    if "working_style" in identity:
        ws = identity["working_style"]
        if ws is not None and not isinstance(ws, dict):
            errors.append("working_style must be a dict")
        elif not ws:
            warnings.append("working_style is empty — add at least pace, pushback, communication")

    # anti_patterns: list of strings only (type violations = errors)
    if "anti_patterns" in identity:
        ap = identity["anti_patterns"]
        if ap is not None and not isinstance(ap, list):
            errors.append("anti_patterns must be a list of strings")
        elif isinstance(ap, list):
            for i, item in enumerate(ap):
                if not isinstance(item, str):
                    errors.append(f"anti_patterns[{i}] is not a string")
                    break

    # capabilities: list of strings only (type violations = errors)
    if "capabilities" in identity:
        caps = identity["capabilities"]
        if caps is not None and not isinstance(caps, list):
            errors.append("capabilities must be a list of strings")
        elif isinstance(caps, list):
            for i, item in enumerate(caps):
                if not isinstance(item, str):
                    errors.append(f"capabilities[{i}] is not a string")
                    break

    # Version sanity
    version = identity.get("version")
    if not isinstance(version, str):
        warnings.append(f"version should be a string, got {type(version).__name__}")

    # Unknown fields = warning, not error (forward compatibility)
    allowed = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
    unknown = [k for k in identity if k not in allowed]
    for k in unknown:
        warnings.append(f"unknown field {k!r} (allowed but won't be used)")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


def validate_file(path: str) -> ValidationResult:
    """Validate a persona identity.json file at the given path."""
    try:
        with open(path) as f:
            identity = json.load(f)
    except FileNotFoundError:
        return ValidationResult(ok=False, errors=[f"file not found: {path}"])
    except json.JSONDecodeError as e:
        return ValidationResult(ok=False, errors=[f"invalid JSON: {e}"])
    except OSError as e:
        return ValidationResult(ok=False, errors=[f"could not read {path}: {e}"])

    return validate(identity)
