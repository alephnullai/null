"""Tests for the org exchange (issue #20 Phase B).

No network: a local bare git repo is the exchange remote; two seats
(separate stores, separate identities) share it. Covers: post → commit →
push, the post/ingest round-trip with provenance + the non-self
confidence discount, repo.push surfacing (pull recommended — NEVER
auto-pulled), the claims TTL lifecycle, query ask/answer, subscription
scoping (unsubscribed streams are not ingested), local dual-logging of
own posts, idempotent re-ingest, and the store-gitignore guard."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from null_memory.agent import AgentMemory
from null_memory.exchange import (
    DEFAULT_CONFIDENCE_DISCOUNT,
    DEFAULT_INGEST_CONFIDENCE,
    EXCHANGE_KINDS,
    ExchangeClient,
    active_claims,
    claims_status_lines,
    exchange_briefing_lines,
    pending_queries,
    recent_repo_pushes,
)

from tests.conftest import quiesce_mem


def _git(cwd, *args) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", *args], cwd=str(cwd),
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    """Commits inside exchange clones need an identity on bare CI boxes."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


@pytest.fixture
def bare(tmp_path):
    bare = tmp_path / "org-exchange.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")
    return bare


def _make_seat(tmp_path, name: str, url: str, subscribe: list[str]):
    """A seat = its own store dir + machine identity + exchange config."""
    seat = tmp_path / name
    seat.mkdir()
    (seat / "config.json").write_text(json.dumps({
        "machine_id": f"{name}-mach",
        "exchange": {"url": url, "subscribe": subscribe},
    }))
    return AgentMemory.load(str(seat))


@pytest.fixture
def seats(tmp_path, bare):
    """Seat A (worker) and seat B (hub, subscribed to A's stream)."""
    mem_a = _make_seat(tmp_path, "seata", str(bare), [])
    mem_b = _make_seat(tmp_path, "seatb", str(bare), ["seata-mach.atlas"])
    yield mem_a, mem_b
    quiesce_mem(mem_a)
    quiesce_mem(mem_b)


# ── posting ─────────────────────────────────────────────────────────────


class TestPost:
    def test_post_appends_commits_pushes(self, seats, bare):
        mem_a, _mem_b = seats
        client = ExchangeClient(mem_a)
        assert client.stream == "seata-mach.atlas"
        event = client.post("broadcast", {"text": "hello org"})
        assert event["kind"] == "broadcast"
        assert event["writer"] == "seata-mach.atlas"
        assert event["seq"] == 1
        # Landed in the local clone's own stream...
        stream_file = os.path.join(client.streams_dir,
                                   "seata-mach.atlas.jsonl")
        with open(stream_file) as f:
            assert json.loads(f.readline())["data"]["text"] == "hello org"
        # ...and was pushed to the shared remote.
        tree = _git(bare, "ls-tree", "-r", "--name-only", "HEAD").stdout
        assert "streams/seata-mach.atlas.jsonl" in tree

    def test_seq_is_monotonic_per_stream(self, seats):
        mem_a, _mem_b = seats
        client = ExchangeClient(mem_a)
        e1 = client.post("broadcast", {"text": "one"})
        e2 = client.post("broadcast", {"text": "two"})
        assert (e1["seq"], e2["seq"]) == (1, 2)

    def test_unknown_kind_rejected(self, seats):
        mem_a, _mem_b = seats
        with pytest.raises(ValueError, match="unknown exchange kind"):
            ExchangeClient(mem_a).post("rm.rf", {"x": 1})

    def test_post_without_config_raises(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path / "lone"))
        try:
            with pytest.raises(RuntimeError, match="exchange not available"):
                ExchangeClient(mem).post("broadcast", {"text": "x"})
        finally:
            quiesce_mem(mem)

    def test_own_posts_dual_log_locally(self, tmp_path, bare, monkeypatch):
        """The seat's own event log records what it posted (audit)."""
        monkeypatch.setenv("NULL_EVENT_LOG", "1")
        mem = _make_seat(tmp_path, "seatd", str(bare), [])
        try:
            ExchangeClient(mem).post("report.session",
                                     {"summary": "did the thing"})
            with open(mem.events.log_path) as f:
                events = [json.loads(line) for line in f if line.strip()]
            posts = [e for e in events if e["kind"] == "exchange.post"]
            assert len(posts) == 1
            assert posts[0]["data"]["kind"] == "report.session"
            assert posts[0]["data"]["data"]["summary"] == "did the thing"
        finally:
            quiesce_mem(mem)

    def test_store_repo_gitignores_exchange_clone(self, tmp_path, bare):
        seat = tmp_path / "seatg"
        seat.mkdir()
        _git(seat, "init", "-b", "main")
        (seat / "config.json").write_text(json.dumps({
            "machine_id": "seatg-mach",
            "exchange": {"url": str(bare), "subscribe": []},
        }))
        mem = AgentMemory.load(str(seat))
        try:
            ExchangeClient(mem).post("broadcast", {"text": "x"})
            gitignore = (seat / ".gitignore").read_text()
            assert "exchange/" in gitignore.splitlines()
            status = _git(seat, "status", "--porcelain").stdout
            assert "exchange/" not in status  # really ignored
        finally:
            quiesce_mem(mem)

    def test_announce_push_reads_repo_state(self, seats, tmp_path):
        mem_a, _mem_b = seats
        code = tmp_path / "code-repo"
        code.mkdir()
        _git(code, "init", "-b", "feature")
        (code / "x.txt").write_text("v1")
        _git(code, "add", "-A")
        _git(code, "commit", "-m", "work")
        _git(code, "remote", "add", "origin",
             "git@github.com:org/hiwave-linux.git")
        sha = _git(code, "rev-parse", "HEAD").stdout.strip()
        event = ExchangeClient(mem_a).announce_push(
            str(code), summary="linux build green")
        assert event["kind"] == "repo.push"
        assert event["data"]["repo"] == "hiwave-linux"
        assert event["data"]["sha"] == sha
        assert event["data"]["branch"] == "feature"
        assert event["data"]["summary"] == "linux build green"

    def test_announce_push_outside_repo_raises(self, seats, tmp_path):
        mem_a, _mem_b = seats
        nowhere = tmp_path / "not-a-repo"
        nowhere.mkdir()
        with pytest.raises(RuntimeError, match="not a git repo"):
            ExchangeClient(mem_a).announce_push(str(nowhere))


# ── ingestion: the round-trip ───────────────────────────────────────────


class TestIngest:
    def test_round_trip_with_provenance_and_discount(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("report.session", {
            "summary": "hiwave-linux: ALSA backend now passes loopback",
            "project": "hiwave",
        })
        report = ExchangeClient(mem_b).ingest()
        assert report["facts"] == 1
        assert report["streams"] == {"seata-mach.atlas": 1}
        facts = mem_b.db.get_active_facts()
        ingested = [f for f in facts if "ALSA" in f["fact"]]
        assert len(ingested) == 1
        fact = ingested[0]
        # Provenance: the writer is recorded, source-tier discounted.
        assert fact["source"] == "exchange:seata-mach.atlas"
        assert fact["provenance"] == "exchange"
        assert fact["project"] == "hiwave"
        expected = round(
            DEFAULT_INGEST_CONFIDENCE * DEFAULT_CONFIDENCE_DISCOUNT, 4)
        assert fact["confidence"] == pytest.approx(expected)

    def test_explicit_confidence_still_discounted(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("directive", {
            "text": "All seats: pin numba below 0.66", "confidence": 0.95})
        ExchangeClient(mem_b).ingest()
        fact = [f for f in mem_b.db.get_active_facts()
                if "numba" in f["fact"]][0]
        assert fact["confidence"] == pytest.approx(
            round(0.95 * DEFAULT_CONFIDENCE_DISCOUNT, 4))
        assert fact["confidence"] < 0.95  # never at face value

    def test_reingest_is_idempotent(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("broadcast", {"text": "once only"})
        client_b = ExchangeClient(mem_b)
        client_b.ingest()
        n = len(mem_b.db.get_active_facts())
        report = client_b.ingest()
        assert report["streams"] == {}  # cursor: nothing new
        assert len(mem_b.db.get_active_facts()) == n
        # Even with a forced cursor reset, the deterministic id dedupes.
        mem_b.db.set_meta("exchange_cursor.seata-mach.atlas", "0")
        mem_b.db.conn.commit()
        client_b.ingest()
        assert len(mem_b.db.get_active_facts()) == n

    def test_word_confidence_does_not_drop_the_event(self, seats):
        """Regression (Talos, 2026-07-14): a peer that sends a WORD in
        `confidence` ('high') instead of a float made float() raise inside
        _ingest_fact, which aborted _apply_foreign_event before the view
        applied and silently dropped the ENTIRE event. A word confidence
        must ingest, not vanish."""
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post(
            "broadcast", {"text": "word-confidence note", "confidence": "high"})
        ExchangeClient(mem_b).ingest()
        facts = [f["fact"] for f in mem_b.db.get_active_facts()]
        assert any("word-confidence note" in f for f in facts), (
            "a peer's string confidence dropped the whole foreign event")

    def test_unsubscribed_stream_not_ingested(self, seats, tmp_path, bare):
        """Subscription scoping: seat C posts, B (not subscribed to C)
        must never ingest it."""
        mem_a, mem_b = seats
        mem_c = _make_seat(tmp_path, "seatc", str(bare), [])
        try:
            ExchangeClient(mem_c).post("broadcast",
                                       {"text": "C's private-ish note"})
            ExchangeClient(mem_a).post("broadcast", {"text": "A's note"})
            report = ExchangeClient(mem_b).ingest()
            assert list(report["streams"]) == ["seata-mach.atlas"]
            facts = [f["fact"] for f in mem_b.db.get_active_facts()]
            assert any("A's note" in f for f in facts)
            assert not any("private-ish" in f for f in facts)
            assert mem_b.db.get_meta(
                "exchange_cursor.seatc-mach.atlas") is None
        finally:
            quiesce_mem(mem_c)

    def test_own_stream_never_ingested_even_if_subscribed(
            self, tmp_path, bare):
        mem = _make_seat(tmp_path, "seatself", str(bare),
                         ["seatself-mach.atlas"])
        try:
            client = ExchangeClient(mem)
            assert client.subscribed == []
            client.post("broadcast", {"text": "talking to myself"})
            client.ingest()
            facts = [f["fact"] for f in mem.db.get_active_facts()]
            assert not any("talking to myself" in f for f in facts)
        finally:
            quiesce_mem(mem)

    def test_ingest_unconfigured_reports_warning(self, tmp_path):
        mem = AgentMemory.load(str(tmp_path / "lone"))
        try:
            report = ExchangeClient(mem).ingest()
            assert report["warning"] == "exchange not configured"
            assert report["facts"] == 0
        finally:
            quiesce_mem(mem)


# ── repo.push: pull recommended, never auto-pulled ──────────────────────


class TestRepoPush:
    def test_repo_push_surfaces_and_never_pulls(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("repo.push", {
            "repo": "hiwave-linux", "sha": "a1b2c3d4e5f6a7b8",
            "branch": "main", "summary": "ALSA fixes"})
        report = ExchangeClient(mem_b).ingest()
        assert report["repo_pushes"] == 1
        assert report["facts"] == 0  # an announcement, not knowledge
        pushes = recent_repo_pushes(mem_b.db)
        assert len(pushes) == 1
        assert pushes[0]["writer"] == "seata-mach.atlas"
        assert pushes[0]["repo"] == "hiwave-linux"
        lines = exchange_briefing_lines(mem_b.db)
        assert any("⚠ seata-mach.atlas pushed hiwave-linux@a1b2c3d" in line
                   and "pull recommended" in line for line in lines)
        # NEVER auto-pulled: there is no such repo anywhere on disk and
        # ingestion succeeded anyway — the event carries a recommendation,
        # not code, and triggers no git operation on code repos.

    def test_repo_push_appears_in_briefing(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("repo.push", {
            "repo": "hiwave-linux", "sha": "deadbeef00", "branch": "main"})
        ExchangeClient(mem_b).ingest()
        assert "pull recommended" in mem_b.briefing()


# ── claims: advisory WIP, TTL lifecycle ─────────────────────────────────


class TestClaims:
    def test_acquire_surfaces_with_ttl(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("claim.acquire", {
            "resource": "repo:null/src/cli.py", "ttl_minutes": 30})
        ExchangeClient(mem_b).ingest()
        claims = active_claims(mem_b.db)
        assert len(claims) == 1
        assert claims[0]["resource"] == "repo:null/src/cli.py"
        assert claims[0]["writer"] == "seata-mach.atlas"
        lines = exchange_briefing_lines(mem_b.db)
        claim_lines = [ln for ln in lines if "holds" in ln]
        assert len(claim_lines) == 1
        assert "seata-mach.atlas holds repo:null/src/cli.py" in claim_lines[0]
        assert "m left" in claim_lines[0]
        # status surface too
        assert any("holds repo:null/src/cli.py" in ln
                   for ln in claims_status_lines(mem_b.db))

    def test_release_clears_claim(self, seats):
        mem_a, mem_b = seats
        client_a = ExchangeClient(mem_a)
        client_a.post("claim.acquire",
                      {"resource": "tool:release.sh", "ttl_minutes": 30})
        client_a.post("claim.release", {"resource": "tool:release.sh"})
        ExchangeClient(mem_b).ingest()
        assert active_claims(mem_b.db) == []

    def test_claim_expires_on_ttl(self, seats):
        mem_a, mem_b = seats
        ExchangeClient(mem_a).post("claim.acquire", {
            "resource": "repo:null/src/db.py", "ttl_minutes": 0.001})
        ExchangeClient(mem_b).ingest()
        import time
        time.sleep(0.1)
        assert active_claims(mem_b.db) == []
        assert exchange_briefing_lines(mem_b.db) == []

    def test_release_by_other_writer_ignored(self, seats, tmp_path, bare):
        """Only the holder can release its claim — a third seat's
        release of A's resource is a no-op."""
        mem_a, mem_b = seats
        mem_c = _make_seat(tmp_path, "seatc", str(bare), [])
        try:
            ExchangeClient(mem_a).post("claim.acquire", {
                "resource": "repo:hiwave", "ttl_minutes": 30})
            ExchangeClient(mem_c).post("claim.release",
                                       {"resource": "repo:hiwave"})
            # B subscribes to both A and C for this scenario.
            cfg = json.loads(
                (tmp_path / "seatb" / "config.json").read_text())
            cfg["exchange"]["subscribe"] = ["seata-mach.atlas",
                                            "seatc-mach.atlas"]
            (tmp_path / "seatb" / "config.json").write_text(json.dumps(cfg))
            ExchangeClient(mem_b).ingest()
            claims = active_claims(mem_b.db)
            assert len(claims) == 1 and claims[0]["writer"] == \
                "seata-mach.atlas"
        finally:
            quiesce_mem(mem_c)

    def test_own_claim_visible_in_own_status(self, seats):
        mem_a, _mem_b = seats
        ExchangeClient(mem_a).post("claim.acquire", {
            "resource": "repo:null/docs", "ttl_minutes": 15})
        assert any("holds repo:null/docs" in ln
                   for ln in claims_status_lines(mem_a.db))
        # ...but a seat doesn't warn ITSELF about its own claim in the
        # briefing (own_stream filter).
        lines = exchange_briefing_lines(mem_a.db,
                                        own_stream="seata-mach.atlas")
        assert not any("holds" in ln for ln in lines)


# ── queries: ask up, answer down ────────────────────────────────────────


class TestQueries:
    def test_query_ask_surfaces_for_the_hub(self, seats):
        mem_a, mem_b = seats
        ask = ExchangeClient(mem_a).post("query.ask", {
            "question": "Why did we reject cr-sqlite?", "project": "null"})
        ExchangeClient(mem_b).ingest()
        queries = pending_queries(mem_b.db)
        assert len(queries) == 1
        assert queries[0]["id"] == ask["id"]
        assert queries[0]["writer"] == "seata-mach.atlas"
        assert "cr-sqlite" in queries[0]["question"]
        assert any("asks" in ln for ln in exchange_briefing_lines(mem_b.db))

    def test_answering_clears_pending_and_lands_as_fact_for_asker(
            self, seats, tmp_path):
        mem_a, mem_b = seats
        ask = ExchangeClient(mem_a).post("query.ask", {
            "question": "Why did we reject cr-sqlite?"})
        client_b = ExchangeClient(mem_b)
        client_b.ingest()
        assert len(pending_queries(mem_b.db)) == 1
        # Hub answers: own post clears the pending view immediately.
        client_b.post("query.answer", {
            "query_id": ask["id"],
            "answer": "cr-sqlite is a native compiled extension — rejected "
                      "on dependency-fragility grounds",
        })
        assert pending_queries(mem_b.db) == []
        # The asker (subscribed to the hub's stream) receives the answer
        # as a fact with provenance.
        cfg = json.loads((tmp_path / "seata" / "config.json").read_text())
        cfg["exchange"]["subscribe"] = ["seatb-mach.atlas"]
        (tmp_path / "seata" / "config.json").write_text(json.dumps(cfg))
        ExchangeClient(mem_a).ingest()
        facts = [f for f in mem_a.db.get_active_facts()
                 if "dependency-fragility" in f["fact"]]
        assert len(facts) == 1
        assert facts[0]["source"] == "exchange:seatb-mach.atlas"
