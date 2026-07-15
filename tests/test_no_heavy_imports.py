"""Nebula quarantine: the product surface must never import the
experimental visualization stack.

A base `pip install null-memory[embeddings]` has no umap/hdbscan/numba/
fastapi — so if importing the CLI, the agent, or the MCP server pulls
any of them in, a customer's install breaks (or drags in numba's fragile
wheels). This test is the enforcement for the lazy-import rule: heavy
deps load only inside nebula code paths, on use.
"""

from __future__ import annotations

import subprocess
import sys

# The fragile nebula-only stack. NOT listed: uvicorn (a dependency of
# the mcp SDK itself, so always present on base installs) and the
# null_memory.nebula package modules (viz.py imports the projector
# MODULE deliberately, fail-soft, stdlib-only at import time — the
# documented lazy boundary).
_FORBIDDEN = ("umap", "hdbscan", "numba", "fastapi")

_PROBE = """
import sys
import null_memory.cli
import null_memory.agent
import null_memory.mcp.server
import null_memory.memory.viz  # the lazy-import boundary itself
loaded = [m for m in {forbidden!r} if m.split(".")[0] in
          {{n.split(".")[0] for n in sys.modules}}]
print("LOADED:" + ",".join(loaded))
"""


def test_product_surface_imports_no_nebula_stack():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(forbidden=_FORBIDDEN)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"product import failed:\n{result.stderr}"
    )
    marker = [l for l in result.stdout.splitlines() if l.startswith("LOADED:")]
    assert marker, f"probe produced no marker:\n{result.stdout}\n{result.stderr}"
    loaded = marker[0][len("LOADED:"):]
    assert loaded == "", (
        f"importing the product surface loaded experimental modules: {loaded}. "
        "Nebula and its heavy deps must only be imported lazily, on use."
    )
