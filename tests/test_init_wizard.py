"""First-run identity wizard (`null init`).

Locks the two things that matter: it NEVER hangs on a non-interactive
install (CI, a seat, a pipe), and interactively it sets the agent's own
name + traits — never 'atlas' (Pete's reserved persona).
"""

from __future__ import annotations

import builtins
import sys
from unittest import mock

import pytest

from null_memory.agent import AgentMemory
from null_memory.cli import _run_init_wizard


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("NULL_NONINTERACTIVE", raising=False)
    return tmp_path


def _run_interactive(answers):
    it = iter(answers)
    with mock.patch.object(sys.stdin, "isatty", return_value=True), \
         mock.patch.object(builtins, "input", lambda *a: next(it)):
        _run_init_wizard(no_input=False)


class TestNonInteractiveNeverHangs:
    def test_no_input_persists_a_stable_name(self, store, capsys):
        # A fresh store's random name is only in memory; --no-input must
        # persist it so it stops changing on every load.
        _run_init_wizard(no_input=True)  # must return without any input()
        assert "Non-interactive" in capsys.readouterr().out
        name1 = AgentMemory.load().identity.get("name")
        name2 = AgentMemory.load().identity.get("name")
        assert name1 and name1 == name2  # stable across reloads
        assert name1.lower() != "atlas"

    def test_non_tty_is_non_interactive(self, store):
        # stdin is not a tty under pytest — must not call input()
        with mock.patch.object(builtins, "input",
                               side_effect=AssertionError("prompted in non-tty")):
            _run_init_wizard(no_input=False)

    def test_ci_env_forces_non_interactive(self, store, monkeypatch):
        monkeypatch.setenv("CI", "1")
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch.object(builtins, "input",
                               side_effect=AssertionError("prompted in CI")):
            _run_init_wizard(no_input=False)


class TestInteractive:
    def test_sets_name_and_traits(self, store):
        _run_interactive(["Sage", "push back, cite sources", "no filler"])
        ident = AgentMemory.load().identity
        assert ident["name"] == "Sage"
        assert ident["capabilities"] == ["push back", "cite sources"]
        assert ident["anti_patterns"] == ["no filler"]

    def test_blank_name_takes_the_suggested_default(self, store):
        _run_interactive(["", "", ""])
        name = AgentMemory.load().identity["name"]
        assert name and name.lower() != "atlas"

    def test_atlas_is_reserved_and_reprompted(self, store):
        # 'atlas' (any case) is rejected until a real name is given
        _run_interactive(["atlas", "ATLAS", "Vega", "", ""])
        assert AgentMemory.load().identity["name"] == "Vega"

    def test_traits_are_optional(self, store):
        _run_interactive(["Nova", "", ""])
        ident = AgentMemory.load().identity
        assert ident["name"] == "Nova"
        # empty answers don't create bogus trait lists
        assert not ident.get("capabilities")
        assert not ident.get("anti_patterns")
