# Create Your Persona

Five minutes from `pip install` to a working AI partner that knows who you are.

## What you're building

A persistent AI persona — not a tool, a partner. It has:
- A name (yours to pick)
- An identity that doesn't reset between sessions
- Memory of conversations, decisions, and what works
- Working style you configure
- Exemplars seeded from a template so it feels useful on day 1

Atlas is Pete Copeland's persona. **You are not building Atlas.** You're building yours.

## Prerequisites

- Python 3.10+
- Claude Code (or another MCP-compatible client)
- 5 minutes

## Step 1 — Install

```bash
pip install null-memory
```

Verify:

```bash
null --version
```

## Step 2 — Run the wizard

```bash
null persona create
```

The wizard walks you through 5 steps:

### Step 1: Name your persona

Lowercase, letters/digits/hyphens, 2-32 chars. Examples: `aria`, `scout`, `max`, `helix`.

> Avoid `atlas` (reserved — that's Pete's). Avoid `null`, `system`, `default`.

### Step 2: Pick a template

| Template | When to use |
|----------|-------------|
| **warm-coach** | Learning, growth, anything where encouragement helps |
| **terse-engineer** | Code partner. No fluff. Pushes back when you're wrong |
| **creative-collaborator** | Writing, brainstorming, ideation |
| **business-analyst** | Strategy, decisions, evidence-driven analysis |
| **twitter-growth** | Voice-matched social content drafting |

See `~/Repos/null/templates/<id>/DESCRIPTION.md` for details on each.

### Step 3: Focus

Be specific. **Bad:** "finance". **Good:** "personal finance coaching for new professionals starting their first 401k".

The focus narrows what the persona pays attention to and what they ignore.

### Step 4: Day-1 interview

Three questions. They seed the persona with enough context to feel real on conversation 1:

1. Who are you and what do you do? (1-2 sentences)
2. Why this persona? What do you want them to help with?
3. If they're doing a great job 30 days from now, what does that look like?

The answers become durable facts + anchors. The persona will refer back to them.

### Step 5: Confirm

Type `y` to create. The wizard:
- Creates `~/.null/personalities/<name>/`
- Initializes the persona's memory
- Seeds 4-10 facts from your answers
- Seeds 3 calibration exemplars from the template
- Sets 2-3 emotional anchors (origin, commitment)
- Prints an MCP config snippet to paste

## Step 3 — Wire up MCP

The wizard prints a JSON snippet like:

```json
{
  "aria": {
    "type": "stdio",
    "command": "/usr/bin/python3",
    "args": ["-m", "null_memory.cli", "serve",
             "/Users/you/.null/personalities/aria"],
    "env": {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
  }
}
```

The `env` block stops git child processes from blocking on credential
prompts (harmless on macOS/Linux, load-bearing on Windows) — keep it.

Add it to `~/.claude/settings.json` under `mcpServers`. Example:

```json
{
  "mcpServers": {
    "null": { ... },
    "aleph": { ... },
    "aria": {
      "type": "stdio",
      "command": "/usr/bin/python3",
      "args": ["-m", "null_memory.cli", "serve",
               "/Users/you/.null/personalities/aria"],
      "env": {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    }
  }
}
```

Restart Claude Code.

## Step 4 — Talk to your persona

Open Claude Code. The persona's identity loads automatically via the SessionStart hook.

First conversation:

> **You:** Hi Aria.
>
> **Aria:** Hi. You mentioned you wanted a pair programmer for API design — what are we working on today?

That recall is the bootstrap doing its job. Without it, Aria would respond like a generic assistant. With it, she's already your collaborator.

## Step 5 — Let it learn

For the first 5-10 sessions, just have real conversations. Don't try to "train" it.

The persona will:
- Record observations every turn (`null_remember` with `kind=observe` runs automatically)
- Note your decisions and reasoning (`null_remember` with `kind=decide`)
- Remember mistakes and what fixed them (`null_remember` with `kind=mistake` — never pruned)
- Build calibration exemplars from how you correct it (`null_exemplar` with `action=add`)

After 30 days, run:

```bash
null persona validate aria      # health check
null calibrate                  # measure how well it knows you
```

## Common workflows

### Switch personas
Each persona is its own MCP server. To talk to a different one, just call its tools (`mcp__aria__null_remember` vs `mcp__scout__null_remember`).

### List your personas
```bash
null multiverse list
```

### Update identity by hand
Edit `~/.null/personalities/<name>/identity.json` directly. Then validate:

```bash
null persona validate <name>
```

### Share knowledge between personas
Personas live separately but can broadcast events:

```bash
null multiverse broadcast "Shipped new pricing tier" --to scout,aria
```

### Back up
Personas are just files. `cp -r ~/.null/personalities/aria /backup/`.

## What NOT to do

- **Don't copy Atlas.** Atlas is Pete's. Six months of relationship work make him irreplaceable. Building yours from scratch is the point.
- **Don't change the template id after creation.** It's used for exemplar seeding. Pick once.
- **Don't share `identity.json` publicly** unless you're comfortable with what's in there — it's a record of who you are to the persona.
- **Don't expect day-1 magic.** The first 5 sessions are warm-up. The persona gets noticeably sharper around session 10-20.

## Troubleshooting

### "Personality already exists"
Either pick a different name or remove the old one:
```bash
rm -rf ~/.null/personalities/<name>
sqlite3 ~/.null/multiverse.db "DELETE FROM personalities WHERE name='<name>';"
```

### "No templates found"
The bundled templates live at `<install-dir>/templates/`. If pip put them elsewhere:
```bash
python -c "from null_memory.persona_wizard import templates_dir; print(templates_dir())"
```

### Persona doesn't remember the day-1 interview
Run `null persona validate <name>` to check the identity file. Check facts landed:
```bash
sqlite3 ~/.null/unified.db \
  "SELECT fact FROM facts WHERE project LIKE '<name>%' AND source LIKE 'bootstrap%';"
```

### "ImportError: persona_wizard"
You're on an older version. Upgrade:
```bash
pip install --upgrade null-memory
```

## Next steps

- [Bootstrap fact seeding details](./BOOTSTRAP.md)
- [Writing your own template](./TEMPLATES.md)
- [Multiverse: managing multiple personas](./MULTIVERSE.md)
- [Hypnos: how the sleep cycle keeps memory healthy](./HYPNOS.md)

---

Questions? Discord: alephnull.ai/discord
