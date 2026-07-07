"""Scoped export — the onboarding-packet v0 (ORG_TOPOLOGY 'Replicate vs. query').

Covers: project/kinds/since filters, identity exclusion by default,
code-word never leaking without the override (+ loud CLI warning when it
does), round-trip through `null import`, unscoped export behaving exactly
as before, and --dry-run counts.
"""

import json
import os

import pytest

from null_memory.agent import AgentMemory
from null_memory.migrate_v3 import init_unified_db
from tests.conftest import run_null

# Keys of the pre-scoped-export wire format. The bare `null export` must
# keep exactly this shape (full-backup semantics, zero behavior change).
LEGACY_EXPORT_KEYS = {
    "version", "exported_at", "identity", "knowledge",
    "decisions", "mistakes", "reflections", "projects",
}


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Two-project fixture on a unified DB (anchors require it):
    facts/decisions/mistakes/reflections in alpha + beta, one global
    fact, one joy-anchored alpha fact, one code-word fact in alpha."""
    init_unified_db(str(tmp_path / "unified.db")).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    m = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert m.db.unified
    m._embeddings = False  # lexical-only — deterministic in CI

    m.learn("alpha uses postgres for the orders store", project="alpha")
    m.learn("alpha deploys via blue-green rollout", project="alpha")
    m.learn("beta frontend is react with vite", project="beta")
    m.learn("personal global fact about morning preferences", project="global")

    # Identity content inside the alpha scope — must NOT leak by default.
    cw = m.learn("identity verification phrase for continuity checks",
                 project="alpha")
    m.db.conn.execute(
        "UPDATE facts SET anchor_type = 'code_word' WHERE id = ?", (cw["id"],))
    m.db.conn.commit()
    joy = m.learn("a joyful alpha milestone worth anchoring", project="alpha")
    assert m.anchor(joy["id"], "joy", "test anchor")

    m.decide("alpha targets sqlite first", "simpler ops", project="alpha")
    m.decide("beta ships weekly", "cadence", project="beta")
    m.mistake("alpha migration ran twice", "missing idempotency guard",
              project="alpha")
    m.mistake("beta cache never invalidated", "no TTL", project="beta")
    m.reflect("alpha sprint went well", "missed the edge case",
              "write the test first", project="alpha")

    m._code_word_id = cw["id"]
    m._joy_id = joy["id"]
    return m


class TestScopedExportFilters:
    def test_project_filter_honored(self, mem):
        data = mem.export_scoped(projects=["alpha"])
        assert data["knowledge"], "alpha facts expected"
        assert all(e["project"] == "alpha" for e in data["knowledge"])
        assert all(e["project"] == "alpha" for e in data["decisions"])
        assert all(e["project"] == "alpha" for e in data["mistakes"])
        assert all(e["project"] == "alpha" for e in data["reflections"])
        texts = " ".join(e["fact"] for e in data["knowledge"])
        assert "beta" not in texts
        assert "global fact" not in texts

    def test_multiple_projects(self, mem):
        data = mem.export_scoped(projects=["alpha", "beta"])
        projects = {e["project"] for e in data["knowledge"]}
        assert projects == {"alpha", "beta"}
        assert "global" not in {e["project"] for e in data["decisions"]}

    def test_kinds_filter(self, mem):
        data = mem.export_scoped(projects=["alpha"], kinds=["fact", "decision"])
        assert data["knowledge"]
        assert data["decisions"]
        assert data["mistakes"] == []
        assert data["reflections"] == []
        # Plurals accepted as aliases
        data2 = mem.export_scoped(projects=["alpha"], kinds=["facts", "decisions"])
        assert len(data2["knowledge"]) == len(data["knowledge"])

    def test_unknown_kind_rejected(self, mem):
        with pytest.raises(ValueError, match="unknown kind"):
            mem.export_scoped(kinds=["fact", "vibes"])

    def test_since_filter(self, mem):
        none_left = mem.export_scoped(projects=["alpha"],
                                      since="2999-01-01T00:00:00+00:00")
        assert none_left["knowledge"] == []
        assert none_left["decisions"] == []
        recent = mem.export_scoped(projects=["alpha"], since="1d")
        assert recent["knowledge"]

    def test_packet_metadata(self, mem):
        data = mem.export_scoped(projects=["alpha"], kinds=["fact"])
        packet = data["packet"]
        assert packet["generated_at"]
        assert packet["source_personality"] == "atlas"
        assert packet["filters"]["projects"] == ["alpha"]
        assert packet["filters"]["kinds"] == ["fact"]
        assert packet["filters"]["include_identity"] is False
        # Wire format unchanged otherwise — import reads only known keys
        assert LEGACY_EXPORT_KEYS <= set(data.keys())


class TestIdentityExclusion:
    def test_identity_excluded_by_default(self, mem):
        data = mem.export_scoped(projects=["alpha"])
        assert data["identity"] == {}
        assert not any(e.get("anchor_type") for e in data["knowledge"])
        ids = {e["id"] for e in data["knowledge"]}
        assert mem._joy_id not in ids

    def test_code_word_never_leaks_without_override(self, mem):
        # Even though the code-word fact is project-scoped to alpha
        data = mem.export_scoped(projects=["alpha"])
        ids = {e["id"] for e in data["knowledge"]}
        assert mem._code_word_id not in ids
        assert data["packet"]["code_word_count"] == 0
        # The legacy text-pattern form must not leak either
        mem.learn("the code word: zephyr-nine", project="alpha")
        data2 = mem.export_scoped(projects=["alpha"])
        assert not any("code word:" in e["fact"].lower()
                       for e in data2["knowledge"])

    def test_include_identity_override(self, mem):
        data = mem.export_scoped(projects=["alpha"], include_identity=True)
        ids = {e["id"] for e in data["knowledge"]}
        assert mem._code_word_id in ids
        assert mem._joy_id in ids
        assert data["identity"] == mem.identity
        assert data["packet"]["code_word_count"] == 1


class TestScopedRoundtrip:
    def test_roundtrip_through_import(self, mem, tmp_path, monkeypatch):
        data = mem.export_scoped(projects=["alpha"])
        n_facts = len(data["knowledge"])
        n_decisions = len(data["decisions"])
        assert n_facts and n_decisions

        # Fresh NULL_DIR so the spoke doesn't share the hub's unified DB
        spoke_base = tmp_path / "spoke_base"
        spoke_base.mkdir()
        monkeypatch.setenv("NULL_DIR", str(spoke_base))
        spoke = AgentMemory.import_from(data, str(spoke_base / "spoke"))
        assert len(spoke.db.get_all_facts()) == n_facts
        assert len(spoke.db.get_decisions()) == n_decisions
        assert len(spoke.db.get_mistakes()) == len(data["mistakes"])
        assert len(spoke.db.get_reflections()) == len(data["reflections"])
        # Zero identity content arrived on the spoke
        assert mem._code_word_id not in {
            f["id"] for f in spoke.db.get_all_facts()}


class TestUnscopedUnchanged:
    def test_export_all_shape_unchanged(self, mem):
        data = mem.export_all()
        assert set(data.keys()) == LEGACY_EXPORT_KEYS
        assert "packet" not in data
        # Full backup: identity and ALL projects/anchors included
        assert data["identity"] == mem.identity
        ids = {e["id"] for e in data["knowledge"]}
        assert mem._code_word_id in ids
        assert mem._joy_id in ids


class TestCLIScopedExport:
    def _seed(self, tmp_path):
        # Flat store at NULL_DIR (no atlas/ subdir) — what the CLI loads
        m = AgentMemory.load(agent_dir=str(tmp_path))
        m._embeddings = False
        m.learn("alpha service speaks grpc internally", project="alpha")
        m.learn("beta queue drains nightly", project="beta")
        m.learn("the code word: zephyr-nine", project="alpha")
        m.decide("alpha pins python 3.11", "wheel availability",
                 project="alpha")
        return m

    def test_cli_unscoped_export_unchanged(self, tmp_path):
        self._seed(tmp_path)
        rc, out, _ = run_null("export")
        assert rc == 0
        data = json.loads(out)
        assert set(data.keys()) == LEGACY_EXPORT_KEYS
        assert "packet" not in data

    def test_cli_scoped_export_filters(self, tmp_path):
        self._seed(tmp_path)
        rc, out, _ = run_null("export", "--project", "alpha",
                              "--kinds", "fact,decision")
        assert rc == 0
        data = json.loads(out)
        assert data["packet"]["filters"]["projects"] == ["alpha"]
        assert all(e["project"] == "alpha" for e in data["knowledge"])
        assert data["mistakes"] == [] and data["reflections"] == []
        # identity excluded by default for scoped exports
        assert data["identity"] == {}
        assert not any("code word:" in e["fact"].lower()
                       for e in data["knowledge"])

    def test_cli_code_word_warning_on_override(self, tmp_path):
        self._seed(tmp_path)
        rc, out, err = run_null("export", "--project", "alpha",
                                "--include-identity")
        assert rc == 0
        data = json.loads(out)
        assert any("code word:" in e["fact"].lower()
                   for e in data["knowledge"])
        assert "WARNING" in err and "CODE WORD" in err

    def test_cli_no_identity_and_include_identity_conflict(self, tmp_path):
        self._seed(tmp_path)
        rc, _, err = run_null("export", "--no-identity", "--include-identity")
        assert rc != 0

    def test_cli_dry_run_counts_and_writes_nothing(self, tmp_path):
        self._seed(tmp_path)
        outfile = tmp_path / "packet.json"
        rc, out, _ = run_null("export", "--project", "alpha",
                              "--dry-run", "-o", str(outfile))
        assert rc == 0
        assert "Dry run" in out
        assert "facts: 1" in out and "alpha=1" in out
        assert "decisions: 1" in out
        assert "identity: excluded" in out
        assert not outfile.exists()

    def test_cli_roundtrip_import(self, tmp_path):
        self._seed(tmp_path)
        packet = tmp_path / "packet.json"
        rc, _, _ = run_null("export", "--project", "alpha", "-o", str(packet))
        assert rc == 0
        spoke_dir = tmp_path / "spoke_null"
        spoke_dir.mkdir()
        rc, out, _ = run_null("import", str(packet), tmp_path=spoke_dir)
        assert rc == 0
        assert "Imported: 1 new facts (0 already present)" in out
