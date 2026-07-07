"""Zero-discipline middleware — wraps any agent loop with automatic memory.

The agent doesn't call Null. Null wraps the agent.

Usage:
    from null_memory.middleware import NullMiddleware

    mw = NullMiddleware(project="my_project")

    # In your agent loop:
    while True:
        user_msg = get_user_message()
        context = mw.before_turn(user_msg)      # auto-recall
        response = agent.respond(user_msg, context)
        mw.after_turn(summary=extract_summary(response))  # auto-observe

    mw.end_session(summary="done")               # auto-close + git commit

That's it. The agent never has to call null_remember, null_recall,
null_checkpoint, or the CLI maintenance commands. The middleware does
all of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from null_memory.agent import AgentMemory


@dataclass
class MiddlewareConfig:
    """Tunable parameters for the middleware."""
    checkpoint_interval: int = 10       # auto-checkpoint every N turns
    consolidate_threshold: int = 50     # auto-consolidate after N new facts in session
    recall_limit: int = 5              # max facts returned by before_turn
    auto_recall: bool = True           # enable auto-recall on before_turn
    auto_observe: bool = True          # enable auto-observe on after_turn
    auto_checkpoint: bool = True       # enable auto-checkpoint
    auto_consolidate: bool = True      # enable auto-consolidation


class NullMiddleware:
    """Wraps any agent loop with automatic memory operations.

    Handles:
    - Auto-session start on first before_turn
    - Auto-recall relevant context for each user message
    - Auto-observe assistant summaries
    - Auto-checkpoint every N turns
    - Auto-consolidate when fact threshold is hit
    - Auto-session close with git commit

    Framework-agnostic. Works with any Python agent.
    """

    def __init__(
        self,
        project: str = "global",
        agent_dir: str | None = None,
        git_cwd: str | None = None,
        config: MiddlewareConfig | None = None,
    ) -> None:
        self.project = project
        self._agent_dir = agent_dir or os.path.join(os.path.expanduser("~"), ".null")
        self._git_cwd = git_cwd
        self.config = config or MiddlewareConfig()
        self._memory: AgentMemory | None = None
        self._started: bool = False
        self._turn_count: int = 0
        self._session_facts_at_start: int = 0
        self._crashed_session: Any = None

    @property
    def memory(self) -> AgentMemory:
        if self._memory is None:
            self._memory = AgentMemory.load(self._agent_dir)
        return self._memory

    def _ensure_started(self) -> None:
        """Auto-start session on first use."""
        if self._started:
            return
        # Check for crash
        if self.memory._prior_crash is not None:
            self._crashed_session = self.memory._prior_crash
        self.memory.start_session(project=self.project, git_cwd=self._git_cwd)
        self._session_facts_at_start = len(self.memory.knowledge)
        self._started = True

    # ── Turn lifecycle ──

    def before_turn(self, user_message: str) -> str:
        """Call at the start of each turn with the user's message.

        Returns formatted relevant context string (may be empty).
        Automatically:
        - Starts session if not started
        - Recalls relevant facts
        - Touches the session timestamp

        The returned string can be prepended to the system prompt
        or injected into the agent's context window.
        """
        self._ensure_started()
        self._turn_count += 1

        if self.memory._current_session is not None:
            self.memory._current_session.touch()

        if not self.config.auto_recall or not user_message.strip():
            return ""

        results = self.memory.recall(
            user_message,
            project=self.project,
            limit=self.config.recall_limit,
            include_mistakes=True,
        )
        if not results:
            return ""

        lines = ["[Null Memory — relevant context]"]
        for entry in results:
            eff = self.memory.effective_confidence(entry)
            entry_type = entry.get("_type", "fact")
            if entry_type == "mistake":
                lines.append(f"  !! [{eff:.0%}] MISTAKE: {entry['mistake'][:120]}")
            else:
                lines.append(f"  [{eff:.0%}] {entry['fact'][:120]}")

        # Include crash warning on first turn
        if self._turn_count == 1 and self._crashed_session is not None:
            cs = self._crashed_session
            lines.insert(1, f"  WARNING: Previous session CRASHED "
                           f"(started {cs.started_at[:19]}, {cs.facts_created} facts saved)")
            self._crashed_session = None

        return "\n".join(lines)

    def after_turn(self, summary: str = "",
                   tool_calls: list[str] | None = None) -> None:
        """Call at the end of each turn.

        Automatically:
        - Observes the summary (if provided)
        - Observes tool calls (if provided)
        - Checkpoints every N turns
        - Triggers consolidation when threshold is hit

        Args:
            summary: One-line description of what happened this turn.
            tool_calls: Optional list of tool call descriptions
                       (e.g. ["Write src/main.py", "Bash: pytest"])
        """
        self._ensure_started()

        # Auto-observe
        if self.config.auto_observe:
            if summary:
                self.memory.observe(summary, project=self.project)
            elif tool_calls:
                # Synthesize observation from tool calls
                tools_summary = "; ".join(tc[:60] for tc in tool_calls[:5])
                self.memory.observe(
                    f"Tools used: {tools_summary}",
                    project=self.project,
                )

        # Auto-checkpoint
        if (self.config.auto_checkpoint
                and self._turn_count > 0
                and self._turn_count % self.config.checkpoint_interval == 0):
            self.memory.checkpoint(
                note=f"auto-checkpoint turn {self._turn_count}",
            )

        # Auto-consolidate when fact threshold is hit
        if self.config.auto_consolidate:
            new_facts = len(self.memory.knowledge) - self._session_facts_at_start
            if new_facts > 0 and new_facts % self.config.consolidate_threshold == 0:
                self.memory.consolidate()

    # ── Session lifecycle ──

    def end_session(self, summary: str = "", went_well: str = "",
                    missed: str = "", do_differently: str = "",
                    decisions_made: list[str] | None = None,
                    lessons: list[str] | None = None) -> dict:
        """Close the session: debrief + reflect + consolidate + sync + git commit.

        Always runs consolidation before closing.
        """
        self._ensure_started()

        # Final consolidation before close
        if self.config.auto_consolidate and len(self.memory.knowledge) > 100:
            self.memory.consolidate()

        result = self.memory.close(
            summary=summary,
            went_well=went_well,
            missed=missed,
            do_differently=do_differently,
            decisions_made=decisions_made,
            lessons=lessons,
            project=self.project,
        )
        self._started = False
        return result

    # ── Direct access (for when the agent does want to be explicit) ──

    def learn(self, fact: str, confidence: float = 0.8) -> dict:
        """Explicitly learn a fact."""
        self._ensure_started()
        return self.memory.learn(fact, confidence, project=self.project, source="explicit")

    def decide(self, decision: str, reasoning: str) -> dict:
        """Log a decision."""
        self._ensure_started()
        return self.memory.decide(decision, reasoning, project=self.project)

    def mistake(self, what: str, why: str) -> dict:
        """Record a mistake."""
        self._ensure_started()
        return self.memory.mistake(what, why, project=self.project)

    def recall(self, query: str, limit: int = 10,
               since: str | None = None) -> list[dict]:
        """Explicit recall (in addition to auto-recall in before_turn)."""
        self._ensure_started()
        return self.memory.recall(query, project=self.project,
                                  limit=limit, since=since)

    # ── Introspection ──

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def fact_count(self) -> int:
        return len(self.memory.knowledge)

    @property
    def name(self) -> str:
        return self.memory.name

    def briefing(self) -> str:
        """Get a session briefing."""
        self._ensure_started()
        return self.memory.briefing(project=self.project)
