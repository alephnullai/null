"""Reasoner tests — verify both sync and async reasoners work, and that
Manager._resolve / _resolve_async cleanly handle either."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from null_memory.managers import (
    Manager,
    ReasonerContext,
    RuleReasoner,
    ScoreResult,
    TickResult,
)


@pytest.fixture
def context() -> ReasonerContext:
    return ReasonerContext(
        manager_name="test",
        preferences={},
        anchors=[],
    )


# ── RuleReasoner is sync v1 — returns values directly ───────────────────


def test_rule_reasoner_score_returns_sync(context):
    result = RuleReasoner().score({}, context)
    assert isinstance(result, ScoreResult)
    assert result.score == 0.5
    assert not inspect.isawaitable(result)


def test_rule_reasoner_score_with_rubric_sync(context):
    def rubric(_item, _context):
        return {
            "base": 0.2,
            "matched": ["remote"],
            "conflicts": ["low_signal"],
            "continuous": {"fit": 0.5},
            "weights": {"fit": 0.4},
        }
    result = RuleReasoner().score_with_rubric({}, context, rubric)
    assert isinstance(result, ScoreResult)
    assert result.score == pytest.approx(0.35)
    assert result.matched == ["remote"]
    assert result.conflicts == ["low_signal"]


def test_rule_reasoner_digest_and_compose_sync(context):
    reasoner = RuleReasoner()
    digest = reasoner.digest(
        [{"title": "High fit", "score": 0.9}, {"title": "Low fit", "score": 0.1}],
        context,
    )
    subject, body = reasoner.compose(
        "Subject", {"default_body": "Body"}, context,
    )
    assert "2 observations" in digest
    assert "[0.90] High fit" in digest
    assert (subject, body) == ("Subject", "Body")


# ── Async reasoner also satisfies the Reasoner protocol ────────────────


class _AsyncEchoReasoner:
    """Minimal async reasoner — proves the Protocol accepts async impls."""
    async def score(self, item, context):
        await asyncio.sleep(0)  # actually yield
        return ScoreResult(score=0.7, rationale="async ok")

    async def digest(self, items, context):
        await asyncio.sleep(0)
        return f"async digest: {len(items)}"

    async def compose(self, subject, body_context, context):
        await asyncio.sleep(0)
        return subject, "async body"


def test_async_reasoner_returns_awaitable(context):
    coro = _AsyncEchoReasoner().score({}, context)
    assert inspect.isawaitable(coro)
    result = asyncio.run(coro)
    assert isinstance(result, ScoreResult)
    assert result.score == 0.7


# ── Manager._resolve unifies both reasoner shapes ──────────────────────


class _DummyManager(Manager):
    name = "dummy"

    def tick(self, items=None):
        return TickResult(manager=self.name)

    def digest(self, since=None):
        return "dummy"


class _Mem:
    """Minimal memory stand-in so Manager.__init__ doesn't blow up."""
    pass


def test_manager_resolve_passes_value_through(context):
    m = _DummyManager(_Mem(), reasoner=RuleReasoner())
    result = m._resolve(m.reasoner.score({}, context))  # sync return
    assert isinstance(result, ScoreResult)
    assert result.score == 0.5


def test_manager_resolve_runs_awaitable(context):
    m = _DummyManager(_Mem(), reasoner=_AsyncEchoReasoner())
    result = m._resolve(m.reasoner.score({}, context))  # awaitable return
    assert isinstance(result, ScoreResult)
    assert result.score == 0.7


def test_manager_resolve_async_handles_both():
    m = _DummyManager(_Mem(), reasoner=RuleReasoner())
    ctx = ReasonerContext(manager_name="t", preferences={}, anchors=[])

    async def go():
        sync_val = m.reasoner.score({}, ctx)
        async_val = _AsyncEchoReasoner().score({}, ctx)
        return await m._resolve_async(sync_val), await m._resolve_async(async_val)

    sync_r, async_r = asyncio.run(go())
    assert isinstance(sync_r, ScoreResult)
    assert isinstance(async_r, ScoreResult)
    assert sync_r.score == 0.5
    assert async_r.score == 0.7


def test_manager_resolve_rejects_call_inside_running_loop():
    """A sync Manager called from inside an active event loop should
    refuse rather than deadlocking. Forces the user to use _resolve_async."""
    m = _DummyManager(_Mem(), reasoner=_AsyncEchoReasoner())
    ctx = ReasonerContext(manager_name="t", preferences={}, anchors=[])

    async def runner():
        with pytest.raises(RuntimeError, match="_resolve_async"):
            m._resolve(m.reasoner.score({}, ctx))

    asyncio.run(runner())
