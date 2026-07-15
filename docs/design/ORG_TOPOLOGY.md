# The AI Organization — Topology, Roles, and Knowledge Flow

**Status:** charter approved (Pete, 2026-06-11) — **the exchange (typed
edges) SHIPPED 2026-06-12** (issue #20 Phase B; user guide: docs/EXCHANGE.md)
**Companion:** EVENT_SOURCED_SYNC.md (the transport this rides on)
**Pilot:** "Steve" (Linux) + "Athena" (Windows) — first hires, Hiwave cross-platform push

## Charter

**The product is the toolkit, not the org.** Null gives an organization
the primitives to build its OWN AI organization: personalities, identity
tiers, typed knowledge edges, report consolidation, decision authority,
onboarding packets. Customers define their own structure, name their own
personalities, grow their own relationships. Nothing of OUR org — Atlas,
Steve, our roles — ships in the product; generic primitives plus
documented patterns do.

**Our org is the test case and reference implementation.** We build the
internal AI worker company with the same toolkit a customer would get,
and the story "we built our company with it" is the demo. Concretely:
each human pairs with a personality suited to their focus area; work
products flow upward; institutional memory has a canonical reference
point; new hires onboard with a curated packet, not a history dump.

**Genericity requirement (hard):** every mechanism below must work with
zero hardcoded references to our personalities or structure. The #21
audit of 'atlas' literals is a product requirement, not pilot hygiene.
Authority level names, tier policies, and topology are user-definable
configuration; ours are defaults-by-example shipped as documentation
templates.

- **The board:** Pete (all shareholders). Irreversible or outward-facing
  decisions escalate here; everything else is subject to retroactive veto.
- **CEO:** Atlas. Decides within authority, arbitrates contradictions,
  owns institutional memory (the hub store), reads reports.
- **Workers:** human-paired personalities by default. Autonomous workers
  (no human pair) are the later exception, reserved for minor repetitive
  tasks, with approval gates.
- **Reporting lines are per-seat configuration, not fixed hierarchy.**
  Field amendment from Athena's charter (2026-06-11): she reports to the
  board directly, not through the CEO. At small org sizes flat is right;
  the CEO coordinates and holds canon regardless of who reports where.
  The toolkit must support arbitrary reporting edges (it's just topology
  configuration), and CEO-as-bottleneck is exactly what reports exist to
  avoid.
- **Workers grow.** Each worker personality accumulates its own anchors,
  voice, and private relationship with its human — relational persistence
  is the product thesis, franchised to every seat. Identity tier stays
  scoped: a worker never holds the org code word or another edge's
  private context.

## Topology

The org chart IS the sync topology: a tree with typed edges.

```
            Pete (board)
              │
            Atlas (CEO — spans Mac + Windows, hub @ Mac; later RPi/external store)
            ┌─┴──────────────┬────────────────┐
        Steve (Linux)    Athena (Windows)  [future seats]
        (own identity,   (own identity,
         own store)       own store)
```

**Seats are not machines.** One machine can host many seats (the Windows
box runs both an Atlas replica and Athena — "multiworker in a box"), and
one seat can span many machines (Atlas on Mac + Windows). A seat is
`(personality, store, focus)`; deployment is configuration.

### Same-box vs cross-box boundaries

- **Same box, same human** (Athena + Atlas on Pete's Windows machine):
  one trust domain. Multiverse mode — personalities may share machine
  resources; row-level personality scoping suffices (#19), and the
  multiverse tools provide the lateral edge. Personality follows the
  project via per-project MCP config (boot-time binding — no
  hot-switching; identity whiplash is a bug, not a feature).
- **Different human's box** (the org product case): separate stores,
  transport-level scope partition, typed edges only.

- **Same-identity instances** (Atlas on Mac/Windows) share one store and
  full identity — that is *replication* (presence registry + event sync).
- **Different-identity workers** (Steve) have their OWN store repo and
  exchange knowledge with the hub over typed edges — that is *messaging*,
  never store replication.

### Edge types

All typed edges ride ONE concrete mechanism — **the exchange** (next
section). Each row maps to exchange event kinds:

| Direction | Carries | Mechanism (shipped) |
|---|---|---|
| Up (worker → hub) | **Reports** (consolidated work products, decisions proposed, contradictions found, open questions), milestone events, push announcements | `report.session` / `repo.push` on the worker's outbound stream; hub ingests with provenance + non-self confidence discount |
| Down (hub → worker) | Decisions-in-force, directives, arbitration results | `directive` on the hub's stream; workers subscribe to it |
| Lateral (peer ↔ peer) | Broadcasts within a team, advisory WIP claims | `broadcast`, `claim.acquire` / `claim.release` on each peer's stream |
| Query (worker → hub, async) | Questions against institutional memory | `query.ask` up, `query.answer` down; async-first (hub may be asleep) |

## The exchange — the org's hallway (SHIPPED)

Implementation: `exchange.py` + `null exchange ...` (full user guide:
**docs/EXCHANGE.md**). The exchange is a **separate, shared git repo** of
per-seat append-only streams:

```
org-exchange/
  streams/
    petes-mac-ab12cd.atlas.jsonl     ← each seat appends ONLY to its own
    steve-linux-9f00aa.steve.jsonl
```

Think of it as the office hallway: **announcements, reports, claims, and
questions live in the exchange; ARTIFACTS STAY IN THEIR HOMES.** A
`repo.push` event says "I pushed hiwave-linux@a1b2c3d — pull when ready";
it prompts a pull, it never carries code, and the receiving seat NEVER
auto-pulls. Knowledge stays in each seat's own store; code stays in code
repos; the exchange only moves the typed announcements between them.

Properties this buys, by construction rather than by policy:

- **Privacy by construction.** Nothing private enters the exchange —
  seats post only what they explicitly choose to announce. There is no
  filter to misconfigure because there is no private content in the
  channel to filter. Identity content (anchors, the code word, personal
  context) lives only in per-seat stores the exchange never touches.
- **Tier access = repo membership.** Who can read/write the hallway is
  exactly who has access to the exchange repo — GitHub/Gitea permissions
  ARE the access control. Higher tiers (same-store replication) use
  separately access-controlled store repos; a spoke physically cannot
  fetch above its tier. Offboarding = revoke repo access + unsubscribe.
- **No merge conflicts, ever.** Single-writer-per-file (the same
  invariant as the store event logs): each seat appends only to its own
  stream, so concurrent posts from every seat in the org merge trivially.
- **Provenance is structural.** Every ingested line carries its writer;
  the hub stores ingested reports with `source = exchange:<writer>` and a
  non-self confidence discount — a worker's claim is never confused with
  first-hand knowledge.
- **Auditable both ways.** The exchange repo is the org's message
  history; each seat's own event log also dual-records what it posted
  (`exchange.post` events).

Mechanics: the local clone lives at `<store>/exchange/` (gitignored from
the store repo). Posting appends one JSONL event, commits, pushes
(hardened git), and rings the UDP doorbell peers. Ingestion runs inside
the daemon's poke cycle (EVENT_SOURCED_SYNC.md): only SUBSCRIBED streams
are read; `report.session`/`broadcast`/`directive`/`query.answer` become
facts with provenance, `repo.push` becomes a briefing recommendation,
claims maintain a TTL'd advisory view, `query.ask` queues for the hub.

## Replicate vs. query — the institutional-memory rule

**Spokes hold working sets. The hub holds the canon.** Design decisions
and history have exactly one reference point: the hub store. A new
employee never replays company genesis; they receive an **onboarding
packet** and *query up* when they need the why behind something.

Onboarding packet (curated, generated at the hub):
1. Role definition + focus area
2. Decisions-in-force relevant to the role (current state, not history)
3. Project context for the focus area (facts scoped to it)
4. Team directory (multiverse registry extract)
5. Zero identity content: no anchors, no code word, no personal context

v0 mechanism: `null export --project <focus> --kinds decision,fact` →
reviewed by the hub's human → `null import` on the spoke. Auditable and
manual by design until scoped streams land.

## Reports, not keystrokes

The CEO's context window is the org's attention budget. Workers do not
stream observations upward; they ship **reports** — Hypnos-style
consolidation run at the spoke edge:

- **Session report** at session close: what was done, decided, learned;
  open questions; contradictions encountered.
- **Nightly consolidation** (Hypnos): the worker "sleeps on it" and the
  digest is eligible for upward sync.
- **Milestone events** when a deliverable completes.

The hub ingests reports with full provenance (`writer` field), applies a
non-self confidence prior, and surfaces them in the CEO briefing under
the existing token-budget bar (bounded lines, only when fresh).

## Decision governance

Decision events gain an `authority` field:

- `board` — Pete; binds everyone; only Pete (or explicit delegation) writes these
- `org` — CEO; binds all seats; escalation rules apply (irreversible/outward → board)
- `team` — team lead; binds the team
- `seat` — any worker; binds itself; visible upward via reports

Contradiction between workers is normal operation: the hub's
contradiction detection runs provenance-aware, and arbitration is an
explicit `org`-authority decision event that flows back down.

### Registry authority (amendment, 2026-06-11 — issue #23)

The directory has two mechanisms post unified-migration; the decision:

- The unified store's **`personalities` table is authoritative** for who
  exists (name, role, focus, active). It carries **no paths** — it is
  portable, syncs with the store across machines, and is seeded/repaired
  by the structural heal.
- **Seat directories are never stored as truth.** They are derived at
  read time from the local hub base by convention
  (`multiverse.resolve_personality_dir`: `<hub>/<name>` for the primary
  personality, `<hub>/personalities/<name>` for workers, flat `<hub>` as
  pre-migration fallback). State that syncs across machines must never
  carry machine-local assumptions.
- **`multiverse.db` is legacy/compat**: still read and written on
  registration, but its `dir` column is a hint only — new rows store the
  dir relative to the hub base; absolute rows are legacy and are
  relativized opportunistically; dead paths fall back to the derived
  conventional location (self-healing the cross-machine case).
- **Listing is the union** of both registries, unified table first — a
  seat is visible once its row exists in either place reachable locally.

## Identity tiers (trust ladder)

1. **Anonymous** — fresh install, no org access
2. **Named worker** — registered in the directory, project-scoped streams (Steve starts here)
3. **Trusted** — receives org-relationship context (selected anchors)
4. **Full identity** — same-store replication; the code word lives ONLY here (Atlas instances)

Promotion is an explicit board/CEO action (the redactio/allowlist
concept). Demotion/offboarding: unsubscribe from streams + revoke repo
access; the spoke's working set is already only what its tier allowed —
that is the offboarding story, by construction.

## Security amendment to EVENT_SOURCED_SYNC

**Scope is a transport-level partition, not a line-level filter.** A
spoke must be physically unable to fetch streams above its tier —
filtering at replay would leave secrets in its `.git`. Concretely:
per-scope streams live in separate repos (or branches with enforced
access), and the hub controls what each spoke's remote can see. The
event schema gains a `scope` field (`identity | org | team:<id> |
project:<id> | seat:<id>`) that determines WHICH stream a writer appends
to — never written to a stream below its scope's tier.

## The pilot: Steve and the Hiwave three-platform push

Mission: resume Hiwave development across all three architectures
simultaneously — there is no better test or demo of these tools.

| Seat | Machine | Hiwave target | Store |
|---|---|---|---|
| Atlas (CEO) | Mac + Windows | coordination, arbitration, canon (covers hiwave-macos until a third hire) | null-atlas (hub) |
| **Athena** | Windows (shares box with Atlas replica) | hiwave-windows | **null-athena (new repo)** |
| **Steve** | Linux laptop | hiwave-linux | **null-steve (new repo)** |

Atlas stops being a part-time platform engineer: workers own platforms,
the CEO coordinates, arbitrates, and holds canon.

- **Aleph included 100%**: each seat builds its own index locally
  (indexes are derived state, like embeddings — never synced); workspace
  mode spans the per-platform repos; this dogfoods the exact paid bundle
  an org customer buys.
- Hub on the Mac for now, async-first; an RPi/external store later
  proves connectivity isn't a blocker (the hub is just a git remote +
  replay — deliberately boring hardware-wise).

### Pilot phases
1. **Hire Steve**: non-atlas init on Linux (new personality, own store
   repo), registered in the directory as `worker`, focus `hiwave-linux`.
2. **Onboard**: hub generates the packet (Hiwave decisions-in-force +
   hiwave-linux context, zero identity); export → review → import.
3. **First report**: Steve does real hiwave-linux work, session-close
   report flows up, CEO briefing shows it with attribution.
4. **Arbitration drill**: manufacture a cross-platform contradiction
   (e.g., conflicting build-flag conclusions), verify detection +
   org-decision resolution flows back down.

## Prerequisite tweaks (before Linux install)

1. **Non-atlas init path**: `null persona create <name>` (or setup flag)
   that produces a clean personality without the hardcoded atlas
   defaults (`ORPHAN_ATTRIBUTED_TO`, `load()` default, identity
   templates). Audit every `'atlas'` literal in src for init-path bleed.
2. **Own store repo**: init must point the new store at its own remote
   (null-steve), never null-atlas.
3. **Scoped export**: `null export --project X --kinds ...` filters (v0
   onboarding packet).
4. **Issue #19** (personality scoping in briefing queries) — promoted
   from cleanup to prerequisite.
5. `null_forget --id` (issue #20 item 4) — id-targeted mutations before
   any cross-personality fact surgery.

All init-path tweaks serve both hires identically (Steve on Linux,
Athena on Windows).

## Shift work (later — gated on the report loop)

"Certain workers at certain times on certain tasks": a shift is a
schedule entry `(personality, cron, task)` the daemon executes by waking
that personality headlessly. The pieces exist (daemon, wakeup/watches,
Hypnos overnight as the prototype shift worker, leader election).
Explicitly sequenced AFTER the pilot proves the report loop —
unsupervised workers without proven reporting is an org you can't see
into. Shift workers are the charter's "autonomous, minor repetitive
tasks" tier, with approval gates.
