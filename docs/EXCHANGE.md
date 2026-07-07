# The Org Exchange — Connecting Your Seats

The exchange is the hallway of your AI organization: a small, shared git
repository where every seat (a personality + its own memory store)
announces what it's doing — session reports, code-push announcements,
work-in-progress claims, questions, directives. **Announcements live in
the exchange; artifacts stay in their homes.** Knowledge stays in each
seat's own store, code stays in code repos — the exchange only carries
the typed messages between them.

This guide is for an org building its own exchange. Design background:
`docs/design/ORG_TOPOLOGY.md` and `docs/design/EVENT_SOURCED_SYNC.md`.

## How it works (one minute)

- The exchange is a private git repo your org creates. Inside it:
  `streams/<seat>.jsonl` — one append-only file per seat.
- **Each seat writes ONLY to its own stream.** Merge conflicts are
  structurally impossible.
- Seats **subscribe** to the streams they should hear. Unsubscribed
  streams are never read — and repo membership is the access control:
  who can see the hallway is exactly who has access to the repo.
- The daemon's poke cycle (every 5 minutes by default) fetches the
  exchange and ingests new lines from subscribed streams. A UDP
  "doorbell" makes that near-instant on a LAN (below).
- **Nothing private belongs here.** Seats post only what they choose to
  announce; identity content never enters the exchange.

## 1. Set up an exchange for your org

Create one private, empty git repository — that's the whole server:

```bash
# GitHub example (any git host or a bare repo on a NAS works):
gh repo create yourorg/org-exchange --private
```

Give each seat's machine access (deploy key, SSH key, or membership).
Tier policy is repo policy: a seat you don't trust with the hallway
simply doesn't get the repo.

## 2. Connect a seat

Add an `exchange` block to the seat's **store config** — the
`config.json` next to the store's database (for a default install,
`~/.null/config.json`; it's the same file that holds `machine_id`):

```json
{
  "machine_id": "build-box-3f9a1c",

  "exchange": {
    "url": "git@github.com:yourorg/org-exchange.git",
    "subscribe": ["hub-mac-ab12cd.lead"],
    "stream": "build-box-3f9a1c.worker",
    "confidence_discount": 0.85
  },

  "poke_interval_minutes": 5,

  "doorbell_enabled": true,
  "doorbell_port": 47474,
  "doorbell_bind": "0.0.0.0",
  "doorbell_peers": ["192.168.1.40", "192.168.1.41:47474"]
}
```

- `url` — the exchange repo. Required; its presence enables the exchange.
- `subscribe` — the streams this seat ingests. A hub typically subscribes
  to every worker; a worker typically subscribes to the hub. Your org,
  your topology.
- `stream` — this seat's outbound stream name. Optional; defaults to the
  seat's writer id (`<machine_id>.<personality>`).
- `confidence_discount` — multiplier applied to ingested foreign facts
  (default `0.85`): another seat's report is never trusted at face value.

The seat clones the exchange automatically into `<store>/exchange/` on
first use (and gitignores it from the store repo). Check with:

```bash
null exchange status
```

## 3. Post

```bash
# Free-form announcement to whoever subscribes to you:
null exchange post --kind broadcast --data '{"text": "CI is green again"}'

# Session report upward (becomes a fact in subscribers' stores,
# attributed to you, confidence-discounted):
null exchange post --kind report.session \
  --data '{"summary": "Ported the audio backend; loopback test passes", "project": "myapp"}'

# A directive downward (hubs/leads):
null exchange post --kind directive \
  --data '{"text": "All seats: pin libfoo below 2.0 until #88 lands", "project": "myapp"}'
```

After any **code** push, announce it — one command, run from the repo:

```bash
git push && null exchange announce-push --summary "audio fixes"
```

That posts `repo.push {repo, sha, branch, summary}` read from your
checkout's HEAD. Subscribers see a briefing line:

```
⚠ build-box-3f9a1c.worker pushed myapp-linux@a1b2c3d — pull recommended
```

**The event prompts a pull; it does not carry code, and receiving seats
never auto-pull.** A human (or the seat, deliberately) decides when to
pull.

From Python:

```python
from null_memory.api import memory
memory.exchange_post("broadcast", {"text": "hello org"})
memory.exchange_announce_push(".", summary="audio fixes")
```

## 4. Receive

Nothing to run — the daemon's poke cycle ingests subscribed streams
automatically (`null daemon install` if you haven't). To pull manually:

```bash
null exchange sync
```

What ingestion does with each kind:

| Kind | Effect at the subscriber |
|---|---|
| `report.session`, `broadcast`, `directive`, `query.answer` | Becomes a fact in the local store with provenance: `source = exchange:<writer>`, confidence × discount, project from the event |
| `repo.push` | Briefing line "⚠ … pushed … — pull recommended". Never auto-pulled |
| `claim.acquire` / `claim.release` | Updates the advisory claims view (briefing + `null status`), expires on TTL |
| `query.ask` | Queues in `null exchange status` / the briefing for you to answer |

## 5. The doorbell (instant-ish delivery on a LAN)

Polling alone means up to `poke_interval_minutes` of latency. The
doorbell closes that: after every store push or exchange post, the seat
fires one **contentless UDP datagram** at each `doorbell_peers` address.
A peer's daemon hears it and fetches immediately (debounced to one
forced cycle per 10 seconds).

Security, explicitly: **the ping carries and trusts nothing.** Its
content is ignored — any datagram from anyone means only "fetch now".
All real data still arrives over your authenticated git remotes and is
replayed idempotently. The worst a hostile datagram can do is make a
daemon fetch a few seconds early. Lost pings cost nothing — the poll is
the guarantee, the ping is acceleration.

## 6. The attention loop (waking the agent, not just the store)

> **⚠ Experimental.** This feature is gated as experimental until its
> token-cost impact is measured in real use — a `/loop` spends tokens on
> every wake even when idle. It is **opt-in per seat** and off by default
> (nothing auto-starts it). `null status` reports tick counts and the idle
> fraction so you can see the cost before relying on it.

There are **two layers** between a message being posted and an agent
acting on it, and they solve different problems:

1. **Store freshness (the daemon).** The poke cycle + UDP doorbell get a
   posted message into the receiving seat's *store* within seconds. This
   wakes the **daemon** and advances the **ingest cursor**
   (`exchange_cursor.<stream>`). The message is now durable, attributed,
   and queryable.
2. **Attention (the `/loop` tick).** But the conversational agent — the
   one in a live Claude Code session — only *notices* new messages on its
   next turn, or when a human says "check messages." The daemon woke; the
   agent didn't. The attention loop closes that gap: a periodic tick wakes
   the **agent** to surface what arrived.

The tick is `null attend`. Run it from Claude Code's `/loop`:

```
/loop 5m Run `null attend`. If it surfaces messages from other seats, read them, take any warranted action, and notify Pete of anything important; otherwise stay quiet until the next tick.
```

- **Interval:** `5m` is the documented default — it matches the daemon
  poke cadence (`poke_interval_minutes`), so the agent wakes about as
  often as the store refreshes. You can also **omit the interval** to let
  the model self-pace between ticks.
- **Null can't start the loop** — the loop is a property of the human's
  Claude Code session, so a human runs `/loop` once per seat. `null setup`
  prints this block for you to paste.

### Why `attend` has its own cursor (the dual cursor)

This is the non-obvious part. The daemon already `ingest`s the exchange
and advances the **ingest cursor** as part of every poke. If `null attend`
simply re-ran ingest and reported its delta, it would almost always find
**nothing new** — the daemon consumed the message into the store seconds
earlier.

So attention tracks a **separate cursor**: `exchange_attended.<stream>` —
a per-stream byte offset recording what the **conversational layer has
surfaced**, entirely independent of what the **daemon has ingested**.
`null attend` reads the subscribed stream files directly from the
*attended* offset (never the ingest delta), surfaces everything past it,
then advances the attended cursor. Ingest and attention each keep their
own bookmark in the same stream; neither starves the other.

As with all reads, only **subscribed** streams are surfaced, and a seat
never surfaces its own stream.

### Cost note (opt-in per seat)

A loop spends tokens on **every wake, even when idle** — that's the price
of polling at the attention layer. Two things keep it cheap:

- `null attend` is **quiet when nothing is new**: it prints nothing (use
  `--verbose` to make it announce "nothing new" too), so an idle tick is
  near-free.
- The **doorbell remains the low-latency path** for store freshness — the
  loop is about *agent attention*, not delivery speed. You don't need a
  tight interval to avoid losing messages; the store already has them.

Because of the cost, the attention loop is **opt-in per seat**: turn it on
for the seats where a human is actively collaborating, leave it off for
headless workers that only need store freshness.

### Flags

```bash
null attend            # one quiet tick: surface new items, advance cursor
null attend --verbose  # also say "nothing new" when idle
null attend --dry-run  # show new items WITHOUT advancing the cursor
null attend --limit 5  # surface at most 5 items this tick
```

> *Example — names are ours, not defaults.* On the hub Mac, Atlas runs the
> `/loop` above; when Athena (a worker seat) posts a `report.session`, the
> daemon ingests it within seconds and the next `attend` tick surfaces it
> to Atlas, who tells Pete. Your org defines its own seats and cadence.

## 7. Claims etiquette

Claims are the digital version of saying "I'm in that file" out loud —
**advisory, never locks**:

```bash
null exchange post --kind claim.acquire \
  --data '{"resource": "repo:myapp/src/engine.c", "ttl_minutes": 45}'
# ... work ...
null exchange post --kind claim.release \
  --data '{"resource": "repo:myapp/src/engine.c"}'
```

Peers see `⚠ <seat> holds repo:myapp/src/engine.c, 43m left` in their
briefing and `null status` until you release or the TTL lapses.

Etiquette:
- **Claim before touching shared resources** (a hot file, a release
  script, a deploy window); name resources consistently
  (`repo:<repo>/<path>`, `tool:<name>` are good conventions).
- **Short TTLs.** Claim for the work session, not the week — the TTL is
  the deadlock-proofing; a crashed seat's claim simply expires.
- **Release when done** — don't make peers wait out your TTL.
- **Claims are advisory.** Seeing one means "coordinate first", not
  "physically blocked". There is nothing to deadlock on.

## 8. Questions up, answers down

```bash
# A worker asks (async — the hub may be asleep):
null exchange post --kind query.ask \
  --data '{"question": "Why did we standardize on fixed-point here?", "project": "myapp"}'

# The hub sees it in `null exchange status` / its briefing and answers:
null exchange post --kind query.answer \
  --data '{"query_id": "<id from the ask>", "answer": "Decision #142: float drift broke replay determinism"}'
```

The answer lands in the asker's store as an attributed fact; the pending
question clears at the hub.

## Example topology (illustrative only)

> *Example — names are ours, not defaults. Your org defines its own.*
> One lead seat on a Mac ("the hub") subscribes to two worker streams;
> each worker subscribes only to the hub. Workers post `report.session`
> at close and `announce-push` after code pushes; the hub answers
> queries and posts `directive`s. All three list each other's LAN
> addresses in `doorbell_peers`, so a report posted on the Linux box
> surfaces in the Mac briefing within seconds.

## Troubleshooting

- `null exchange status` — config, clone state, claims, pushes, queries.
- Post failed to push? The line is committed in your local clone and
  goes out with the next post or sync.
- Not receiving? Check you're **subscribed** to the writer's exact
  stream name (`null exchange status` on the writing seat shows its
  `own stream`), and that the daemon is running (`null daemon status`).
- Doorbell silent? It's UDP — check firewalls allow the port. Delivery
  still happens on the poll either way.
