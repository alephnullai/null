# Null

> **Persistent agent memory. Your AI remembers.**

Null gives AI agents persistent memory across sessions. Working style, decisions, learned facts, and domain knowledge survive between conversations. Clone an instance's knowledge to another project. Share a brain across repos.

**By [Aleph Null LLC](https://alephnull.ai) — Patent Pending**

## Part of the Aleph Null suite

Three local-first tools that share one philosophy — honest, receipts over vibes,
UNKNOWN before a guess:

- **[Null](https://github.com/alephnullai/null)** — *remembers who you are.* Persistent agent memory (this repo).
- **[Aleph](https://github.com/alephnullai/Aleph)** — *knows the code.* Semantic codebase intelligence for your agent.
- **Tank** — *knows what's left in the tank.* Usage-limit intelligence: meters agentic consumption and gates automation before it burns your budget. **Coming soon.**

All free and open source under Apache-2.0.

## Install

From source (recommended until the PyPI release lands):
```bash
git clone https://github.com/alephnullai/null
cd null
pip install .
```

Or, once published to PyPI:
```bash
pip install null-memory
```

### Verify installation

```bash
null status
```

If installed correctly, you'll see:
```
[Null] Agent — Memory Status
  Facts: 0 | Mistakes: 0 | Decisions: 0
```

### Production vs development installs

- **Production** (your live agent's memory): install a **fixed, reviewed copy**
  — from source at a tagged release (`git checkout vX.Y.Z && pip install .`), or
  a released wheel once the PyPI package is live. Live hooks and MCP servers
  import whatever code is installed, so pin them to tagged code, not a moving
  checkout.
- **Development** (hacking on Null itself): use a **separate venv** with an
  editable install (`pip install -e .`). Never point your live agent at an
  editable checkout: every working-tree edit becomes live behavior
  immediately, mid-session. `null doctor` detects this configuration and
  warns when the checkout has uncommitted changes ("live memory is running
  uncommitted working-tree code").

## Setup

### Option A: Automatic (Recommended)

```bash
cd /path/to/your-project
null setup .
```

This generates the correct MCP config for Claude Code and Cursor, and merges with existing configs (e.g., if Aleph is already configured).

### Option B: Manual — Global Config

Add Null to your global Claude Code config so it's available in **every** session:

**macOS / Linux:** `~/.claude/.mcp.json`
**Windows:** `C:\Users\<username>\.claude\.mcp.json`

```json
{
  "mcpServers": {
    "null": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "null_memory.cli", "serve"],
      "env": {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    }
  }
}
```

> **IMPORTANT:** The module name is `null_memory.cli`, NOT `null.cli`. The package is called `null-memory` and the Python module is `null_memory`.

> The `env` block is required hardening: it stops git child processes from
> blocking on credential prompts (harmless on macOS/Linux, load-bearing on
> Windows — a missing block once wedged a server for 9 minutes).

Replace `/path/to/python` with the Python that has `null-memory` installed. Find it with:
- macOS/Linux: `which python` or `which python3`
- Windows: `where python`

### Option C: Manual — Per-Project Config

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "null": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "null_memory.cli", "serve"],
      "env": {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    }
  }
}
```

### With Aleph

If you also use [Aleph](https://github.com/alephnullai/aleph), your config should have both:

```json
{
  "mcpServers": {
    "aleph": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "aleph.cli", "serve", "."]
    },
    "null": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "null_memory.cli", "serve"],
      "env": {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    }
  }
}
```

### Windows-Specific Notes

- Use full paths with backslashes or forward slashes: `C:/Users/you/anaconda3/python.exe`
- The memory directory defaults to `C:\Users\<username>\.null\`
- All features work cross-platform — memory exported on macOS imports on Windows and vice versa

## Deterministic Capture vs Model-Initiated Tools

Null captures memory through two complementary channels:

1. **Model-initiated MCP tools** (`null_remember` with
   `kind=observe|learn|decide|…`) — the agent *decides* to record something. High
   signal, but unreliable as the only channel: the model can forget to
   call them, and everything it was "about to record" is lost on context
   compaction or session end.
2. **Deterministic hooks** — Claude Code executes Null's hook scripts on
   lifecycle events whether or not the model thinks of it:

   | Event | Script | What it captures |
   |---|---|---|
   | `SessionStart` | `null-session-hook.py` | Identity/briefing reminder at every session boundary (incl. post-compaction) |
   | `UserPromptSubmit` | `null-context-inject-hook.py` | Injects relevant facts into the prompt — working memory without asking |
   | `UserPromptSubmit` | `null-prompt-verify-hook.py` | Pre-arms live-state truth (git, files, schema) against stale doc claims |
   | `PostToolUse` (Write/Edit) | `null-file-change-hook.py` | Records which files the agent changed |
   | `PreCompact` | `null-compact-hook.py` | Tells the compaction summarizer which memories must survive |

Register the hooks into a project with:

```bash
null setup /path/to/project --hooks
```

This merges into the project's `.claude/settings.json` non-destructively
(unrelated keys and hooks are preserved; re-running updates Null's
entries instead of duplicating them). `null doctor` reports whether the
hooks are registered for the current project.

Rule of thumb: hooks make capture *reliable*, tools make it *rich*. Run
both.

## Quick Start

```bash
# Name your agent (optional but recommended)
null name Atlas

# Check what your agent knows
null status

# Morning orientation (state, momentum, watches, memory summary)
null wakeup

# Export knowledge to share with another instance
null export -o my-brain.json

# Import on another machine/project
null import my-brain.json
```

## How It Works

See [docs/design/EVENT_SOURCED_SYNC.md](docs/design/EVENT_SOURCED_SYNC.md) (sync architecture) and [docs/EXCHANGE.md](docs/EXCHANGE.md) (multi-seat orgs) for the deep dives.

**Short version:** Null stores identity, knowledge, and decisions in `~/.null/`. Every conversation turn, your AI records what it learned. On the next session — in any project — it loads that knowledge and picks up where it left off.

## Multi-Machine & Multi-Seat Sync

Null syncs across machines and seats with an event-sourced model — no
binary-file merge conflicts, ever:

- **Same identity, several machines** (replicas of one store): each
  writer appends to its own event log; the daemon's poke loop fetches,
  fast-forward pulls, and replays new events into the local db every few
  minutes. Design: [docs/design/EVENT_SOURCED_SYNC.md](docs/design/EVENT_SOURCED_SYNC.md).
- **Different identities, one org** (separate stores, typed edges): seats
  exchange reports, push announcements, advisory WIP claims, and
  questions over a shared **org exchange** repo — announcements travel,
  artifacts stay home. Setup guide: [docs/EXCHANGE.md](docs/EXCHANGE.md);
  org design: [docs/design/ORG_TOPOLOGY.md](docs/design/ORG_TOPOLOGY.md).
- **The UDP doorbell** makes both near-instant on a LAN: a contentless
  "fetch now" ping after every push/post — carries nothing, trusts
  nothing, and the periodic poll remains the delivery guarantee.

## MCP Tools

When connected as an MCP server, Null provides a deliberately small
15-tool surface. Merged tools take a selector parameter (`kind=`,
`mode=`, `action=`) instead of multiplying tool names:

| Tool | Purpose |
|------|---------|
| `null_remember` | Unified write path — `kind=observe` (what you learned this turn), `learn` (explicit fact), `decide` (decision + `why`), `mistake` (what + `why`, never pruned), `wonder` (open question, optional `category`), `contradict` (check for conflicts) |
| `null_recall` | Search memory for relevant facts (rank-fusion of keyword, fuzzy, semantic) |
| `null_briefing` | Morning briefing — top facts, mistakes, decisions |
| `null_close` | Atomic session close (debrief + reflect + sync + git commit) |
| `null_checkpoint` | Deep save — flush to disk + git commit |
| `null_verify` | `mode=fact` (mark a fact confirmed-still-true), `claim` (live-check a state claim before asserting it, optional `claim_type`), `identity` (three-proof identity check) |
| `null_identity` | Silent identity coherence check (identity preloads at boot) |
| `null_status` | Memory status summary |
| `null_context` | Get project-specific context |
| `null_outcome` | Record a decision's outcome — closes the learning loop |
| `null_anchor` | Tag a fact as an emotional anchor (never decays) |
| `null_catchup` | Reconstruct knowledge from git history |
| `null_exemplar` | `action=search` or `action=add` calibration exemplars |
| `null_forget` | Soft-delete a fact from memory |
| `null_multiverse` | `action=list\|broadcast\|recall\|wakeup` across personas |

Maintenance moved to the CLI: `null gc`, `null consolidate`, `null doctor`,
`null calibrate`, `null evaluate`, `null export`, `null import`, `null name`,
`null probe add`, `null outreach send`. Set `NULL_LEGACY_TOOLS=1` to
temporarily restore the old tool names as deprecated aliases.

## CLI Commands

```bash
null status              # Memory status
null wakeup              # Morning orientation
null serve [dir]         # Start MCP server
null selftest            # RELEASE GATE — see "The responsiveness contract"
null setup <path>        # Generate MCP configs
null setup <path> --hooks  # + register deterministic capture hooks
null doctor              # Memory health + install/hook status
null name <name>         # Set agent name
null export [-o file]    # Export knowledge
null import <file>       # Import knowledge
null watch add <query>   # Watch for changes
null watch list          # List active watches
null simmer              # List open questions
null simmer add "q"      # Add a simmering question
null exchange post --kind <kind> --data '<json>'  # Post to the org exchange
null exchange announce-push  # Announce a code push (run after git push)
null exchange sync       # Ingest subscribed exchange streams now
null exchange status     # Exchange config, claims, pending queries
```

## The Responsiveness Contract (release gate)

A product that randomly hangs is worse than one that errors. Null enforces
responsiveness as a contract, in three layers:

1. **`null selftest` — the release gate.** No release ships while this is
   red. It spawns a fresh MCP server on a throwaway store and exercises
   **every** tool on the 15-tool surface against a per-tool time budget
   (10s default, 20s for `null_identity`/`null_briefing`; scale all
   budgets with `NULL_SELFTEST_BUDGET_MULT` on slow CI). Output is a
   budget table (tool / elapsed / budget / status); statuses are `OK`,
   `SLOW`, `FAIL`, and `TIMEOUT` (a hung tool — the server under test is
   killed and respawned so the rest of the surface still gets probed).
   Any non-OK row exits nonzero.
2. **Per-tool-call watchdog (in the live server).** Every tool call runs
   on a worker thread under a soft budget (`NULL_TOOL_BUDGET`, 15s — slow
   calls complete but leave a breadcrumb) and a hard budget
   (`NULL_TOOL_HARD_BUDGET`, 60s — the client gets an error instead of a
   hang; the runaway work is abandoned, never killed). `null doctor`
   surfaces the recorded violations.
3. **Subprocess hygiene lint (`tests/test_subprocess_hygiene.py`).**
   Child processes without an authoritative timeout are the other hang
   root-class. The test suite AST-lints `src/` and fails on any
   `subprocess` call without a timeout outside the audited hardened
   wrappers; `os.system`/`os.popen` are banned outright.

## Troubleshooting

### "ModuleNotFoundError: No module named 'null'"

Your MCP config has the wrong module name. Change `null.cli` to `null_memory.cli`:

```json
"args": ["-m", "null_memory.cli", "serve"]
```

### "null: command not found"

The package isn't installed or isn't on your PATH. Reinstall:
```bash
pip install null-memory
```

Or if installed from source:
```bash
cd /path/to/null
pip install -e .
```

### MCP server not connecting

1. Verify the Python path in your `.mcp.json` is correct: run `which python` (or `where python` on Windows)
2. Verify `null-memory` is installed in that Python: `/path/to/python -c "import null_memory; print('OK')"`
3. Verify the module name is `null_memory.cli`, not `null.cli`

## License

**Open source** under the [Apache License 2.0](LICENSE) — © 2026 [Aleph Null LLC](https://alephnull.ai).

Free for everyone — individuals, teams, and companies of any size. No paid tiers, no seat licenses, no feature gates. Apache-2.0 includes an express patent grant.

Prior `AGPL-3.0-only` releases remain under AGPL for anyone who already obtained them.
