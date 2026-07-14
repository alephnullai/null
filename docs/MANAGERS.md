# Authoring a Personality Manager

Null ships **zero specific managers**. Its value as a product is the
substrate (memory + Hypnos + outreach + Nebula) and the *framework*
(`Manager` ABC + `Reasoner` Protocol) for user-defined personalities.

This guide walks through building a manager of your own.

## Concept

A **personality** (also called a *manager*) is a specialized agent that:

1. Observes a single domain (inbox, calendar, jobs, code, trades…).
2. Reasons about what it sees via a swappable `Reasoner` backend.
3. Records findings in its own memory namespace (`personality='<name>'`).
4. Surfaces signals to you via the existing outreach pipeline.
5. Answers digest queries ("what did you notice this week?").

## Directory layout

Each personality lives in its own directory under `~/.null/personalities/`:

```
~/.null/personalities/
  <name>/
    manager.py         # your code — class <Name>(Manager): ...
    identity.json      # scope, reports_to, color, anti_patterns
    preferences.json   # user-editable preferences your manager reads
    test_manager.py    # optional — your tests
```

The directory name is the personality's identifier (`<name>`). Lowercase,
hyphen-free for CLI cleanliness.

## Minimum viable manager

Sync and async are both supported. Pick whichever fits the work your
manager does — sync for rule-based scoring, async if your reasoner does
network I/O (LLM calls, web requests, etc.). The CLI runner handles
either signature transparently.

**Sync (recommended for v1 — matches the default `RuleReasoner`):**

```python
# ~/.null/personalities/mymanager/manager.py
from null_memory.managers import Manager, TickResult


class MyManager(Manager):
    name = "mymanager"
    scope = "What this manager does in one sentence."
    outreach_kind = "mymanager_match"   # tag used in outreaches.log

    def tick(self, items=None):
        """Observe + reason + maybe fire outreach. Return a TickResult."""
        return TickResult(manager=self.name, observed_count=0)

    def digest(self, since=None):
        """One-paragraph summary for Atlas briefing."""
        return "Nothing to report."
```

**Async (when you have an LLM-backed reasoner or other I/O):**

```python
class MyAsyncManager(Manager):
    name = "myasync"

    async def tick(self, items=None):
        result = await self._resolve_async(self.reasoner.score(items[0], ctx))
        return TickResult(manager=self.name)

    async def digest(self, since=None):
        return await self._resolve_async(
            self.reasoner.digest(items, self._load_context())
        )
```

Drop `identity.json` next to it:

```json
{
  "name": "MyManager",
  "color": "#abcdef",
  "scope": "What this manager does.",
  "reports_to": "atlas",
  "outreach_kind": "mymanager_match",
  "anti_patterns": [
    "scope creep beyond what I was built for"
  ]
}
```

That's it. Null discovers it automatically.

## CLI

```
null personality list                   # every discovered personality
null personality describe <name>        # dump identity.json
null personality digest <name>          # call digest()
null personality tick <name> items.json # feed items to tick()
```

## The Reasoner Protocol — v1 vs v2

Your manager reasons through `self.reasoner`. Null ships `RuleReasoner`
as v1 — pure rule-based scoring, zero LLM cost, deterministic. When you
graduate to v2 (your own LLM per personality — local or API), you swap
in a `Reasoner` implementation that calls the model. The manager itself
never changes.

### RuleReasoner rubric pattern

```python
def _rubric(self, item: dict, context: ReasonerContext) -> dict:
    matched, conflicts, hard_failed = [], [], []
    continuous: dict[str, float] = {}

    # Example: hard constraint
    if item["company"].lower() in context.preferences.get("excluded", []):
        hard_failed.append("excluded_company")

    # Example: soft match
    if context.preferences.get("remote_only") and item["remote"]:
        matched.append("remote")

    return {
        "matched": matched,
        "conflicts": conflicts,
        "hard_failed": hard_failed,
        "continuous": continuous,
        "weights": {},
        "base": 0.3,
    }


def score_item(self, item):
    ctx = self._load_context()
    # _resolve handles both sync and async reasoners — sync RuleReasoner
    # returns ScoreResult directly; an async LLM reasoner returns a
    # coroutine that _resolve runs to completion.
    return self._resolve(self.reasoner.score_with_rubric(item, ctx, self._rubric))
```

See the `Reasoner` base class in `null_memory.managers.base` for the
full protocol.

## Sync vs async — when to pick which

**Reasoner side (the scoring backend):**
- `RuleReasoner` — sync. Zero I/O, deterministic scoring.
- LLM reasoner — async. Network call → use `async def`.
- Both implement the same `Reasoner` Protocol — its return types are
  declared as `T | Awaitable[T]` so either signature satisfies it.

**Manager side (your code):**
- `Manager._resolve(value)` — call from a sync method. If `value` is
  already a `ScoreResult`, returns it; if it's a coroutine, runs it
  to completion. Refuses to run if there's an active event loop —
  that's an "async context calling sync helper" anti-pattern.
- `Manager._resolve_async(value)` — call from an `async def` method.
  Awaits if needed, returns directly otherwise.

The four combinations all interoperate:

| Reasoner | Manager  | Mechanism                              |
|----------|----------|----------------------------------------|
| sync     | sync     | direct return — no resolve needed      |
| async    | sync     | `self._resolve(...)` runs the coroutine|
| sync     | async    | `self._resolve_async(...)` returns directly |
| async    | async    | `await self._resolve_async(...)`       |

## Memory namespace

When your manager calls `self.memory.learn(...)`, `self.memory.mistake(...)`,
etc., writes are tagged with your personality name automatically. Your
observations don't pollute Atlas's memory or other managers'.

```python
self.memory.learn(
    "Observed interesting thing",
    confidence=0.8,
    project="mymanager",   # namespace tag
    source=self.name,
)
```

## Nebula colors

Set `"color": "#rrggbb"` in `identity.json`. Null's palette loader picks
it up at startup and Nebula renders your personality's points in that hue.

## Tests

Drop `test_manager.py` next to `manager.py`. Nothing in Null's test
suite runs your personal tests — but your own `pytest ~/.null/personalities`
invocation will.

## Naming suggestions

Pick a short, evocative name. Greek/mythological names fit the
multiverse theme but anything works. Examples:

- `argus` — vigilant watcher (scanning, pattern-matching)
- `hermes` — messenger (inbox, notifications)
- `kairos` — opportune moment (calendar, timing)
- `mnemosyne` — memory (long-term pattern recall)

## Good manager design

- **One domain per manager.** Scope creep is the failure mode.
- **Conservative thresholds.** Miss signals rather than flood.
- **Explicit `NEEDS_INPUT`.** Where preferences can't be safely defaulted,
  flag gaps in `digest()` so the user refines them.
- **No outbound network surprises.** Read-only by default. Anything that
  writes externally should require explicit opt-in.
- **Fail gracefully.** A manager that crashes shouldn't take down Null.
