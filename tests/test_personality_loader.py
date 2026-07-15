"""Personality loader — framework tests.

Null ships zero specific managers. These tests verify the loader can
discover and run ANY user-defined Manager subclass dropped into
``~/.null/personalities/<name>/manager.py``.
"""

from __future__ import annotations

import os
import pytest

from null_memory.cli import _run_maybe_async
from null_memory.personality import (
    InvalidPersonalityName,
    ManagerNotInModule,
    PersonalityNotFound,
    list_personalities,
    load_manager,
    palette_augments,
)
from null_memory.managers import Manager, TickResult


MANAGER_SOURCE = '''
from null_memory.managers import Manager, TickResult


class Toy(Manager):
    name = "toy"
    scope = "Toy manager used by the loader tests."
    outreach_kind = "toy_match"

    async def tick(self, items=None):
        return TickResult(manager=self.name, observed_count=len(items or []))

    async def digest(self, since=None):
        return "Toy digest"
'''


@pytest.fixture
def toy_personality_dir(tmp_path, monkeypatch):
    """Sandbox a fake ~/.null/personalities with one toy manager."""
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    pdir = tmp_path / "personalities" / "toy"
    pdir.mkdir(parents=True)
    (pdir / "manager.py").write_text(MANAGER_SOURCE)
    (pdir / "identity.json").write_text(
        '{"name": "Toy", "color": "#ff00ff", "scope": "toy"}'
    )
    return pdir


def _fake_memory():
    """Minimal stand-in — we only verify the loader wires it through."""
    class _Mem:
        pass
    return _Mem()


# ── Discovery ────────────────────────────────────────────────────────────


def test_list_personalities_discovers_dropin(toy_personality_dir):
    entries = list_personalities()
    assert len(entries) == 1
    assert entries[0].name == "toy"
    assert entries[0].color == "#ff00ff"


def test_list_empty_when_no_personalities(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    assert list_personalities() == []


def test_list_skips_dirs_without_manager_py(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    (tmp_path / "personalities" / "bare").mkdir(parents=True)
    # No manager.py — should be skipped, not crash
    assert list_personalities() == []


# ── Loading + instantiation ──────────────────────────────────────────────


def test_load_manager_instantiates_subclass(toy_personality_dir):
    manager = load_manager("toy", _fake_memory())
    assert isinstance(manager, Manager)
    assert manager.name == "toy"


def test_load_manager_tick_is_callable(toy_personality_dir):
    manager = load_manager("toy", _fake_memory())
    result = _run_maybe_async(manager.tick(items=[{"x": 1}, {"x": 2}]))
    assert isinstance(result, TickResult)
    assert result.observed_count == 2


def test_load_manager_digest_is_callable(toy_personality_dir):
    manager = load_manager("toy", _fake_memory())
    assert _run_maybe_async(manager.digest()) == "Toy digest"


def test_load_manager_raises_personality_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    with pytest.raises(PersonalityNotFound):
        load_manager("missing", _fake_memory())


def test_load_manager_raises_when_no_manager_subclass(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    pdir = tmp_path / "personalities" / "empty"
    pdir.mkdir(parents=True)
    (pdir / "manager.py").write_text("# no Manager subclass here\n")
    with pytest.raises(ManagerNotInModule):
        load_manager("empty", _fake_memory())


# ── Palette augments ─────────────────────────────────────────────────────


def test_palette_augments_picks_up_identity_color(toy_personality_dir):
    palette = palette_augments()
    assert palette == {"toy": "#ff00ff"}


def test_palette_augments_ignores_personality_without_color(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    pdir = tmp_path / "personalities" / "grey"
    pdir.mkdir(parents=True)
    (pdir / "manager.py").write_text(MANAGER_SOURCE)
    (pdir / "identity.json").write_text('{"name": "Grey"}')   # no color
    assert palette_augments() == {}


# ── Phase 7-review: name validation + cache ──────────────────────────────


def test_invalid_name_traversal_is_rejected(tmp_path, monkeypatch):
    from null_memory.personality import InvalidPersonalityName, load_manager
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    # Classic path traversal
    with pytest.raises(InvalidPersonalityName):
        load_manager("../../../etc/passwd", _fake_memory())


def test_invalid_name_absolute_path_is_rejected(tmp_path, monkeypatch):
    from null_memory.personality import InvalidPersonalityName, load_manager
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    with pytest.raises(InvalidPersonalityName):
        load_manager("/absolute/path", _fake_memory())


def test_invalid_name_uppercase_is_rejected(tmp_path, monkeypatch):
    from null_memory.personality import load_manager
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    with pytest.raises(InvalidPersonalityName):
        load_manager("Argus", _fake_memory())


@pytest.mark.parametrize("name", ["nul", "con", "com1", "lpt1", "abc\n"])
def test_invalid_reserved_or_multiline_name_is_rejected(tmp_path, monkeypatch, name):
    from null_memory.personality import load_manager
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    with pytest.raises(InvalidPersonalityName):
        load_manager(name, _fake_memory())


def test_valid_name_with_underscore_and_hyphen_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    for name in ("my-bot", "my_bot", "bot1", "bot_1-2"):
        pdir = tmp_path / "personalities" / name
        pdir.mkdir(parents=True)
        (pdir / "manager.py").write_text(MANAGER_SOURCE)
    from null_memory.personality import list_personalities, load_manager
    entries = {e.name for e in list_personalities()}
    assert {"my-bot", "my_bot", "bot1", "bot_1-2"}.issubset(entries)
    for name in ("my-bot", "my_bot"):
        m = load_manager(name, _fake_memory())
        assert m.name == "toy"


def test_manager_cache_avoids_re_execution(toy_personality_dir, monkeypatch):
    """Second load_manager call for the same personality uses the cache
    instead of re-executing manager.py module-level code."""
    from null_memory.personality import _MANAGER_CACHE, load_manager, reload_manager
    # Ensure cache is empty for this personality
    reload_manager("toy")
    cls1 = type(load_manager("toy", _fake_memory()))
    cls2 = type(load_manager("toy", _fake_memory()))
    assert cls1 is cls2  # same class object — cache hit


def test_reload_manager_invalidates_cache(toy_personality_dir):
    from null_memory.personality import load_manager, reload_manager, _MANAGER_CACHE
    reload_manager("toy")
    _ = load_manager("toy", _fake_memory())
    assert len(_MANAGER_CACHE) >= 1
    reload_manager("toy")
    # After reload, the cache entry for this path is gone; re-loading
    # works again without error.
    _ = load_manager("toy", _fake_memory())


def test_reload_manager_validates_name(tmp_path, monkeypatch):
    from null_memory.personality import reload_manager
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    with pytest.raises(InvalidPersonalityName):
        reload_manager("../escape")


def test_preferences_show_and_set_round_trip(toy_personality_dir, tmp_path):
    """Generic prefs CLI helpers — read, write, type-coerce."""
    from null_memory.personality import (
        read_preferences, write_preferences, coerce_pref_value,
    )
    # Empty when no preferences.json exists
    assert read_preferences("toy") == {}

    # Round-trip: write, read back
    write_preferences("toy", {"comp_floor_usd": 180000, "tags": ["a", "b"]})
    got = read_preferences("toy")
    assert got["comp_floor_usd"] == 180000
    assert got["tags"] == ["a", "b"]


def test_coerce_pref_value_typing():
    from null_memory.personality import coerce_pref_value
    # int field stays int
    assert coerce_pref_value(100, "200") == 200
    # list field splits comma
    assert coerce_pref_value(["a"], "x, y, z") == ["x", "y", "z"]
    # bool field
    assert coerce_pref_value(True, "false") is False
    assert coerce_pref_value(True, "yes") is True
    # None field with all-digit string → int
    assert coerce_pref_value(None, "180000") == 180000
    # None field with 'null' → None
    assert coerce_pref_value(None, "null") is None
    # None field with arbitrary string → str
    assert coerce_pref_value(None, "remote_only") == "remote_only"


def test_preferences_invalid_name_rejected(tmp_path, monkeypatch):
    from null_memory.personality import (
        InvalidPersonalityName, read_preferences, write_preferences,
    )
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    with pytest.raises(InvalidPersonalityName):
        read_preferences("../escape")
    with pytest.raises(InvalidPersonalityName):
        write_preferences("../escape", {})
