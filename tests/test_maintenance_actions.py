"""Unit tests for the shared maintenance primitives (P2-2).

Covers the pure actions in null_memory.memory.maintenance_actions
(merge-pair detection on both the embedding-cosine and Jaccard-fallback
paths, demote/fade/archive candidate selection) and the shared
LeaderLock in null_memory.memory.leader.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from null_memory.memory.leader import LeaderLock
from null_memory.memory.maintenance_actions import (
    DEMOTE_AGE_DAYS_MIN,
    DEMOTE_CONFIDENCE_MAX,
    MERGE_COSINE_THRESHOLD,
    MERGE_JACCARD_THRESHOLD,
    cold_storage_candidates,
    decay_archive_candidates,
    demote_candidates,
    fade_candidates,
    find_band_merge_groups,
    find_merge_pairs,
    jaccard_words,
    merge_decision,
    merge_threshold,
    pick_merge_winner,
    similarity,
)
from null_memory.migrate_v3 import init_unified_db


NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _fact(fid, text, confidence=0.8, project="global", created_days_ago=10,
          access_count=0, anchor_type=None, last_accessed=None):
    return {
        "id": fid,
        "fact": text,
        "confidence": confidence,
        "project": project,
        "created_at": (NOW - timedelta(days=created_days_ago)).isoformat(),
        "access_count": access_count,
        "anchor_type": anchor_type,
        "last_accessed": last_accessed,
    }


# ── Similarity (the ONE definition) ───────────────────────────────────────


class TestSimilarity:
    def test_jaccard_words(self):
        assert jaccard_words("a b c", "a b c") == 1.0
        assert jaccard_words("a b", "c d") == 0.0
        assert jaccard_words("", "") == 0.0

    def test_cosine_path_when_vectors_present(self):
        score, method = similarity(
            "x", "y", vec_a=[1.0], vec_b=[1.0],
            cosine_fn=lambda a, b: 0.97,
        )
        assert method == "cosine"
        assert score == 0.97

    def test_jaccard_fallback_without_vectors(self):
        score, method = similarity("same words here", "same words here")
        assert method == "jaccard"
        assert score == 1.0

    def test_thresholds_per_method(self):
        assert merge_threshold("cosine") == MERGE_COSINE_THRESHOLD == 0.85
        assert merge_threshold("jaccard") == MERGE_JACCARD_THRESHOLD == 0.65


# ── Merge decisions / pairs ───────────────────────────────────────────────


class TestMergeDecision:
    def test_winner_by_confidence(self):
        a = _fact("a", "t", confidence=0.9)
        b = _fact("b", "t", confidence=0.5)
        assert pick_merge_winner(a, b) == (a, b)
        assert pick_merge_winner(b, a) == (a, b)

    def test_tie_keeps_older(self):
        a = _fact("a", "t", confidence=0.8, created_days_ago=5)
        b = _fact("b", "t", confidence=0.8, created_days_ago=50)
        assert pick_merge_winner(a, b) == (b, a)

    def test_below_threshold_is_none(self):
        a = _fact("a", "t")
        b = _fact("b", "t")
        assert merge_decision(a, b, 0.84, method="cosine") is None
        assert merge_decision(a, b, 0.86, method="cosine") is not None

    def test_anchors_are_untouchable(self):
        a = _fact("a", "t", anchor_type="origin")
        b = _fact("b", "t")
        assert merge_decision(a, b, 0.99, method="cosine") is None
        assert merge_decision(b, a, 0.99, method="cosine") is None


class TestFindMergePairs:
    def test_jaccard_fallback_path(self):
        facts = [
            _fact("a", "the deploy pipeline uses docker on ci", 0.7),
            _fact("b", "the deploy pipeline uses docker on ci runners", 0.9),
            _fact("c", "completely unrelated topic about cooking pasta", 0.8),
        ]
        pairs = find_merge_pairs(facts)
        assert len(pairs) == 1
        winner, loser, score = pairs[0]
        assert winner["id"] == "b"   # higher confidence
        assert loser["id"] == "a"
        assert score >= MERGE_JACCARD_THRESHOLD

    def test_embedding_cosine_path(self):
        facts = [
            _fact("a", "totally different words one", 0.7),
            _fact("b", "nothing shared lexically two", 0.9),
        ]
        vectors = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
        pairs = find_merge_pairs(
            facts, vectors=vectors,
            cosine_fn=lambda x, y: 0.95,
        )
        assert len(pairs) == 1
        assert pairs[0][0]["id"] == "b"
        assert pairs[0][2] == 0.95

    def test_cosine_below_threshold_no_merge_even_if_jaccard_high(self):
        # When both vectors exist the cosine verdict is authoritative.
        facts = [
            _fact("a", "identical words here exactly", 0.7),
            _fact("b", "identical words here exactly today", 0.9),
        ]
        vectors = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
        pairs = find_merge_pairs(
            facts, vectors=vectors, cosine_fn=lambda x, y: 0.1,
        )
        assert pairs == []

    def test_mixed_pair_falls_back_to_jaccard(self):
        facts = [
            _fact("a", "the deploy pipeline uses docker on ci", 0.7),
            _fact("b", "the deploy pipeline uses docker on ci runners", 0.9),
        ]
        pairs = find_merge_pairs(
            facts, vectors={"a": [1.0]},  # b has no vector
            cosine_fn=lambda x, y: 0.0,
        )
        assert len(pairs) == 1

    def test_project_boundary_respected(self):
        facts = [
            _fact("a", "shared fact text exactly alike", project="alpha"),
            _fact("b", "shared fact text exactly alike", project="beta"),
        ]
        assert find_merge_pairs(facts) == []

    def test_anchors_skipped(self):
        facts = [
            _fact("a", "shared fact text exactly alike", anchor_type="joy"),
            _fact("b", "shared fact text exactly alike"),
        ]
        assert find_merge_pairs(facts) == []

    def test_short_facts_skipped(self):
        facts = [_fact("a", "two words"), _fact("b", "two words")]
        assert find_merge_pairs(facts) == []

    def test_loser_not_reused(self):
        facts = [
            _fact("a", "alpha beta gamma delta", 0.5),
            _fact("b", "alpha beta gamma delta", 0.9),
            _fact("c", "alpha beta gamma delta", 0.7),
        ]
        pairs = find_merge_pairs(facts)
        losers = [l["id"] for _, l, _ in pairs]
        assert len(losers) == len(set(losers))
        # b survives everything
        assert all(w["id"] == "b" for w, _, _ in pairs)


class TestBandMergeGroups:
    def test_band_merge_picks_highest_eff_conf(self):
        facts = [
            _fact("a", "aleph uses tree-sitter for parsing source code into abstract syntax trees", 0.8),
            _fact("b", "aleph uses tree-sitter for parsing source code with fast incremental updates", 0.9),
        ]
        eff = {"a": 0.8, "b": 0.9}
        groups = find_band_merge_groups(facts, eff, 0.40, 0.65)
        assert len(groups) == 1
        winner, losers = groups[0]
        assert winner["id"] == "b"
        assert [l["id"] for l in losers] == ["a"]

    def test_duplicates_above_band_not_grouped(self):
        facts = [
            _fact("a", "exactly the same five words here", 0.8),
            _fact("b", "exactly the same five words here", 0.9),
        ]
        eff = {"a": 0.8, "b": 0.9}
        assert find_band_merge_groups(facts, eff, 0.40, 0.65) == []


# ── Demote / fade / archive candidates ────────────────────────────────────


class TestDemoteCandidates:
    def test_stale_low_confidence_selected(self):
        f = _fact("a", "t", confidence=0.05,
                  last_accessed=(NOW - timedelta(days=90)).isoformat())
        assert demote_candidates([f], NOW) == [f]

    def test_never_accessed_selected(self):
        f = _fact("a", "t", confidence=None, last_accessed=None)
        assert demote_candidates([f], NOW) == [f]

    def test_confident_fact_kept(self):
        f = _fact("a", "t", confidence=0.9,
                  last_accessed=(NOW - timedelta(days=90)).isoformat())
        assert demote_candidates([f], NOW) == []

    def test_recently_accessed_kept(self):
        f = _fact("a", "t", confidence=0.05,
                  last_accessed=(NOW - timedelta(days=1)).isoformat())
        assert demote_candidates([f], NOW) == []

    def test_anchor_kept(self):
        f = _fact("a", "t", confidence=0.01, anchor_type="origin")
        assert demote_candidates([f], NOW) == []

    def test_constants(self):
        assert DEMOTE_CONFIDENCE_MAX == 0.10
        assert DEMOTE_AGE_DAYS_MIN == 60


class TestFadeCandidates:
    def test_old_untouched_fades(self):
        f = _fact("a", "t", created_days_ago=60)
        f["base_confidence"] = 0.8
        out = fade_candidates([f], NOW, fade_days=30)
        assert len(out) == 1
        assert abs(out[0][1] - 0.64) < 1e-9

    def test_recent_or_accessed_kept(self):
        recent = _fact("a", "t", created_days_ago=5)
        accessed = _fact("b", "t", created_days_ago=60, access_count=3)
        assert fade_candidates([recent, accessed], NOW, fade_days=30) == []


class TestArchiveCandidates:
    def test_decay_sweep_old_low_conf(self):
        f = _fact("a", "t", created_days_ago=90)
        out = decay_archive_candidates([f], {"a": 0.01}, NOW,
                                       threshold=0.05, min_age_days=60)
        assert len(out) == 1
        assert "age=" in out[0][1]

    def test_decay_sweep_ultra_low_any_age(self):
        f = _fact("a", "t", created_days_ago=5)
        out = decay_archive_candidates([f], {"a": 0.01}, NOW,
                                       threshold=0.05, min_age_days=60)
        assert len(out) == 1
        assert "age=" not in out[0][1]

    def test_decay_sweep_keeps_accessed(self):
        f = _fact("a", "t", created_days_ago=90, access_count=2)
        assert decay_archive_candidates([f], {"a": 0.01}, NOW) == []

    def test_cold_storage_dormant(self):
        f = _fact("a", "t", created_days_ago=120)
        out = cold_storage_candidates([f], {"a": 0.1}, NOW)
        assert len(out) == 1

    def test_cold_storage_keeps_young_or_confident(self):
        young = _fact("a", "t", created_days_ago=30)
        confident = _fact("b", "t", created_days_ago=120)
        out = cold_storage_candidates(
            [young, confident], {"a": 0.1, "b": 0.9}, NOW,
        )
        assert out == []


# ── LeaderLock ────────────────────────────────────────────────────────────


class TestLeaderLock:
    def _db(self, tmp_path):
        path = tmp_path / "unified.db"
        init_unified_db(str(path)).close()
        return str(path)

    def test_two_claimants_one_wins(self, tmp_path):
        db = self._db(tmp_path)
        l1 = LeaderLock(db, "test_leader", "worker-1")
        l2 = LeaderLock(db, "test_leader", "worker-2")
        results = []

        def claim(lock):
            results.append(lock.claim_or_refresh(ttl_seconds=90))

        threads = [threading.Thread(target=claim, args=(l,)) for l in (l1, l2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, True]

    def test_holder_refreshes(self, tmp_path):
        db = self._db(tmp_path)
        lock = LeaderLock(db, "test_leader", "worker-1")
        assert lock.claim_or_refresh(90) is True
        assert lock.claim_or_refresh(90) is True

    def test_expiry_hands_over(self, tmp_path):
        db = self._db(tmp_path)
        l1 = LeaderLock(db, "test_leader", "worker-1")
        l2 = LeaderLock(db, "test_leader", "worker-2")
        assert l1.claim_or_refresh(90) is True
        assert l2.claim_or_refresh(90) is False
        # TTL 0 → l1's heartbeat is immediately stale
        assert l2.claim_or_refresh(0) is True
        # ...and now l1 is locked out under a sane TTL
        assert l1.claim_or_refresh(90) is False

    def test_keys_are_independent(self, tmp_path):
        db = self._db(tmp_path)
        a = LeaderLock(db, "engine_a_leader", "worker-1")
        b = LeaderLock(db, "engine_b_leader", "worker-2")
        assert a.claim_or_refresh(90) is True
        assert b.claim_or_refresh(90) is True
