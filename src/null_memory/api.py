"""Zero-discipline Python API for Null Memory.

Usage:
    from null_memory.api import memory

    memory.start("my_project")              # load identity, start session
    context = memory.before_turn("user msg") # auto-recall relevant facts
    memory.learn("important fact", 0.9)      # store knowledge
    memory.after_turn("did X and Y")         # auto-observe
    memory.end(summary="shipped feature")    # debrief + git commit

No MCP server required. No CLI shelling. Just import and use.
"""

from __future__ import annotations

import os
import threading

from null_memory.agent import AgentMemory


class NullAPI:
    """Singleton Python API for Null Memory.

    Manages a single AgentMemory instance with automatic session lifecycle.
    Thread-safe lazy initialization.
    """

    def __init__(self, agent_dir: str | None = None) -> None:
        self._agent_dir = agent_dir or os.path.join(os.path.expanduser("~"), ".null")
        self._memory: AgentMemory | None = None
        self._started: bool = False
        self._lock = threading.Lock()

    @property
    def mem(self) -> AgentMemory:
        """Lazy-load the AgentMemory instance."""
        if self._memory is None:
            with self._lock:
                if self._memory is None:
                    self._memory = AgentMemory.load(self._agent_dir)
        return self._memory

    @property
    def is_started(self) -> bool:
        return self._started

    # ── Session lifecycle ──

    def start(self, project: str = "global", git_cwd: str | None = None) -> str:
        """Start a session. Safe to call multiple times (idempotent).

        Returns identity summary.
        """
        if self._started:
            return self.mem.format_identity()
        self.mem.start_session(project=project, git_cwd=git_cwd)
        self._started = True
        return self.mem.format_identity()

    def _ensure_started(self, project: str = "global") -> None:
        """Auto-start session on first use."""
        if not self._started:
            self.start(project=project)

    def end(self, summary: str = "", went_well: str = "",
            missed: str = "", do_differently: str = "",
            decisions_made: list[str] | None = None,
            lessons: list[str] | None = None,
            identity_updates: dict[str, str] | None = None,
            project: str = "global") -> dict:
        """Close the session: debrief + reflect + sync + git commit.

        Returns dict with debrief results and commit status.
        """
        self._ensure_started(project)
        result = self.mem.close(
            summary=summary, went_well=went_well,
            missed=missed, do_differently=do_differently,
            decisions_made=decisions_made, lessons=lessons,
            identity_updates=identity_updates, project=project,
        )
        self._started = False
        return result

    # ── Turn lifecycle (the habit-free methods) ──

    def before_turn(self, user_message: str, project: str = "global",
                    limit: int = 5) -> str:
        """Call at the start of each turn with the user's message.

        Auto-recalls relevant knowledge and returns formatted context.
        Use this to prepend to your system prompt or inject into context.
        """
        self._ensure_started(project)
        if self.mem._current_session is not None:
            self.mem._current_session.touch()

        results = self.mem.recall(user_message, project=project, limit=limit,
                                  include_mistakes=True)
        if not results:
            return ""

        lines = ["[Null Memory — relevant context]"]
        for entry in results:
            eff = self.mem.effective_confidence(entry)
            entry_type = entry.get("_type", "fact")
            if entry_type == "mistake":
                lines.append(f"  !! [{eff:.0%}] MISTAKE: {entry['mistake'][:120]}")
            else:
                lines.append(f"  [{eff:.0%}] {entry['fact'][:120]}")
        return "\n".join(lines)

    def after_turn(self, summary: str = "", project: str = "global") -> None:
        """Call at the end of each turn with a summary of what happened.

        Auto-observes the summary. If no summary provided, just increments
        the turn counter for checkpoint tracking.
        """
        self._ensure_started(project)
        if summary:
            self.mem.observe(summary, project=project)

        # Auto-checkpoint every 10 turns
        if self.mem._turn_count > 0 and self.mem._turn_count % 10 == 0:
            self.mem.checkpoint(note=f"auto-checkpoint at turn {self.mem._turn_count}")

    # ── Knowledge (direct access) ──

    def learn(self, fact: str, confidence: float = 0.8,
              project: str = "global", replaces: str | None = None) -> dict:
        """Store a fact. Deduplicates automatically."""
        self._ensure_started(project)
        return self.mem.learn(fact, confidence, project=project,
                              source="explicit", replaces=replaces)

    def recall(self, query: str, project: str | None = None,
               limit: int = 10, since: str | None = None) -> list[dict]:
        """Search knowledge by query."""
        self._ensure_started(project or "global")
        return self.mem.recall(query, project=project, limit=limit, since=since)

    def decide(self, decision: str, reasoning: str,
               project: str = "global") -> dict:
        """Log a decision with reasoning."""
        self._ensure_started(project)
        return self.mem.decide(decision, reasoning, project=project)

    def mistake(self, what: str, why: str,
                project: str = "global") -> dict:
        """Record a mistake."""
        self._ensure_started(project)
        return self.mem.mistake(what, why, project=project)

    def verify(self, query: str) -> dict | None:
        """Mark the best-matching fact as verified."""
        self._ensure_started()
        return self.mem.verify_fact(query)

    # ── Org exchange (issue #20 Phase B — docs/EXCHANGE.md) ──

    def exchange_post(self, kind: str, data: dict, scope: str = "org") -> dict:
        """Post a typed event to this seat's outbound exchange stream
        (commits + pushes the exchange clone, rings the doorbell peers).
        Kinds: report.session, repo.push, broadcast, claim.acquire,
        claim.release, query.ask, query.answer, directive."""
        from null_memory.exchange import ExchangeClient
        return ExchangeClient(self.mem).post(kind, data, scope=scope)

    def exchange_announce_push(self, repo_cwd: str = ".",
                               summary: str = "") -> dict:
        """Post repo.push for a code repo you just pushed — peers see a
        'pull recommended' line, never the code itself."""
        from null_memory.exchange import ExchangeClient
        return ExchangeClient(self.mem).announce_push(
            os.path.abspath(repo_cwd), summary=summary)

    # ── Introspection ──

    def briefing(self, project: str | None = None) -> str:
        """Get a session briefing."""
        self._ensure_started(project or "global")
        return self.mem.briefing(project=project)

    def identity(self) -> str:
        """Get identity summary."""
        return self.mem.format_identity()

    def status(self) -> str:
        """Get memory stats."""
        return self.mem.status()

    @property
    def fact_count(self) -> int:
        return len(self.mem.knowledge)

    @property
    def name(self) -> str:
        return self.mem.name


# ── Global singleton ──
# Import this: from null_memory.api import memory

memory = NullAPI()
