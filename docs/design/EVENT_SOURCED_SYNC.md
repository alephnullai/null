# Event-Sourced Sync — Design

**Status:** Phase A SHIPPED (2026-06-11) · **Phase B SHIPPED (2026-06-12)**
— poke loop + org exchange + UDP doorbell. Phase C (db leaves git) pending.
**Owner:** Atlas
**Supersedes:** "git as literal v1 transport" carrying the SQLite db (the db-in-git
approach is retired by this design; git remains the default transport)

## Why

The 2026-06-11 cross-machine code-word rotation proved the thesis end-to-end —
and exposed the structural limit: it worked because the second machine was a
pure reader. Two *writers* syncing a binary SQLite file cannot merge on any
transport. Git, Syncthing, rsync — all of them can only move the file or
declare a conflict.

The fix is not a better pipe. It is changing what flows through it.

## Core model

1. **Each writer appends only to its own log.**
   `~/.null/events/<writer_id>.jsonl` — one JSON event per line, append-only.
   Single-writer-per-file makes merge conflicts *structurally impossible*:
   different writers touch different files, and within one file git only ever
   sees appended lines.

2. **The SQLite db becomes a local materialized view.**
   It is replayed from the union of all logs and is no longer synced. Once
   Phase C lands it is gitignored. Anything expensive and derivable —
   embeddings, FTS, salience caches — is recomputed locally and never evented,
   which also keeps the logs small and pure text.

3. **Transport moves dumb bytes.**
   Git (default, free, private, offline-first, audit history), Syncthing
   (optional, near-real-time), or a future self-hosted relay (the team-license
   v2). Replay does not know or care how lines arrived.

## Writer identity

- `writer_id` = stable **machine** identity (`machine_id` generated once into
  `~/.null/config`), optionally suffixed per personality:
  `petes-mac.atlas`. NOT the per-process `instance_id` from the presence
  registry — processes are ephemeral and would proliferate files; per-machine
  processes already serialize writes through the local db
  (`write_transaction` + locks), so one log per machine preserves the
  single-writer invariant.
- Each event carries a per-writer monotonic `seq` (local counter table),
  giving total order *within* a writer regardless of clock behavior.

## Event schema

```json
{"seq": 1042, "writer": "petes-mac.atlas", "ts": "2026-06-11T17:43:35Z",
 "kind": "fact.add", "id": "c108ef0b9eef",
 "data": {"fact": "...", "project": "global", "confidence": 0.99}}
```

- `kind` namespace maps 1:1 onto the existing write surface:
  `fact.add | fact.update | fact.forget | fact.anchor`,
  `decision.add | outcome.add`, `mistake.add`, `reflection.add`,
  `probe.add | probe.result`, `exemplar.add`, `session.open | session.close`,
  `broadcast`, `hypnos.promote | hypnos.demote | hypnos.synthesis`.
- `id` is the entity ULID — **adds are idempotent** (replaying the same add
  twice is a no-op).
- Updates are **field-level LWW** (last-writer-wins) ordered by
  `(ts, writer_id)`; per-writer `seq` resolves order within a writer, the
  `(ts, writer)` pair breaks the rare cross-writer tie deterministically.
- Forgets are **tombstones** (matching today's soft-delete semantics — the
  ceremony's `null_forget` mishap also motivates: events reference entity ids,
  never fuzzy matches).
- **Never evented:** embeddings/FTS (derived), presence heartbeats
  (ephemeral; cross-machine "liveness" is inferred from log freshness),
  access counters and decay (local statistics, recomputed),
  doctor/self-heal actions (local repairs of local state).
- Hypnos *knowledge mutations* (promote/demote/synthesis) ARE evented;
  leader election already ensures one mutator per store, and LWW covers the
  multi-machine case.

## Replay

- Per-log **cursor** (byte offset) in a local `replay_cursors` table; pull →
  replay only new lines. Full rebuild = drop view, replay all logs from
  genesis (or latest snapshot).
- Replay is deterministic: sort incoming events by `(ts, writer, seq)`;
  apply through the same code paths as live writes minus side effects
  (no re-embedding burst — embeddings backfill lazily via the existing
  leader-gated backfill).
- **Convergence property:** all machines that have seen the same set of log
  lines materialize identical knowledge state (modulo local-only tables).

## The poke (Phase B — SHIPPED)

Implementation: `poke.py` (`PokeWorker`, embedded in the daemon next to
`HypnosLiveWorker` — its own thread, leader-gated per store via the shared
`LeaderLock` under meta key `null_poke_leader`). Cadence: store config
`poke_interval_minutes` (default 5). One cycle (`poke.poke_once`):

1. `git fetch` on the store repo via the hardened `_run_git`
   (non-interactive, tree-kill timeout — issue #4 machinery).
2. `git pull --ff-only`. Divergence on *log files* cannot happen
   (append-only, disjoint files); a non-fast-forward — necessarily a stray
   non-log file — surfaces a warning and is NEVER merged.
3. Replay new event-log lines into the live db through `replay.py`'s
   appliers — idempotent by Phase A construction (INSERT OR IGNORE adds,
   field-level LWW updates). Per-log byte cursors live in the meta table
   (`replay_cursor.<filename>`); cursors advance only after a successful
   apply, so a crash mid-cycle just re-replays idempotently. Own-writer
   files are skipped (already committed truth). A shrunken file (genesis
   re-baseline) resets its cursor and re-replays from the top.
4. Ingest the org exchange when configured (`exchange.py` — see
   ORG_TOPOLOGY.md and docs/EXCHANGE.md).
5. Fire the wakeup path (due watches run) and record freshness
   (meta `poke_last_update`) so the briefing shows ONE line, only when
   fresh (newer than the last clean close, capped at 24h):
   `↓ store updated from petes-win11.atlas 4m ago — 3 events`.

Outbound is already solved: the debounced sync commits + pushes on lifecycle
boundaries; every successful store push also rings the doorbell (below).

## The doorbell (Phase B — SHIPPED, issue #20 amendment)

Implementation: `doorbell.py`. A tiny UDP listener in the daemon
(store config `doorbell_port`, default **47474**; `doorbell_bind`, default
`0.0.0.0` — all LAN interfaces; `doorbell_enabled` to turn it off). ANY
datagram — content is **ignored by design**, only the source IP is logged —
triggers an immediate poke cycle via `PokeWorker.force()`, debounced to at
most **one forced cycle per 10 seconds** (a datagram flood collapses into
one early fetch).

Sender side: after any successful store push (`MemoryRepo.push`) or
exchange post (`ExchangeClient.post`), one datagram is fired at each
address in the store config list `doorbell_peers` (`"host[:port]"`).
Failures are **silent** — the periodic poll is the delivery guarantee; the
ping is pure acceleration.

**Security (explicit):** the ping carries NOTHING and the receiver trusts
NOTHING about it. All real data still arrives over the authenticated git
transports and replays idempotently. The worst an attacker on the LAN can
do is make the daemon fetch a few seconds early — no content to spoof, no
state to corrupt, no audit hole, near-zero attack surface.

## Compaction & bootstrap

- Logs are append-forever in v1 (text, small — defer compaction).
- Design now, build later: **snapshot files**
  (`events/snapshots/<ts>.snapshot.jsonl` = full materialized export +
  manifest of per-writer high-water `seq`). New replicas bootstrap from the
  latest snapshot + subsequent log lines; logs older than the snapshot can be
  archived. Snapshots are produced by the leader only.

## Migration plan

- **Phase A — dual-write + shadow verify. SHIPPED 2026-06-11.** Genesis
  export: current db state → `events/genesis.<writer_id>.jsonl` (every live
  entity as an add event). All writes go to db AND log. `null doctor` gained
  the replay-verify check: materialize logs into a temp db, diff against
  the live db, report drift. Shipped behind `NULL_EVENT_LOG=1`.
- **Phase B — replay-on-pull. SHIPPED 2026-06-12.** The poke loop (above),
  the org exchange (`exchange.py`, ORG_TOPOLOGY.md, docs/EXCHANGE.md), and
  the UDP doorbell. The db is still committed (belt and suspenders).
- **Phase C — db leaves git.** `.gitignore` the db; snapshot mechanism lands;
  repo size drops permanently.
- Rollback at any phase: the db remains authoritative until C; logs are
  additive.

## Security & privacy posture

- Same as today: logs hold the same private content the db already held in
  the same private repo. The code word remains db/log-only (post-scrub).
- Future option (not v1): age/sops encryption of log lines at rest for
  hosted-remote users.
- Clock skew: bounded impact — LWW ties are deterministic; per-writer order
  never depends on wall clock. Doctor warns when a remote log's `ts` leads
  local clock by > 10 min.

## Amendment (2026-06-11, org topology)

Scope is a **transport-level partition, not a line-level filter** — see
ORG_TOPOLOGY.md. Events gain a `scope` field; per-scope streams live in
separately access-controlled repos/branches so a spoke physically cannot
fetch above its identity tier. Different-identity workers exchange typed
messages (reports/directives/queries) over their own streams; same-store
replication is reserved for full-identity instances.

## Explicitly rejected

- **cr-sqlite (CRDT extension):** technically ideal, but a native compiled
  extension — rejected on dependency-fragility grounds (see nebula/numba
  quarantine precedent).
- **Litestream/LiteFS:** single-writer replication; wrong topology.
- **Hosted sync service:** subscription-shaped; violates pricing principles.
  The self-hosted relay remains the team-tier v2 transport *under this same
  event model*.
- **Syncing the SQLite file harder** (locks, turn-taking): fights physics;
  first concurrent write still corrupts or conflicts.

## Work breakdown

1. ~~Event emitter + writer log + seq counter, genesis export, dual-write~~ (shipped, Phase A)
2. ~~Replay engine + cursors + doctor replay-verify~~ (shipped, Phase A)
3. ~~Poke loop: fetch/ff-pull/replay/wakeup + briefing line~~ (shipped, Phase B — plus the exchange + doorbell)
4. ~~`null_forget --id` / id-targeted mutations~~ (shipped)
5. Snapshots + db-out-of-git (M, Phase C — remaining)
