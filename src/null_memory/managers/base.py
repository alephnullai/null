"""Manager base types — the contract every multiverse manager implements.

The Reasoner protocol is the v1→v2 swap boundary. v1 implementations
are rule-based (RuleReasoner — sync); v2 implementations will be
per-personality LLMs (likely async, local or API).

Both sync AND async are first-class. Reasoner methods may return either
a ``ScoreResult`` directly or an ``Awaitable[ScoreResult]``. Manager
helpers (``_resolve`` for sync contexts, ``_resolve_async`` for async)
collapse either return shape to the value type.

A user-defined Manager subclass may override ``tick`` / ``digest`` with
either ``def`` or ``async def``. The CLI runner (``_run_maybe_async``)
handles either shape transparently.

Net effect: drop in a sync RuleReasoner and a sync Manager and it works.
Drop in an async LLMReasoner and a sync Manager and it works (the
Manager's ``_resolve`` handles the await internally). Drop in a sync
Reasoner and an async Manager and that works too. The four combinations
all interoperate.
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Protocol, TypeVar, Union


T = TypeVar("T")


@dataclass
class ScoreResult:
    """Output of scoring a single item against preferences/context.

    score: 0-1, where 1 is 'strong fit', 0 is 'reject'
    rationale: human-readable why, shown to Pete in outreach
    matched: list of constraint/preference names that matched
    conflicts: list that actively conflicted (lowering the score)
    hard_constraint_failed: if True, score must be clamped to 0 regardless
    """
    score: float
    rationale: str
    matched: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    hard_constraint_failed: bool = False


@dataclass
class TickResult:
    """Summary of one manager tick — what was observed + what fired."""
    manager: str
    observed_count: int = 0
    scored_count: int = 0
    flagged_count: int = 0
    fired_outreach: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class ReasonerContext:
    """Everything a Reasoner needs to score/digest/compose.

    Pulled by the Manager from its memory before calling the Reasoner,
    so the Reasoner has no direct DB coupling — it's pure function of
    (item, context) → result. That keeps v2 LLM prompts self-contained."""
    manager_name: str
    preferences: dict[str, Any]          # Pete's preferences for this domain
    anchors: list[dict]                  # relevant emotional anchors
    recent_memories: list[dict] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


# Reasoner methods may be sync OR async — managers handle either via
# the Manager._resolve helpers. The union return type signals this in
# the type system without forcing implementations to one style.
class Reasoner(Protocol):
    """Pluggable reasoning backend. v1: rule-based (sync). v2:
    LLM-per-manager (typically async). Either is valid."""

    def score(
        self, item: dict, context: ReasonerContext,
    ) -> Union[ScoreResult, Awaitable[ScoreResult]]:
        """Score a single domain item (job posting, email, meeting) 0-1."""
        ...

    def digest(
        self, items: list[dict], context: ReasonerContext,
    ) -> Union[str, Awaitable[str]]:
        """Compose a natural-language digest of recent items."""
        ...

    def compose(
        self, subject: str, body_context: dict, context: ReasonerContext,
    ) -> Union[tuple[str, str], Awaitable[tuple[str, str]]]:
        """Compose an outreach (subject, body) for a high-score item.
        body_context carries the item + ScoreResult + memory refs."""
        ...


class Manager(ABC):
    """Base class for every multiverse manager.

    Concrete managers (Argus, Hermes, Kairos, Mnemosyne, Cybil) subclass
    this and implement observe/tick specifics. Memory namespacing
    (personality=<name>) is handled here so subclasses focus on domain
    logic, not plumbing.

    ``tick`` / ``digest`` are declared as sync ``def`` for backward
    compatibility with existing user managers. Subclasses MAY override
    with ``async def``; the CLI runner handles either shape."""

    name: str = "manager"          # override in subclass
    scope: str = ""                # natural-language scope from identity.json
    outreach_kind: str = ""        # trigger kind emitted by this manager

    def __init__(self, memory: Any, reasoner: Reasoner | None = None):
        self.memory = memory       # AgentMemory instance, personality=self.name
        # Default to RuleReasoner so user managers work out of the box
        # without having to import it. v2 swap: pass your own LLM reasoner.
        if reasoner is None:
            from null_memory.managers.reasoners import RuleReasoner
            reasoner = RuleReasoner()
        self.reasoner = reasoner

    @abstractmethod
    def tick(self, items: list[dict] | None = None) -> TickResult:
        """Run one observation cycle. If items is provided, score those;
        otherwise fetch from the domain source (when the manager owns
        its poller; e.g., Phase 7 launchd-wrapped subprocess).

        May be overridden as ``async def`` — the CLI runner handles either."""
        raise NotImplementedError

    @abstractmethod
    def digest(self, since: datetime | None = None) -> str:
        """What this manager has seen / done since a given time. Used
        by Atlas briefing to summarize manager activity.

        May be overridden as ``async def`` — the CLI runner handles either."""
        raise NotImplementedError

    # ── Sync/async resolution helpers ─────────────────────────────────
    #
    # Reasoner methods may return either a value or an awaitable. These
    # helpers let a sync Manager call any Reasoner transparently. An
    # async Manager should use _resolve_async to keep cooperative
    # scheduling intact.

    @staticmethod
    def _resolve(value: Union[T, Awaitable[T]]) -> T:
        """Resolve a sync-or-awaitable Reasoner return from a SYNC context.

        If the value is a coroutine/awaitable, run it to completion using
        a fresh event loop. Falls back to ``asyncio.run`` when no loop
        is active. Raises if called from inside a running event loop —
        sync Managers must not be invoked from async code (use
        ``_resolve_async`` instead)."""
        if not inspect.isawaitable(value):
            return value
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                raise RuntimeError(
                    "Manager._resolve called from inside a running event "
                    "loop — use _resolve_async instead."
                )
        except RuntimeError as e:
            # No event loop bound to this thread — that's fine, asyncio.run
            # will create one. The "inside a running loop" case above
            # re-raises with a clearer message.
            if "running event loop" in str(e):
                raise
        return asyncio.run(value)  # type: ignore[arg-type]

    @staticmethod
    async def _resolve_async(value: Union[T, Awaitable[T]]) -> T:
        """Resolve a sync-or-awaitable Reasoner return from an ASYNC context."""
        if inspect.isawaitable(value):
            return await value
        return value

    # ── Shared plumbing ────────────────────────────────────────────────

    def _load_context(self) -> ReasonerContext:
        """Build the ReasonerContext from this manager's memory scope."""
        prefs = self.load_preferences()
        anchors = self._load_anchors()
        return ReasonerContext(
            manager_name=self.name,
            preferences=prefs,
            anchors=anchors,
        )

    def _load_anchors(self) -> list[dict]:
        """Load emotionally-significant facts relevant to this manager."""
        try:
            rows = self.memory.db.conn.execute(
                """SELECT id, fact, anchor_type, anchor_note
                   FROM facts
                   WHERE anchor_type IS NOT NULL
                     AND archived = 0 AND forgotten = 0
                     AND superseded_by IS NULL"""
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def load_preferences(self) -> dict[str, Any]:
        """Override to read preferences from personality dir or memory."""
        return {}
