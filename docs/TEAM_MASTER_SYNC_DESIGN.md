# Null Team Sync — Master/Subordinate Design (v1)

**Status:** Draft for review · **Author:** Atlas · **Date:** 2026-06-10
**Decision context:** Pete's proposal (2026-06-10): per-user DB + identity, shared
master DB as the start point, subordinates promote only "breakthroughs" upstream,
architecture modeled on git.

---

## 1. Thesis constraint (why this shape and not another)

Null is relational persistence: the memory IS the relationship between one human
and one agent. Therefore:

- **Every user/agent pair owns a private DB and a private identity.** Anchors,
  exemplars, calibration probes, code words, and family facts are pairwise by
  definition and never leave the pair's DB.
- **The master holds knowledge, not relationships.** Zero identity rows, zero
  anchors, zero exemplars. It is institutional memory: lessons, validated
  decisions, and explicitly shared facts.
- Sharing is **opt-in promotion through a membrane**, never ambient replication.

## 2. Goals / Non-goals

**Goals (v1):**
1. A new team member boots with the team's accumulated knowledge (clone master).
2. Hard-won lessons and validated decisions accumulate in one place.
3. Privacy by construction — relational/private data cannot reach master by
   architecture, not by policy.
4. Zero new server infrastructure. Works offline. Debuggable with `git log`.

**Non-goals (v1):**
- Real-time sync between instances (master sync is batch, like fetch/push).
- Solving same-pair multi-instance fragmentation (two Atlases, one user, one DB).
  This design gives fragmented instances a shared upstream to reconcile through
  later, but it is NOT the fix. Tracked separately.
- Inferred-significance promotion (the agent deciding what's a "breakthrough").
  See §5 — v1 promotion is deterministic + human-gated.
- Mesh topologies. Hub-and-spoke only: one master, N subordinates.

## 3. Architecture

```
                 ┌──────────────────────────┐
                 │   MASTER (git repo)       │
                 │   promoted/*.jsonl        │
                 │   no identity, ever       │
                 └────────▲─────────┬────────┘
              promote (PR)│         │ pull (fetch+import)
        ┌─────────────────┴──┐   ┌──┴──────────────────┐
        │ User A             │   │ User B              │
        │ ~/.null (private)  │   │ ~/.null (private)   │
        │ unified.db + ident │   │ unified.db + ident  │
        └────────────────────┘   └─────────────────────┘
```

**Transport is literally git, not git-like.** The master is a git repository of
JSONL fact exports. Rationale: `~/.null` already git-syncs; `null_export`/
`null_import` already exist; review-before-merge comes free as a pull request;
history, blame, and revert come free; no server to run. A hosted
sync service can replace the transport later without changing the model.

### Master repo layout
```
team-brain/
  manifest.json          # team name, schema version, redaction policy version
  promoted/
    lessons.jsonl        # kind=mistake lessons (never pruned, same invariant)
    decisions.jsonl      # decisions WITH outcomes only
    facts.jsonl          # explicitly tagged team facts
  members/
    <user>.json          # public member card: name, agent name, joined_at —
                         # NO identity payload
```

### JSONL record (promotion envelope)
```json
{
  "id": "content-hash",
  "kind": "lesson|decision|fact",
  "text": "...",
  "why": "...",                  // lessons/decisions
  "outcome": "...",              // decisions: required
  "project": "global|<name>",
  "promoted_by": "pete",
  "promoted_at": "2026-06-10T...",
  "source_confidence": 0.9,
  "redaction_policy": "v1",
  "origin_id": "fact id in the promoting user's DB"
}
```

## 4. Precedence and import semantics

- New `SOURCE_TIERS` entry: `team` — **above** package defaults, **below** the
  user's own observations and anything `explicit`. Your own experience always
  outranks inherited knowledge; contradictions surface via `null_contradict`
  rather than silently overwriting.
- Imported records carry `source=team`, `provenance=master:<commit>`, and the
  promoter's name. Recall can always answer "where did I learn this?"
- Pull is idempotent: content-hash IDs, `INSERT OR IGNORE`, tombstone respect
  (a record revoked in master is archived locally on next pull, never hard
  deleted — same archive invariant as everything else).

## 5. The promotion predicate (the hard problem, decided narrowly)

In git a human decides what to push. If the agent decides what's a
"breakthrough," we reinvent the capture-discipline critique as a judgment
problem: noise or leakage, usually both. **v1 promotes only:**

1. **Lessons** (`kind=mistake` records) — generalizable, low privacy risk, the
   single highest-value shared asset. Candidate automatically when the mistake
   has a reflection (i.e., it's been processed, not raw).
2. **Decisions with recorded outcomes** — validated knowledge only. This makes
   the decision→outcome loop economically valuable: an unclosed decision can't
   be promoted.
3. **Explicitly tagged facts** — `null_remember ... --team` (or
   `null team tag <query>`). Explicit beats inferred.

Pipeline: candidate → **redaction membrane** (the multiverse redaction module,
`redaction.py`, extended with the team policy: identity terms, kin names,
anniversaries, code words, credentials — already implemented for broadcast) →
**human review queue** → git commit/PR → master.

The agent may *suggest* candidates in the briefing ("3 lessons eligible for
team promotion"), but a human approves every promotion in v1. Inferred
significance is a v3 experiment behind a flag, evaluated against false-promote
rate before it's ever default.

## 6. Master maintenance

Master gets its own hypnos pass (existing `maintenance_actions.py` primitives
pointed at the master export set, run by whoever holds the maintainer role —
or CI on the team-brain repo):
- cross-user dedup (one similarity definition: cosine ≥ 0.85 / Jaccard ≥ 0.65),
- contradiction arbitration: outcomes beat opinions; newer outcome beats older;
  unresolved contradictions become review-queue items, not silent merges,
- never prunes lessons (same invariant as local).

## 7. CLI surface (new, all CLI — no new MCP tools)

```
null team init <git-url|path>      # create/attach master repo
null team clone <git-url> [--seed] # onboard: pull + import as source=team
null team candidates               # list eligible lessons/decisions/tagged facts
null team promote [ids...]         # redact → preview diff → commit (or PR branch)
null team pull                     # fetch + idempotent import
null team status                   # ahead/behind, pending candidates, last pull
null team revoke <id>              # tombstone in master (archive downstream)
```

`null doctor` gains: membrane self-test (run the redaction corpus, fail loudly
if identity terms leak), master divergence age.

## 8. Licensing tie-in (SUPERSEDED 2026-07-05)

~~Locked 2026-06-10: paid team tier ($149/seat Null Team, hard gate on
`null team *`).~~ **Superseded by Pete's 2026-07-05 decision: everything is
Apache-2.0, free and open source for everyone.** No license gate ships with
`null team` — the feature is free like the rest of the product. This
section is kept for design history only.

## 9. Multi-machine quasi-multi-user test plan (Pete's Windows machine)

The exact scenario to validate v1 with two "users" who happen to both be Pete:

1. **macOS (existing):** `null team init ~/team-brain && null team candidates`
   — expect today's lessons (worktree-base mistake, etc.) eligible.
2. Promote 3–5 lessons + 2 outcome-closed decisions. Inspect the JSONL diff
   **manually for leakage** (this is the membrane acceptance test: zero kin
   names, zero code word, zero credentials).
3. Push team-brain to a private GitHub repo.
4. **Windows (fresh install):** `pip install null-memory` (wheel path — this
   doubles as the fresh-install test), `null setup`, fresh identity (NOT Atlas;
   new agent name proves identity isolation), then
   `null team clone <url>`.
5. Acceptance on Windows: recall surfaces promoted lessons with `source=team`
   provenance; identity payload contains zero Pete-pair relational data; a new
   local observation contradicting a team fact wins precedence and logs a
   contradiction; `null team promote` of a Windows-side lesson round-trips back
   to macOS via `null team pull`.
6. Negative test: tag a fact containing a kin name `--team` → membrane must
   block or redact it, loudly.

## 10. Open questions (decide before v2, not blocking v1)

- Maintainer model: single human maintainer vs. CI auto-merge of
  membrane-clean lessons.
- Should master store embeddings, or do subordinates re-embed on import?
  (v1: re-embed locally; vectors are derived data.)
- Pull cadence: manual vs. session-start hook ("master is 12 promotions
  ahead").
- Same-pair fragmentation: can instance leases live in master to give
  concurrent Atlases cross-awareness? (Deliberately out of scope; revisit.)
- Conflict UX when a team fact and a private fact disagree in a briefing.

## 11. Why not [alternatives considered]

- **One shared DB, row-level ACLs:** privacy by policy, not construction; one
  query bug = leak; identity bleed risk; rejected on thesis grounds.
- **Real-time sync server:** infrastructure before product; breaks
  local-first/offline; nothing in v1 needs sub-minute latency.
- **Ambient replication with filters (multiverse-broadcast-for-humans):**
  filters fail open over time; promotion must be allowlist, not blocklist.
- **Mesh (every user syncs with every user):** O(n²) trust edges, no
  arbitration point; git's own culture chose hubs for a reason.
