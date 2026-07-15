"""Import dedup across ALL entity kinds (issue #26).

Live incident (2026-06-11, first real two-store content merge): importing
the Windows store export into the Mac store deduped facts correctly
(2,809 incoming → +55 new) but blindly re-inserted decisions (213→424),
mistakes (46→92), and reflections (115→230). Repair was manual SQL + git
history restore.

Under test:
  * exports stamp decisions/mistakes/reflections with a stable
    content-hash "uid" (facts already carry content-hash ids);
  * import skips rows whose id/uid already exists, per kind;
  * per-kind imported/skipped counts are reported;
  * round-trip compatibility — legacy export files WITHOUT uid keys
    dedup via the content-hash fallback (same grouping keys as the live
    repair);
  * regression: the incident shape (two stores, one a stale superset of
    the other) merges in both directions with stable counts.
"""

from __future__ import annotations

import json
import os

import pytest

from null_memory.agent import AgentMemory


# Facts must be semantically DISTINCT: learn() runs embedding-based
# near-duplicate detection, so "fact number 0/1/2 about X" would collapse
# to a single fact and break the count assertions.
_FACTS = [
    "the build system pins CMake 3.28 via presets",
    "production deploys roll out blue-green behind the load balancer",
    "the ingest pipeline uses Kafka with twelve partitions",
    "auth tokens rotate every forty-five minutes via the vault sidecar",
    "grafana dashboards are versioned in the observability repo",
]


def _make_store(path, n_facts=3, n_decisions=2, n_mistakes=2,
                n_reflections=2, tag="a"):
    mem = AgentMemory.load(str(path))
    for i in range(n_facts):
        mem.learn(f"[{tag}] {_FACTS[i]}", confidence=0.9)
    for i in range(n_decisions):
        mem.decide(f"[{tag}] decision {i}", f"reasoning {i}")
    for i in range(n_mistakes):
        mem.mistake(f"[{tag}] mistake {i}", f"why {i}")
    for i in range(n_reflections):
        mem.reflect(f"[{tag}] went well {i}", f"missed {i}",
                    f"do differently {i}")
    return mem


def _counts(mem):
    return {
        "facts": mem.db.count_facts(active_only=False),
        "decisions": mem.db.count_decisions(),
        "mistakes": mem.db.count_mistakes(),
        "reflections": mem.db.count_reflections(),
    }


class TestExportCarriesStableIds:
    def test_decisions_mistakes_reflections_have_uids(self, tmp_path):
        mem = _make_store(tmp_path / "src")
        data = mem.export_all()
        for kind in ("decisions", "mistakes", "reflections"):
            assert data[kind], f"fixture must produce {kind}"
            for row in data[kind]:
                assert row.get("uid"), f"{kind} row missing stable uid"

    def test_uid_is_stable_across_exports(self, tmp_path):
        mem = _make_store(tmp_path / "src")
        first = mem.export_all()
        second = mem.export_all()
        for kind in ("decisions", "mistakes", "reflections"):
            assert [r["uid"] for r in first[kind]] == \
                [r["uid"] for r in second[kind]]

    def test_scoped_export_also_stamps_uids(self, tmp_path):
        mem = _make_store(tmp_path / "src")
        data = mem.export_scoped(kinds=["decision", "mistake", "reflection"])
        for kind in ("decisions", "mistakes", "reflections"):
            for row in data[kind]:
                assert row.get("uid")


class TestImportDedupPerKind:
    def test_second_import_of_same_export_is_all_skipped(self, tmp_path):
        src = _make_store(tmp_path / "src")
        data = src.export_all()
        target_dir = str(tmp_path / "target")

        first = AgentMemory.import_from(data, target_dir)
        c1 = first.last_import_counts
        assert c1["knowledge"]["imported"] == 3
        assert c1["decisions"]["imported"] == 2
        assert c1["mistakes"]["imported"] == 2
        assert c1["reflections"]["imported"] == 2
        first.db.close()

        second = AgentMemory.import_from(data, target_dir)
        c2 = second.last_import_counts
        for kind in ("knowledge", "decisions", "mistakes", "reflections"):
            assert c2[kind]["imported"] == 0, f"{kind} re-imported"
        assert c2["knowledge"]["skipped"] == 3
        assert c2["decisions"]["skipped"] == 2
        assert c2["mistakes"]["skipped"] == 2
        assert c2["reflections"]["skipped"] == 2

        assert _counts(second) == {
            "facts": 3, "decisions": 2, "mistakes": 2, "reflections": 2}
        second.db.close()

    def test_new_rows_still_import_alongside_skips(self, tmp_path):
        src = _make_store(tmp_path / "src")
        target_dir = str(tmp_path / "target")
        mem = AgentMemory.import_from(src.export_all(), target_dir)
        mem.db.close()

        # source grows by one of each kind
        src.learn("the licensing server runs an air-gapped FlexLM mirror",
                  confidence=0.8)
        src.decide("new decision", "new reasoning")
        src.mistake("new mistake", "new why")
        src.reflect("new went well", "new missed", "new differently")

        mem = AgentMemory.import_from(src.export_all(), target_dir)
        c = mem.last_import_counts
        assert c["knowledge"] == {"imported": 1, "skipped": 3}
        assert c["decisions"] == {"imported": 1, "skipped": 2}
        assert c["mistakes"] == {"imported": 1, "skipped": 2}
        assert c["reflections"] == {"imported": 1, "skipped": 2}
        mem.db.close()

    def test_legacy_export_without_uids_dedups_by_content_hash(self, tmp_path):
        """Round-trip compatibility: an OLD export file (no uid keys)
        must still dedup on re-import via the content-hash fallback."""
        src = _make_store(tmp_path / "src")
        data = src.export_all()
        for kind in ("decisions", "mistakes", "reflections"):
            for row in data[kind]:
                row.pop("uid", None)

        target_dir = str(tmp_path / "target")
        first = AgentMemory.import_from(data, target_dir)
        assert first.last_import_counts["decisions"]["imported"] == 2
        first.db.close()

        second = AgentMemory.import_from(data, target_dir)
        c = second.last_import_counts
        for kind in ("knowledge", "decisions", "mistakes", "reflections"):
            assert c[kind]["imported"] == 0, \
                f"legacy {kind} rows duplicated on re-import"
        second.db.close()

    def test_report_format(self, tmp_path):
        src = _make_store(tmp_path / "src")
        target_dir = str(tmp_path / "target")
        mem = AgentMemory.import_from(src.export_all(), target_dir)
        mem.db.close()
        mem = AgentMemory.import_from(src.export_all(), target_dir)
        report = AgentMemory.format_import_report(mem.last_import_counts)
        assert report == (
            "Imported: 0 new facts (3 already present), "
            "0 new decisions (2 already present), "
            "0 new mistakes (2 already present), "
            "0 new reflections (2 already present)"
        )
        mem.db.close()


class TestIncidentRegression:
    def test_stale_superset_merge_both_directions_counts_stable(self, tmp_path):
        """The live incident shape: store B is a stale snapshot of store
        A (a strict subset); A kept growing. Merging in BOTH directions —
        repeatedly — must converge with stable counts, never duplicate
        decisions/mistakes/reflections (the 213→424 failure)."""
        a = _make_store(tmp_path / "a", n_facts=4, n_decisions=3,
                        n_mistakes=2, n_reflections=2)

        # B = stale snapshot of A
        b = AgentMemory.import_from(a.export_all(), str(tmp_path / "b"))
        b.db.close()

        # A grows after the snapshot — B is now a stale subset
        a.learn("the firmware signing key lives in the HSM enclave",
                confidence=0.9)
        a.decide("post-snapshot decision", "because")
        a.mistake("post-snapshot mistake", "why")
        a.reflect("post-snapshot well", "missed", "differently")
        a_counts = _counts(a)
        assert a_counts == {
            "facts": 5, "decisions": 4, "mistakes": 3, "reflections": 3}

        b = AgentMemory.load(str(tmp_path / "b"))
        b_export = b.export_all()
        b.db.close()

        # B → A: everything in B is already in A — counts must not move
        a.db.close()
        a = AgentMemory.import_from(b_export, str(tmp_path / "a"))
        assert _counts(a) == a_counts, \
            "importing a stale subset must be a no-op (the incident)"
        c = a.last_import_counts
        assert all(v["imported"] == 0 for v in c.values())

        # A → B: B catches up to A exactly
        a_export = a.export_all()
        a.db.close()
        b = AgentMemory.import_from(a_export, str(tmp_path / "b"))
        assert _counts(b) == a_counts
        b.db.close()

        # Second round trip in both directions: fully stable now
        for src_dir, dst_dir in ((tmp_path / "b", tmp_path / "a"),
                                 (tmp_path / "a", tmp_path / "b")):
            src = AgentMemory.load(str(src_dir))
            export = src.export_all()
            src.db.close()
            dst = AgentMemory.import_from(export, str(dst_dir))
            assert _counts(dst) == a_counts
            assert all(
                v["imported"] == 0
                for v in dst.last_import_counts.values()
            )
            dst.db.close()

    def test_import_via_file_roundtrip(self, tmp_path):
        """JSON-file round trip (the real `null import` path)."""
        src = _make_store(tmp_path / "src")
        packet = tmp_path / "export.json"
        with open(packet, "w", encoding="utf-8") as f:
            json.dump(src.export_all(), f)
        src.db.close()

        with open(packet, encoding="utf-8") as f:
            data = json.load(f)
        target_dir = str(tmp_path / "target")
        AgentMemory.import_from(data, target_dir).db.close()
        mem = AgentMemory.import_from(data, target_dir)
        assert all(
            v["imported"] == 0 for v in mem.last_import_counts.values())
        mem.db.close()
