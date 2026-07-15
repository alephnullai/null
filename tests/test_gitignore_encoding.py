"""Store .gitignore encoding — the cp1252 em dash that killed the exchange.

Older builds wrote the store .gitignore with the platform default encoding
(``open(path, "w")`` — cp1252 on Windows), so its header em dash landed as
byte 0x97. The exchange's strict-UTF-8 read of that file then crashed every
``ensure_clone``/``post`` on the seat (observed live: athena's first
exchange post, 2026-06-12). Writers now pin UTF-8; readers tolerate legacy
bytes.
"""

from __future__ import annotations

import os

from null_memory.agent import AgentMemory
from null_memory.exchange import ExchangeClient

# The exact bytes an older Windows build produced.
LEGACY_CP1252_GITIGNORE = b"# Null Memory \x97 transient files\r\n.lock\r\n"


def _seat_with_exchange(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    mem = AgentMemory(agent_dir=str(tmp_path))
    mem.config["exchange"] = {"url": str(tmp_path / "remote.git")}
    return mem


def test_legacy_cp1252_gitignore_does_not_kill_the_exchange(
        tmp_path, monkeypatch):
    mem = _seat_with_exchange(tmp_path, monkeypatch)
    (tmp_path / ".gitignore").write_bytes(LEGACY_CP1252_GITIGNORE)

    client = ExchangeClient(mem)
    # Must not raise UnicodeDecodeError — clone may still fail (no real
    # remote), but the gitignore step has to survive the legacy bytes.
    client._ensure_store_gitignore()

    text = (tmp_path / ".gitignore").read_text(encoding="utf-8",
                                               errors="replace")
    assert "exchange/" in text.splitlines()
    assert ".lock" in text


def test_new_seat_gitignore_is_utf8(tmp_path):
    from null_memory.persona_wizard import init_store_repo

    store = tmp_path / "seat"
    store.mkdir()
    bare = tmp_path / "remote.git"
    import subprocess
    subprocess.run(["git", "init", "--bare", str(bare)],
                   capture_output=True, check=True)
    init_store_repo(str(store), str(bare))

    raw = (store / ".gitignore").read_bytes()
    raw.decode("utf-8")  # must not raise
    assert b"\x97" not in raw


def test_memory_repo_gitignore_is_utf8(tmp_path, monkeypatch):
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    from null_memory.session import MemoryRepo

    repo = MemoryRepo(str(tmp_path))
    repo.init()
    gi = os.path.join(repo.repo_dir, ".gitignore")
    if os.path.isfile(gi):
        with open(gi, "rb") as f:
            raw = f.read()
        raw.decode("utf-8")  # must not raise
        assert b"\x97" not in raw
