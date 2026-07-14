"""Tests for event-sourced sync Phase A (issue #20).

Covers: emitter schema/atomicity/seq monotonicity, dual-write (exactly one
event per write, every kind), the NULL_EVENT_LOG gate (no events dir when
unset), genesis export + replay-verify clean on a fresh populated store,
drift detection (injected un-evented mutations), forget tombstone replay,
and the scope field (defaults + preservation).
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from null_memory.agent import AgentMemory
from null_memory.events import (
    EVENTS_DIRNAME,
    EventEmitter,
    event_log_enabled,
    export_genesis,
    get_machine_id,
)
from null_memory.migrate_v3 import init_unified_db
from null_memory.replay import materialize, replay_verify

from tests.conftest import quiesce_mem, run_null


# ── Helpers ────────────────────────────────────────────────────────────────


def _events_dir(mem: AgentMemory) -> str:
    return os.path.join(os.path.dirname(mem.db.db_path), EVENTS_DIRNAME)


def _read_log(mem: AgentMemory) -> list[dict]:
    """All events in this writer's (non-genesis) log file."""
    path = mem.events.log_path
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@pytest.fixture
def emem(tmp_path, monkeypatch):
    """AgentMemory with the event log enabled."""
    monkeypatch.setenv("NULL_EVENT_LOG", "1")
    m = AgentMemory.load(str(tmp_path / "agent"))
    yield m
    quiesce_mem(m)


@pytest.fixture
def unified_emem(tmp_path, monkeypatch):
    """Unified-store AgentMemory with the event log enabled (needed for
    anchor events — anchors are unified-only)."""
    unified = tmp_path / "unified.db"
    init_unified_db(str(unified)).close()
    monkeypatch.setenv("NULL_DIR", str(tmp_path))
    monkeypatch.setenv("NULL_EVENT_LOG", "1")
    agent_dir = tmp_path / "atlas"
    agent_dir.mkdir()
    m = AgentMemory.load(agent_dir=str(agent_dir), personality="atlas")
    assert m.db.unified
    yield m
    quiesce_mem(m)


# ── Gate: NULL_EVENT_LOG unset = zero behavior change ─────────────────────


class TestGate:
    def test_disabled_by_default(self, mem, monkeypatch):
        monkeypatch.delenv("NULL_EVENT_LOG", raising=False)
        assert not event_log_enabled()
        assert mem.events is None

    def test_no_events_dir_when_unset(self, mem, monkeypatch):
        monkeypatch.delenv("NULL_EVENT_LOG", raising=False)
        mem.learn("gate test fact", project="demo")
        mem.decide("gate decision", "reasoning", project="demo")
        mem.mistake("gate mistake", "why", project="demo")
        assert not os.path.isdir(_events_dir(mem))

    def test_emit_returns_none_when_disabled(self, mem, monkeypatch):
        monkeypatch.setenv("NULL_EVENT_LOG", "1")
        emitter = mem.events
        monkeypatch.delenv("NULL_EVENT_LOG")
        assert emitter.emit("fact.add", "abc", {"fact": "x"}) is None
        assert not os.path.isfile(emitter.log_path)


# ── Writer identity ────────────────────────────────────────────────────────


class TestWriterIdentity:
    def test_machine_id_generated_once_into_store_config(self, tmp_path):
        store = str(tmp_path / "store")
        first = get_machine_id(store)
        second = get_machine_id(store)
        assert first == second
        with open(os.path.join(store, "config.json")) as f:
            assert json.load(f)["machine_id"] == first

    def test_machine_id_preserves_existing_config(self, tmp_path):
        store = str(tmp_path / "store")
        os.makedirs(store)
        with open(os.path.join(store, "config.json"), "w") as f:
            json.dump({"age_decay_rate": 0.005}, f)
        get_machine_id(store)
        with open(os.path.join(store, "config.json")) as f:
            cfg = json.load(f)
        assert cfg["age_decay_rate"] == 0.005
        assert "machine_id" in cfg

    def test_writer_id_includes_personality(self, emem):
        assert emem.events.writer_id.endswith(".atlas")
        assert emem.events.writer_id == (
            f"{emem.events.machine_id}.atlas")


# ── Emitter: schema, atomicity, seq ────────────────────────────────────────


class TestEmitter:
    def test_event_schema_matches_design_doc(self, emem):
        emem.events.emit("fact.add", "abc123", {"fact": "x", "project": "p"})
        events = _read_log(emem)
        assert len(events) == 1
        ev = events[0]
        # Exact key set AND order per the doc (+ scope amendment)
        assert list(ev.keys()) == [
            "seq", "writer", "ts", "kind", "id", "scope", "data"]
        assert ev["seq"] == 1
        assert ev["writer"] == emem.events.writer_id
        assert ev["ts"].endswith("Z")
        assert ev["kind"] == "fact.add"
        assert ev["id"] == "abc123"
        assert ev["scope"] == "org"
        assert ev["data"] == {"fact": "x", "project": "p"}

    def test_seq_monotonic_within_writer(self, emem):
        for i in range(5):
            emem.events.emit("fact.add", f"id{i}", {})
        seqs = [ev["seq"] for ev in _read_log(emem)]
        assert seqs == [1, 2, 3, 4, 5]

    def test_seq_survives_reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NULL_EVENT_LOG", "1")
        agent_dir = str(tmp_path / "agent")
        m1 = AgentMemory.load(agent_dir)
        m1.events.emit("fact.add", "a", {})
        m1.events.emit("fact.add", "b", {})
        quiesce_mem(m1)
        m1.db.close()
        m2 = AgentMemory.load(agent_dir)
        ev = m2.events.emit("fact.add", "c", {})
        assert ev["seq"] == 3
        quiesce_mem(m2)

    def test_appends_are_line_atomic(self, emem):
        # Every line in the log parses standalone — no torn/interleaved JSON.
        for i in range(20):
            emem.events.emit("fact.add", f"id{i}", {"fact": "x" * 100})
        with open(emem.events.log_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        assert len(lines) == 20
        for line in lines:
            json.loads(line)

    def test_unknown_kind_rejected(self, emem):
        with pytest.raises(ValueError):
            emem.events.emit("fact.destroy", "abc", {})

    def test_scope_defaults_to_org_and_is_preserved(self, emem):
        emem.events.emit("fact.add", "a", {})
        emem.events.emit("broadcast", "b", {"event": "x"}, scope="team")
        events = _read_log(emem)
        assert events[0]["scope"] == "org"
        assert events[1]["scope"] == "team"


# ── Dual-write: exactly one event per write, every kind ───────────────────


class TestDualWrite:
    def test_learn_emits_one_fact_add(self, emem):
        entry = emem.learn("Rust has zero-cost abstractions", project="demo")
        adds = [e for e in _read_log(emem) if e["kind"] == "fact.add"]
        assert len(adds) == 1
        assert adds[0]["id"] == entry["id"]
        # Enough data to reconstruct the row
        assert adds[0]["data"]["fact"] == "Rust has zero-cost abstractions"
        assert adds[0]["data"]["project"] == "demo"
        assert "created_at" in adds[0]["data"]
        # Local statistics are never evented
        assert "access_count" not in adds[0]["data"]

    def test_duplicate_learn_emits_one_fact_update(self, emem):
        emem.learn("Postgres supports JSONB columns", project="demo",
                   confidence=0.8)
        emem.learn("Postgres supports JSONB columns", project="demo",
                   confidence=0.95)
        events = _read_log(emem)
        assert [e["kind"] for e in events] == ["fact.add", "fact.update"]
        assert events[1]["data"]["confidence"] == 0.95

    def test_learn_replaces_emits_supersede_update(self, emem):
        old = emem.learn("Deploys go out through the legacy bash script",
                         project="demo")
        new = emem.learn("Kubernetes now orchestrates every deployment "
                         "rollout via Argo", project="demo",
                         replaces=old["id"])
        updates = [e for e in _read_log(emem) if e["kind"] == "fact.update"]
        assert len(updates) == 1
        assert updates[0]["id"] == old["id"]
        assert updates[0]["data"] == {"superseded_by": new["id"]}

    def test_forget_emits_tombstone_by_id(self, emem):
        entry = emem.learn("Temporary credential note", project="demo")
        emem.forget(fact_id=entry["id"])
        forgets = [e for e in _read_log(emem) if e["kind"] == "fact.forget"]
        assert len(forgets) == 1
        assert forgets[0]["id"] == entry["id"]

    def test_forget_missing_id_emits_nothing(self, emem):
        assert emem.forget(fact_id="nonexistent") is None
        assert not [e for e in _read_log(emem)
                    if e["kind"] == "fact.forget"]

    def test_decide_emits_one_decision_add(self, emem):
        emem.decide("Use JSONL logs", "append-only merges", project="demo")
        events = [e for e in _read_log(emem) if e["kind"] == "decision.add"]
        assert len(events) == 1
        assert events[0]["data"]["decision"] == "Use JSONL logs"
        assert events[0]["data"]["reasoning"] == "append-only merges"

    def test_record_outcome_emits_one_outcome_add(self, emem):
        emem.decide("Adopt SQLite WAL", "concurrency", project="demo")
        emem.record_outcome("SQLite WAL", "worked well", success=True)
        events = [e for e in _read_log(emem) if e["kind"] == "outcome.add"]
        assert len(events) == 1
        assert events[0]["data"]["outcome"] == "worked well"
        assert events[0]["data"]["success"] is True

    def test_mistake_emits_one_mistake_add(self, emem):
        entry = emem.mistake("Deployed on Friday", "no rollback window",
                             project="demo")
        events = [e for e in _read_log(emem) if e["kind"] == "mistake.add"]
        assert len(events) == 1
        assert events[0]["id"] == str(entry["id"])
        assert events[0]["data"]["mistake"] == "Deployed on Friday"

    def test_reflect_emits_one_reflection_add(self, emem):
        emem.reflect("shipped it", "missed tests", "test first",
                     project="demo")
        events = [e for e in _read_log(emem)
                  if e["kind"] == "reflection.add"]
        assert len(events) == 1
        assert events[0]["data"]["went_well"] == "shipped it"

    def test_add_exemplar_emits_one_exemplar_add(self, emem):
        emem.add_exemplar("scenario", "user text", "agent text", "cal",
                          tags=["t"])
        events = [e for e in _read_log(emem) if e["kind"] == "exemplar.add"]
        assert len(events) == 1
        assert events[0]["data"]["user_text"] == "user text"

    def test_add_probe_emits_one_probe_add(self, emem):
        probe = emem.add_probe("What is the limit?", "500", probe_type="user")
        events = [e for e in _read_log(emem) if e["kind"] == "probe.add"]
        assert len(events) == 1
        assert events[0]["id"] == str(probe["id"])
        assert events[0]["data"]["probe_type"] == "user"

    def test_session_open_and_close_evented(self, emem):
        session = emem.start_session(project="demo")
        emem.end_session("wrapped up")
        kinds = [e["kind"] for e in _read_log(emem)]
        assert kinds.count("session.open") == 1
        assert kinds.count("session.close") == 1
        opens = [e for e in _read_log(emem) if e["kind"] == "session.open"]
        assert opens[0]["id"] == session.session_id
        assert opens[0]["data"]["project"] == "demo"

    def test_anchor_emits_fact_anchor(self, unified_emem):
        mem = unified_emem
        entry = mem.learn("The moment Null began", project="null")
        mem.anchor(entry["id"], "origin", note="load-bearing")
        events = [e for e in _read_log(mem) if e["kind"] == "fact.anchor"]
        assert len(events) == 1
        assert events[0]["id"] == entry["id"]
        assert events[0]["data"]["anchor_type"] == "origin"

    def test_events_carry_db_committed_truth(self, emem):
        """Every fact.add id exists in the db (events describe commits)."""
        emem.learn("Kubernetes orchestrates containers", project="demo")
        for ev in _read_log(emem):
            if ev["kind"] == "fact.add":
                assert emem.db.get_fact_by_id(ev["id"]) is not None


# ── Genesis export ─────────────────────────────────────────────────────────


class TestGenesis:
    def test_genesis_requires_flag(self, mem, monkeypatch):
        monkeypatch.delenv("NULL_EVENT_LOG", raising=False)
        with pytest.raises(RuntimeError):
            export_genesis(mem)

    def test_genesis_exports_every_live_entity(self, emem):
        emem.learn("fact one about python", project="a")
        emem.learn("fact two about rust", project="b")
        emem.decide("a decision", "reasoning", project="a")
        emem.mistake("a mistake", "why", project="a")
        result = export_genesis(emem)
        assert os.path.isfile(result["path"])
        with open(result["path"], encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        # Header + one add-event per entity
        assert lines[0]["kind"] == "genesis"
        assert lines[0]["data"]["high_water"][emem.events.writer_id] > 0
        kinds = [l["kind"] for l in lines[1:]]
        assert kinds.count("fact.add") >= 2  # + outcome lesson facts etc.
        assert kinds.count("decision.add") == 1
        assert kinds.count("mistake.add") == 1
        assert result["count"] == len(lines) - 1

    def test_genesis_is_deterministic(self, emem):
        emem.learn("deterministic export check", project="a")
        genesis_file = os.path.join(
            emem.events.events_dir,
            f"genesis.{emem.events.writer_id}.jsonl")
        export_genesis(emem)
        with open(genesis_file, encoding="utf-8") as f:
            first = f.read()
        export_genesis(emem, force=True)
        with open(genesis_file, encoding="utf-8") as f:
            second = f.read()
        # Identical apart from the header timestamp line
        assert first.splitlines()[1:] == second.splitlines()[1:]

    def test_genesis_refuses_overwrite_without_force(self, emem):
        export_genesis(emem)
        with pytest.raises(FileExistsError):
            export_genesis(emem)
        export_genesis(emem, force=True)  # does not raise


# ── Replay + verify ────────────────────────────────────────────────────────


def _populate(mem: AgentMemory) -> dict:
    facts = {}
    facts["f1"] = mem.learn("Python uses indentation for code blocks",
                            project="demo", confidence=0.9)
    facts["f2"] = mem.learn("Rust enforces borrow checking at compile time",
                            project="demo", confidence=0.95)
    mem.decide("Use event logs for sync", "merge-free by construction",
               project="demo")
    mem.record_outcome("event logs", "two machines converged", success=True)
    mem.mistake("Synced binary db over git", "conflicts are unmergable",
                project="demo")
    mem.reflect("emitter shipped", "missed clock skew", "bound it with seq",
                project="demo")
    mem.add_exemplar("sync design", "how do we merge?", "we don't — append",
                     "calm", tags=["design"])
    mem.add_probe("What construction makes merges impossible?",
                  "append-only", probe_type="user")
    return facts


class TestReplayVerify:
    def test_clean_on_fresh_populated_store(self, emem):
        export_genesis(emem)  # empty baseline
        _populate(emem)
        report = replay_verify(emem.db, _events_dir(emem))
        assert report["drift"] == 0, report["details"]
        assert report["clean"] is True
        assert report["counts"]["fact"]["live"] > 0
        assert (report["counts"]["fact"]["live"]
                == report["counts"]["fact"]["replayed"])

    def test_clean_when_genesis_taken_after_unevented_history(
            self, tmp_path, monkeypatch):
        # History written with the flag OFF, then genesis re-baselines it.
        agent_dir = str(tmp_path / "agent")
        monkeypatch.delenv("NULL_EVENT_LOG", raising=False)
        m = AgentMemory.load(agent_dir)
        _populate(m)
        monkeypatch.setenv("NULL_EVENT_LOG", "1")
        export_genesis(m)
        m.learn("a post-genesis fact about gravity", project="demo")
        report = replay_verify(m.db, _events_dir(m))
        assert report["clean"], report["details"]
        quiesce_mem(m)

    def test_detects_unevented_delete(self, emem):
        export_genesis(emem)
        facts = _populate(emem)
        # Un-evented hard delete from the live db — replay still has it.
        emem.db.conn.execute("DELETE FROM facts WHERE id = ?",
                             (facts["f1"]["id"],))
        emem.db.conn.commit()
        report = replay_verify(emem.db, _events_dir(emem))
        assert not report["clean"]
        assert any(facts["f1"]["id"] in d and "extra" not in d or
                   facts["f1"]["id"] in d for d in report["details"])

    def test_detects_unevented_insert(self, emem):
        export_genesis(emem)
        _populate(emem)
        emem.db.insert_fact({
            "id": "deadbeef0000",
            "fact": "smuggled in behind the log's back",
            "created_at": "2026-01-01T00:00:00+00:00",
        })
        emem.db.conn.commit()
        report = replay_verify(emem.db, _events_dir(emem))
        assert not report["clean"]
        assert any("deadbeef0000" in d for d in report["details"])

    def test_detects_field_drift(self, emem):
        export_genesis(emem)
        facts = _populate(emem)
        emem.db.conn.execute(
            "UPDATE facts SET fact = 'rewritten without an event' "
            "WHERE id = ?", (facts["f2"]["id"],))
        emem.db.conn.commit()
        report = replay_verify(emem.db, _events_dir(emem))
        assert not report["clean"]
        assert any("fact" in d and facts["f2"]["id"] in d
                   for d in report["details"])

    def test_forget_tombstone_replays(self, emem):
        export_genesis(emem)
        facts = _populate(emem)
        emem.forget(fact_id=facts["f1"]["id"])
        report = replay_verify(emem.db, _events_dir(emem))
        assert report["clean"], report["details"]

    def test_tombstone_mismatch_detected(self, emem):
        export_genesis(emem)
        facts = _populate(emem)
        # Un-evented forget (raw db) — replay disagrees on the flag.
        emem.db.forget_fact(facts["f2"]["id"])
        report = replay_verify(emem.db, _events_dir(emem))
        assert not report["clean"]
        assert any("forgotten" in d for d in report["details"])

    def test_probe_result_replays_counts(self, emem):
        export_genesis(emem)
        probe = emem.add_probe("What checks borrows?", "Rust",
                               probe_type="user")
        emem.learn("Rust enforces borrow checking", project="global")
        emem._execute_probe(dict(probe))
        # Replay into a scratch db and compare the probe counters directly.
        from null_memory.db import NullDB
        scratch = os.path.join(os.path.dirname(emem.db.db_path), "scratch")
        rdb = NullDB(scratch)
        rdb.initialize()
        materialize(_events_dir(emem), rdb)
        live = emem.db.conn.execute(
            "SELECT run_count, pass_count, last_result FROM probes "
            "WHERE id = ?", (probe["id"],)).fetchone()
        replayed = rdb.conn.execute(
            "SELECT run_count, pass_count, last_result FROM probes "
            "WHERE id = ?", (probe["id"],)).fetchone()
        assert tuple(live) == tuple(replayed)
        rdb.close()

    def test_anchor_replays(self, unified_emem):
        mem = unified_emem
        export_genesis(mem)
        entry = mem.learn("The first conversation", project="null")
        mem.anchor(entry["id"], "origin", note="where it began")
        from null_memory.db import NullDB
        scratch = os.path.join(os.path.dirname(mem.db.db_path), "scratch")
        rdb = NullDB(scratch)
        rdb.initialize()
        materialize(_events_dir(mem), rdb)
        row = rdb.conn.execute(
            "SELECT anchor_type, anchor_note FROM facts WHERE id = ?",
            (entry["id"],)).fetchone()
        assert row[0] == "origin"
        assert row[1] == "where it began"
        rdb.close()
        report = replay_verify(mem.db, _events_dir(mem))
        assert report["clean"], report["details"]

    def test_replay_is_idempotent(self, emem):
        export_genesis(emem)
        _populate(emem)
        from null_memory.db import NullDB
        scratch = os.path.join(os.path.dirname(emem.db.db_path), "scratch")
        rdb = NullDB(scratch)
        rdb.initialize()
        materialize(_events_dir(emem), rdb)
        first = rdb.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        materialize(_events_dir(emem), rdb)  # replay everything again
        second = rdb.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        assert first == second
        rdb.close()


# ── CLI: null events genesis + doctor replay-verify ───────────────────────


class TestCLI:
    def test_genesis_requires_flag(self, tmp_path):
        rc, out, err = run_null("events", "genesis", tmp_path=tmp_path)
        assert rc == 1
        assert "NULL_EVENT_LOG" in err

    def test_genesis_export_and_refusal(self, tmp_path):
        env = {"NULL_EVENT_LOG": "1"}
        rc, out, _ = run_null("learn", "CLI genesis test fact",
                              env_override=env, tmp_path=tmp_path)
        assert rc == 0
        rc, out, _ = run_null("events", "genesis", env_override=env,
                              tmp_path=tmp_path)
        assert rc == 0
        assert "Genesis exported" in out
        assert os.path.isdir(tmp_path / "events")
        # Idempotent: second run refuses
        rc, _, err = run_null("events", "genesis", env_override=env,
                              tmp_path=tmp_path)
        assert rc == 1
        assert "force" in err
        # --force re-baselines
        rc, out, _ = run_null("events", "genesis", "--force",
                              env_override=env, tmp_path=tmp_path)
        assert rc == 0

    def test_doctor_reports_clean_then_drift(self, tmp_path):
        env = {"NULL_EVENT_LOG": "1"}
        rc, _, _ = run_null("events", "genesis", env_override=env,
                            tmp_path=tmp_path)
        assert rc == 0
        rc, _, _ = run_null("learn", "Doctor replay verify fact",
                            env_override=env, tmp_path=tmp_path)
        assert rc == 0
        rc, out, _ = run_null("doctor", env_override=env, tmp_path=tmp_path)
        assert rc == 0
        assert "replay-verify clean" in out
        # Inject drift: un-evented delete straight into the store db.
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM facts")
        conn.commit()
        conn.close()
        rc, out, _ = run_null("doctor", env_override=env, tmp_path=tmp_path)
        assert rc == 0
        assert "drift" in out

    def test_doctor_silent_when_flag_unset(self, tmp_path):
        rc, _, _ = run_null("learn", "No event log here",
                            tmp_path=tmp_path)
        assert rc == 0
        rc, out, _ = run_null("doctor", tmp_path=tmp_path)
        assert rc == 0
        assert "replay-verify" not in out
        assert not os.path.isdir(tmp_path / "events")
