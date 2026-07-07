"""Personality loader — Null's framework for user-defined managers.

Null ships ZERO specific personalities. Users define their own in
``~/.null/personalities/<name>/manager.py`` — a Python module that
subclasses ``null_memory.managers.Manager``. The loader discovers,
imports, and instantiates them on demand.

Directory convention:
    ~/.null/personalities/<name>/
        manager.py       # class <Name>(Manager): ...
        identity.json    # scope, reports_to, color, etc.
        preferences.json # user-editable preferences the manager reads
        test_manager.py  # optional — user-owned tests

``identity.json`` may declare a ``color`` field; Nebula's palette loader
picks it up automatically so user personalities render in their chosen hue.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from null_memory.managers.base import Manager

logger = logging.getLogger(__name__)

# Personality names must be lowercase alphanumeric + hyphen/underscore.
# Enforces a single-segment name so ``root / name / "manager.py"`` can
# never traverse outside the personalities directory via ``..`` / ``/``.
_VALID_NAME = re.compile(r"(?!(con|prn|aux|nul|com[1-9]|lpt[1-9])$)[a-z0-9][a-z0-9_-]{0,63}")

# Cache of loaded manager classes keyed by personality name. Keeps
# module-level code from re-running on every load_manager() call — a real
# cost in the MCP daemon where user managers may be invoked many times.
# Tests sandbox via NULL_DIR so cache entries from one test don't leak
# into another's discovery; see _cache_key().
_MANAGER_CACHE: dict[str, type[Manager]] = {}


def _cache_key(name: str) -> str:
    """Scope the module cache by the absolute personality file path so
    tests with different NULL_DIRs don't collide in the shared cache."""
    return str((personalities_dir() / name / "manager.py").resolve())


def _validate_name(name: str) -> None:
    """Reject personality names that could escape the personalities dir."""
    if not _VALID_NAME.fullmatch(name):
        raise InvalidPersonalityName(
            f"Invalid personality name {name!r}. Must match "
            f"[a-z0-9][a-z0-9_-]{{0,63}} (no slashes, no dots, "
            f"no uppercase, no Windows reserved names)."
        )


class InvalidPersonalityName(ValueError):
    """Raised when a personality name would escape the personalities dir
    or otherwise violates the naming contract."""


def personalities_dir() -> Path:
    """Resolve the personalities root. Honors NULL_DIR for sandboxing."""
    base = os.environ.get("NULL_DIR") or os.path.expanduser("~/.null")
    return Path(base) / "personalities"


def default_agent_dir() -> str:
    """Default store dir for the hub primary: NULL_DIR (or ~/.null), using
    the ``atlas/`` subdir on migrated hubs, flat layout otherwise.

    Companion to :func:`infer_personality` — the dir-resolution half of the
    shared resolver. MCP serve and create_server previously each carried a
    private copy of this atlas-fallback; every entry point must agree on
    where the default store lives, or hub setups split-brain (PR #37
    review)."""
    base = os.environ.get("NULL_DIR") or os.path.join(
        os.path.expanduser("~"), ".null")
    atlas_dir = os.path.join(base, "atlas")
    return atlas_dir if os.path.isdir(atlas_dir) else base


def infer_personality(agent_dir: str) -> str:
    """Personality a store path belongs to (shared by MCP serve, CLI,
    and the daemon — every entry point must agree, or a worker seat's
    writes get attributed to 'atlas').

    Conventions:
      ~/.null                           → atlas (legacy flat)
      ~/.null/atlas                     → atlas
      <hub>/personalities/<name>        → <name>

    Env var NULL_PERSONALITY overrides path inference.
    """
    override = os.environ.get("NULL_PERSONALITY", "").strip().lower()
    if override:
        return override
    try:
        real = os.path.realpath(agent_dir)
        parts = real.rstrip(os.sep).split(os.sep)
        if "personalities" in parts:
            idx = parts.index("personalities")
            if idx + 1 < len(parts):
                return parts[idx + 1].lower()
        if parts and parts[-1].lower() == "atlas":
            return "atlas"
    except Exception:
        pass
    return "atlas"


@dataclass
class PersonalityEntry:
    name: str
    dir: Path
    manager_path: Path
    identity: dict
    color: str | None = None


def list_personalities() -> list[PersonalityEntry]:
    """Discover personalities under ``personalities_dir()``.

    A personality is a directory containing ``manager.py``. Identity
    fields are read from ``identity.json`` if present — malformed or
    missing identity doesn't block discovery."""
    root = personalities_dir()
    if not root.is_dir():
        return []
    out: list[PersonalityEntry] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        manager_path = sub / "manager.py"
        if not manager_path.is_file():
            continue
        identity: dict = {}
        ident_path = sub / "identity.json"
        if ident_path.is_file():
            try:
                identity = json.loads(ident_path.read_text())
            except (json.JSONDecodeError, OSError):
                identity = {}
        out.append(PersonalityEntry(
            name=sub.name,
            dir=sub,
            manager_path=manager_path,
            identity=identity,
            color=identity.get("color"),
        ))
    return out


def load_manager(name: str, memory: Any, reasoner: Any | None = None) -> Manager:
    """Load and instantiate a user's Manager subclass by personality name.

    Imports ``~/.null/personalities/<name>/manager.py`` as a module,
    finds the first class subclassing ``Manager``, and returns an
    instance bound to the supplied memory + reasoner. Raises
    ``InvalidPersonalityName`` / ``PersonalityNotFound`` /
    ``ManagerNotInModule`` on failure. Modules are cached after first load
    so repeated calls don't re-execute user code."""
    _validate_name(name)
    root = personalities_dir()
    manager_path = root / name / "manager.py"
    if not manager_path.is_file():
        raise PersonalityNotFound(
            f"No manager.py at {manager_path}. Create one with a class "
            f"subclassing null_memory.managers.Manager."
        )

    cache_key = _cache_key(name)
    candidate = _MANAGER_CACHE.get(cache_key)
    if candidate is None:
        spec = importlib.util.spec_from_file_location(
            f"null_personality_{name}", manager_path,
        )
        if spec is None or spec.loader is None:
            raise PersonalityLoadError(
                f"Couldn't form module spec for {manager_path}"
            )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is Manager or obj.__name__ == "Manager":
                continue
            if issubclass(obj, Manager) and obj.__module__ == spec.name:
                candidate = obj
                break
        if candidate is None:
            raise ManagerNotInModule(
                f"No Manager subclass found in {manager_path}. Your module "
                f"must define e.g. `class Argus(Manager): ...`."
            )
        _MANAGER_CACHE[cache_key] = candidate
        logger.debug("loaded personality %r from %s", name, manager_path)

    if reasoner is None:
        return candidate(memory)
    return candidate(memory, reasoner)


def reload_manager(name: str) -> None:
    """Invalidate the cache entry for ``name`` so the next ``load_manager``
    re-executes the module. Use during development after editing a
    manager.py file."""
    _validate_name(name)
    _MANAGER_CACHE.pop(_cache_key(name), None)

    import sys
    prefix = f"null_personality_{name}"
    to_pop = [k for k in sys.modules if k == prefix or k.startswith(f"{prefix}.")]
    for k in to_pop:
        sys.modules.pop(k, None)


class PersonalityNotFound(FileNotFoundError):
    pass


class PersonalityLoadError(ImportError):
    pass


class ManagerNotInModule(AttributeError):
    pass


def palette_augments() -> dict[str, str]:
    """Return {personality_name: color_hex} for every personality that
    declared a color in its identity.json. Nebula merges this into its
    PERSONALITY_COLORS so user personalities render in their own hue."""
    out: dict[str, str] = {}
    for entry in list_personalities():
        if entry.color:
            out[entry.name.lower()] = entry.color
    return out


# ── Preferences helpers ────────────────────────────────────────────────
#
# Convention: each personality MAY have a preferences.json next to its
# manager.py. The Manager subclass decides what fields it expects; these
# helpers are pure JSON read/write wrappers so the framework can offer
# a uniform CLI without knowing any specific manager's schema.

def _preferences_path(name: str) -> Path:
    """Resolve preferences.json for a validated personality name."""
    _validate_name(name)
    return personalities_dir() / name / "preferences.json"


def read_preferences(name: str) -> dict[str, Any]:
    """Return the personality's preferences dict. Empty dict if the file
    doesn't exist or is malformed (Manager subclass can seed defaults)."""
    path = _preferences_path(name)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_preferences(name: str, prefs: dict[str, Any]) -> None:
    """Persist the preferences dict. Creates the personality dir if
    missing (so a fresh manager can be configured before first use)."""
    path = _preferences_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2))


def coerce_pref_value(current: Any, raw: str) -> Any:
    """Type-coerce a CLI string value to match the existing field type.

    Heuristics:
      - existing int → int
      - existing list → comma-split list of stripped strings
      - existing bool → 'true'/'1'/'yes' truthy
      - existing None / no current value:
          - 'null' / 'none' → None
          - all-digit string → int
          - otherwise → str
      - otherwise → str
    """
    if isinstance(current, bool):
        return raw.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(current, list):
        return [v.strip() for v in raw.split(",") if v.strip()]
    if current is None:
        rl = raw.strip().lower()
        if rl in {"null", "none", ""}:
            return None
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw
    return raw
