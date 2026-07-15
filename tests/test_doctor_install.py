"""Tests for `null doctor`'s install-integrity diagnostic.

Guards against the incident this feature exists for: two editable installs
of null_memory on one machine, with the live MCP server silently importing
the stale one, while doctor reported a clean install.
"""

import os
import sys

from tests.conftest import run_null


def test_package_exposes_real_version():
    import null_memory

    assert isinstance(null_memory.__version__, str)
    assert null_memory.__version__  # non-empty
    # Whatever the fallback path, it never silently becomes "?" / unset.
    assert null_memory.__version__ != "?"


def test_scan_returns_current_interpreter():
    from null_memory.cli import _scan_null_installs

    installs = _scan_null_installs()
    assert isinstance(installs, list)
    assert installs, "scan should find at least the running interpreter"
    # Every probe carries the fields doctor renders.
    for inst in installs:
        assert set(inst) >= {"python", "file", "version", "mcp"}
        assert inst["file"] and inst["version"]

    # The running interpreter's install must be among the results.
    import null_memory

    loaded = os.path.normcase(os.path.normpath(null_memory.__file__))
    files = {os.path.normcase(os.path.normpath(i["file"])) for i in installs}
    assert loaded in files


def test_probe_current_interpreter_succeeds():
    from null_memory.cli import _probe_interpreter

    probe = _probe_interpreter(sys.executable)
    assert probe is not None
    assert probe["python"] == sys.executable
    assert probe["file"] and probe["version"]


def test_probe_bad_interpreter_is_none():
    from null_memory.cli import _probe_interpreter

    # Nonexistent path must fail-soft to None, never raise.
    assert _probe_interpreter("does-not-exist-xyz-python") is None


def test_doctor_renders_install_integrity_section():
    rc, out, err = run_null("doctor")
    assert rc == 0, err
    assert "Install integrity:" in out
    assert "Running interpreter:" in out
    # The running interpreter path itself is reported.
    assert sys.executable in out
    assert "Loaded version:" in out
