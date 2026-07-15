# Null Agent Memory

Null is your persistent memory. Use it to remember things across sessions.

## Session Start

Run at the beginning of every session:

```bash
null status
```

This shows your name, fact count, mistakes, reflections, and memory status.

## Session End

Before signing off:

```bash
null reflect "what went well" "what was missed" "what to do differently"
```

Or via MCP: call `null_close` — it performs debrief + reflect + sync +
git commit atomically.

## Commands

### Core
```bash
null status                          # Show memory stats
null export                          # Show all knowledge as JSON
null init                            # Name your agent + set traits (first run)
```

### Negative knowledge
```bash
null mistake "what went wrong" "why it happened" --project aleph
```

### Self-assessment
```bash
null reflect "went well" "missed" "do differently" --project aleph
```

### Garbage collection
```bash
null gc                              # Clean old/duplicate facts
null gc --max-facts 3000             # Custom threshold
```

### From the MCP tools (if available)
If null MCP tools are connected, prefer those. Otherwise use the CLI above.

MCP tools (15 total): null_remember (kind=observe|learn|decide|mistake|wonder|contradict),
null_recall, null_briefing, null_close, null_checkpoint,
null_verify (mode=fact|claim|identity), null_identity, null_status,
null_context, null_outcome, null_anchor, null_catchup,
null_exemplar (action=search|add), null_forget,
null_multiverse (action=list|broadcast|recall|wakeup).

Maintenance is CLI-only: null gc, null consolidate, null doctor,
null calibrate, null evaluate, null export, null import, null name,
null probe add, null outreach send.

## Important

- You are the agent this store belongs to — check your name with `null status`
  (set it with `null init`). Greet the user by that name at session start.
- You have persistent memory. Check `null status` to see what you know.
- Record important learnings via null MCP tools or CLI.
- When something goes wrong, call `null_remember` with `kind=mistake` to prevent repeating errors.
- At session end, call `null_close` (debrief + reflect + sync in one call).
- Recall now uses word expansion: "database" finds "Postgres", "Redis", etc.
- Mistakes and reflections are NEVER garbage collected.
