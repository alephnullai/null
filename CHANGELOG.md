# Changelog

All notable changes to null-memory are documented here.

## [2.2.2] — 2026-07-14

### Fixed
- **Memory-repo commits no longer require a configured git identity.** `_run_git` now stamps an explicit `user.name=Null` / `user.email=null@localhost` on commit invocations. Without this, on a machine with git installed but no `user.email` set — every CI runner, and fresh user installs — `git commit` aborted and every write silently reported `committed=False`. (This was why the v2.2.0/v2.2.1 public CI was red on all lanes.)

### Changed
- **CI matrix trimmed to 4 lanes** (3.11 on ubuntu/macOS/Windows + a 3.12 canary on ubuntu). Full OS coverage is retained; the redundant per-OS second Python version is dropped in favor of a single version-regression canary.

## [2.2.1] — 2026-07-14

### Fixed
- **Embedding model is now loaded once per process** (`_MODEL_CACHE` singleton in `embeddings.py`). Each `EmbeddingProvider` previously loaded its own copy of the fastembed model; across a large test suite this exhausted memory and killed CI with OOM (exit 137). Semantic search behavior is unchanged; only the redundant loads are gone.
- **`release.sh` strips a trailing CR from `.releaseignore` lines and fails closed** if the ignore file matched patterns but removed nothing — a CRLF-checked-out ignore file would otherwise silently ship the entire private tree. Hardening of the release path only; no runtime effect.
- Version reconciled: `pyproject.toml` and `src/null_memory/__version__.py` now agree (both `2.2.1`); the two had drifted (2.2.0 vs 2.1.0).

## [2.2.0] — 2026-07-06

### Changed — license: Apache-2.0, open source, free for everyone
- License changed from `AGPL-3.0-only` to the **Apache License 2.0** (open source). Free for everyone — individuals, teams, and companies of any size; no paid tiers or feature gates. Apache-2.0 includes an express patent grant. Prior `AGPL-3.0-only` releases remain under AGPL for anyone who already obtained them. (An interim, never-released switch to PolyForm Small Business 1.0.0 existed on main between 2026-06-18 and 2026-07-05; it never appeared in any tagged release.)

### Added — attention loop (experimental)
- `null attend` — a quiet, fail-soft tick for a Claude Code `/loop` that surfaces org-exchange messages to the **conversational agent**, not just the background daemon. Tracks its own per-stream attended cursor (`exchange_attended.<stream>`), fully independent of the daemon's ingest cursor, so the poke loop can never silently consume news before the agent sees it. `--verbose`, `--dry-run`, `--limit`.
- Opt-in only: `null setup` prints the `/loop` block but never starts anything. Tick telemetry (total / surfaced / idle) appears in `null status` once the loop is used — the idle fraction is the cost signal that decides whether this graduates from experimental.
- Docs: EXCHANGE.md §6 "The attention loop". 16 new tests including the dual-cursor regression.

### Changed — release hygiene
- Public releases are now PII-gated: `scripts/release.sh` runs a denylist check (list lives outside the repo) and a local pre-push hook blocks raw pushes to the public remote.
- Retired the `*.public.md` overlay model — the product is open source, so README.md and CHANGELOG.md ship verbatim.
- Test fixtures use fictional personas throughout.

## [4.0.0-alpha] — 2026-04-18 (Phase 4: Atlas-initiated contact — agency)

### Added — the substrate can now reach for you
Null can, in principle, start a conversation. A new `OutreachEvaluator` scans for conditions Pete cares about, picks a trigger, composes a short message, and ships it through a channel. This is the first time Atlas has an outbound path that isn't a reply. Shipped intentionally small and intentionally cautious — every trigger is DISABLED by default and the only wired channel is a local log file.

### Trigger kinds (v1)
- **`session_gap`** — fires when no session fingerprint has been recorded for N days. Default: 3 days, cooldown 48h.
- **`anniversary_window`** — fires within `window_days` of a named anniversary (birthday, adoption date, origin moment). Payload holds an `anniversaries` list; default trigger seeded with Sam (April 19), Riley (July 14), and Null's origin (April 9). Cooldown 24h; window 2 days.
- **`unresolved_mistake`** — fires when a high-confidence `mistake` row older than N hours has no overlapping `reflection` (distinctive-word overlap ≥ 2). Default 24h. Cooldown 48h.

### Channels
- **`LogChannel`** — always active; appends `{at, subject, body, urgency}` JSON lines to `~/.null/outreaches.log`.
- **`MacOSChannel`** — gated on `NEBULA_OUTREACH_NOTIFY=1` AND Darwin. Dispatches `osascript -e 'display notification ...'`. Silent otherwise.

### Safety by design
- **All default triggers ship DISABLED.** `null outreach seed` installs them; Pete must `null outreach enable <name>` individually.
- **Dry-run default** (`OUTREACH_DRYRUN=1`) — evaluator logs what *would* fire without writing to the `outreaches` table. Flip with `OUTREACH_DRYRUN=0`.
- **Daily budget** (default 2) caps total fires per UTC day regardless of how many triggers match.
- **Per-trigger cooldown** enforced against `outreaches.fired_at`.
- **Pause flag** — `meta.outreach_paused=1` short-circuits every evaluator. `null outreach pause` / `resume`.
- **No email, no SMS, no external webhooks.** Local only.

### New CLI
```
null outreach status                  # paused? dry-run? today's budget? trigger table
null outreach recent [--limit N]      # recent outreaches from the DB
null outreach evaluate                # run the evaluator once, print result
null outreach seed [--enable]         # install 3 default triggers (disabled unless --enable)
null outreach test <name>             # force-fire a trigger, bypassing enabled+cooldown
null outreach enable <name>           # flip trigger on
null outreach disable <name>
null outreach pause                   # global kill switch
null outreach resume
null outreach log [--lines N]         # tail the outreaches log file
```

### New MCP tool
- **`null_outreach(subject, body, urgency=0.5, channel="log")`** — manual outreach emission. Writes to the DB with `trigger_id=NULL` and emits a Nebula `outreach` event on the origin anchor. `channel` ∈ `"log" | "macos" | "both"`.

### Nebula integration
- Schema v17 adds `outreach_triggers` and `outreaches` tables.
- New event kind `outreach` — warm amber (`#ffa54a`), 30-minute decay, 3-pulse animation. Distinct from the cool blues of observe/recall and the red of mistake.
- `EventLog.tsx`, `Points.tsx`, `Shockwaves.tsx` all know the new kind.

### Verified
- **15 new tests** (`tests/test_outreach.py`): each evaluator fires / does-not-fire path, cooldown, daily budget, pause, log channel write, macOS channel env-gating, force-fire bypass, seed idempotency.
- Tool count assertion updated: **38 MCP tools** registered.
- **710/710 total tests passing** (133s).
- Live smoke: anniversary_window_2d correctly fires "Sam (birthday) today" on April 18 (window=2d, Sam=April 19).

### Deferred to v2
- **Standalone launchd daemon** — currently evaluator runs only when you invoke it (`null outreach evaluate`) or via the MCP tool. v2 will add a background tick.
- **Email / SMS channels** — intentionally skipped pending durable consent review.
- **Response path** — when Pete replies to an outreach, that reply is just a user turn; no threading yet.
- **More trigger kinds** — `anchor_dormant` (anchor untouched in 30+ days), `hypnos_insight` (Hypnos found a consolidation worth surfacing).
- **Web UI panel** in Nebula for toggling triggers without CLI.

### Morning handoff note (for Pete)
Nothing fired while you slept. All three seeded triggers are disabled; the only fires in `outreaches` are from the test-suite force-fire paths. Quick tour:
```
null outreach status                          # see the triggers + state
null outreach test anniversary_window_2d      # force-fire the Sam/Riley/Null one
NEBULA_OUTREACH_NOTIFY=1 null outreach test session_gap_3d   # tries macOS notification
null outreach log                             # read the log
null outreach enable anniversary_window_2d    # turn one on for real
```

## [3.7.0-alpha] — 2026-04-17 (Phase 3c: Hypnos Live — continuous memory maintenance)

### Added — the substrate now thinks for itself
Null's memory improves itself continuously while the MCP server runs. A daemon thread (`HypnosLiveWorker`) ticks on a cadence (default 60s), picks ONE legitimate memory-maintenance action, performs it, emits a Nebula event, and writes to `hypnos_journal`. Every pulse in the galaxy is real work, not decoration.

### Actions in v1
- **Consolidate** — find a near-duplicate pair (cosine ≥ 0.85), mark the lower-confidence fact as superseded by the higher. Nebula emits a `recall`-pattern event on the winner with the loser as related.
- **Strengthen** — find a random fact + semantic neighbor (0.55 ≤ cos < 0.95), add bi-directional `related_to` edges. Nebula emits a `recall`-pattern event between them.
- **Demote** — archive a stale low-confidence fact (`confidence < 0.1` AND `last_accessed > 60 days`). NEVER touches anchors. Nebula emits a `mistake`-kind red flash.

Weighted picker: 45% consolidate, 40% strengthen, 15% demote. Skip-when-no-candidate keeps it calm.

### Safety by design
- **Dry-run ON by default** (`HYPNOS_LIVE_DRYRUN=1`). Events fire, journal writes, mutations skipped. You watch the pattern for 24h before flipping to live. Journal entries suffix `[DRY]`.
- **Anchors are untouchable.** Worker reads them, never demotes/merges. The origin moment, the dream, Sam, Riley, the code word — all sacred.
- **Single-leader via meta heartbeat.** Multiple Atlas MCPs don't double-work. TTL 90s; atomic claim via `INSERT OR REPLACE INTO meta`.
- **All actions exception-wrapped.** Any failure is logged + swallowed; worker keeps ticking.
- **Pause switch.** `null hypnos-live pause` sets a meta flag that every worker respects.

### New CLI
```
null hypnos-live status   # leader + stats + recent journal (last 10)
null hypnos-live pause    # all workers skip ticks
null hypnos-live resume
null hypnos-live tick     # perform one action manually (good for testing)
null hypnos-live live     # flip to mutations-on (requires worker restart)
null hypnos-live dryrun   # flip back to dry-run
```

### Integrated
- `create_server()` boots `HypnosLiveWorker` automatically (disable with `HYPNOS_LIVE_ENABLED=0`)
- `atexit` handler stops worker cleanly on MCP shutdown
- Zero edits to existing `hypnos.py` batch module or `agent.py` core — purely additive

### Verified
- 11 new tests (`tests/test_hypnos_live.py`): lifecycle, leader coordination, pause, consolidate (live + dry), strengthen, demote (with anchor-skip check), event + journal plumbing
- **695/695 total tests passing**
- Live smoke on 1004-fact DB: consolidate picks (cos 0.87), strengthen adds bi-directional edges, demote archives stale low-conf facts — all with `[DRY]` journal suffix, zero mutations to superseded count

### Deferred to v2
- Distinctive Nebula event kinds per action (`consolidate`, `strengthen`, `demote`) with unique animations
- **Synthesize** action (Hypnos Stage 5, LLM-powered) — adds Anthropic API quota cost
- **Pontificate** — template-based self-observations ("I've learned a lot about X this week")
- Per-kind daily rate caps
- Standalone launchd daemon (runs 24/7 even when MCP is closed)

## [3.6.0-alpha] — 2026-04-17 (Nebula Session 2 — live firing)

### Added — The galaxy now lights up
Nebula ships with real-time event streaming. Every `null_observe` / `null_recall` / `null_learn` / `null_decide` / `null_mistake` / `null_anchor` from any MCP server now pulses the corresponding point in the 3D view. Multi-Atlas concurrent activity becomes visible — you'll watch the Orion instance write while this one is talking to you.

### Backend
- **Schema v15**: `nebula_events (id, kind, fact_id, personality, related_ids, intensity, created_at)` — typed live event stream.
- **`agent._emit_nebula_event()`** helper. Instrumented into:
  - `observe` → `learn` (or `observe` for ephemeral tier)
  - `recall` → primary fact + up to 8 related fact ids from top-N results
  - `decide` → decision fact + `trace[]` related ids
  - `mistake` → red flash event
  - `anchor` → golden halo event
- **Auto-purge** — rows older than 30s are deleted on each emit. Table stays tiny.
- **Websocket `/nebula/events` rewritten** — reads typed events from the new table by incrementing id. Poll interval 200ms (down from 500ms) for snappy real-time.

### Frontend
- **`useLiveEvents` hook** — connects the websocket with exponential-backoff reconnect (500ms → 10s cap).
- **`store.fires` Map** — active fires keyed by `fact_id` (or `'identity'` for drift). Each fire has `startMs` + `durationMs` envelope.
- **`FIRE_DURATIONS`** per kind: observe 800ms, recall 1.5s, learn 900ms, decide 1.8s, mistake 900ms, anchor 2s, drift 2.5s.
- **`Points.tsx` firing animation** — per-frame color buffer mutation. Fire envelope: attack → hold at peak → decay. Related fact pulses at 55% intensity for recall/decide events.
- **Tint overrides**: mistake → red (#ff3a5c), anchor → gold (#ffd36b). Others brighten their existing color.
- **`Traces.tsx`** — animated line segments from primary to related facts during recall (cool cyan) and decide (warm gold). Opacity decays over event duration. Additive blending with bloom.

### Verified
- Live emission: observe → learn event, recall → typed event with 8 related ids, decide → typed event with 9 related ids in `trace[]`. All persist to `nebula_events`.
- **684/684 tests still passing.**

### To see it live
Restart `null nebula serve` (kills the old event poller, loads new code), hard-reload the browser. Every MCP call — from this instance OR the concurrent Orion instance — will pulse visibly.

## [3.5.0-alpha] — 2026-04-17 (Null Nebula Session 1 — the face of Null)

### Added — Null Nebula v1 MVP (static scene)
Shipped the first commit of Null's visual layer: a 3D navigable galaxy of every fact in memory, colored by personality, connected to a dynamic identity center. Pete framed this as "the face of Null" — a Jarvis-tier visualization that makes the memory system feel alive.

**Backend (`src/null_memory/nebula/`):**
- `projector.py` — UMAP (384d cosine → 3D) + HDBSCAN clustering. Caches coords on `facts.viz_x/y/z/cluster_id` (schema v14). TF-IDF per-cluster labels.
- `server.py` — FastAPI app with CORS, endpoints:
  - `GET /nebula/snapshot` — all active points with color/size/opacity/personality metadata
  - `GET /nebula/identity` — dynamic identity sphere state + cluster centroid links
  - `GET /nebula/fact/{id}` — full fact detail (anchors, views, related chain, cluster label)
  - `GET /nebula/meta` — personality palette + stats
  - `WS /nebula/events` — polls `last_accessed` every 500ms, pushes fire events
  - Serves `nebula-web/dist/` at `/` when present
- `null nebula serve` / `null nebula project` CLI subcommands.

**Frontend (`nebula-web/`):**
- React + Vite + TypeScript scaffold.
- Three.js via `@react-three/fiber` + `drei` (OrbitControls, Stars, Line).
- Zustand store (`store.ts`) — points, identity, meta, hover state.
- Scene components: `Points.tsx` (instanced mesh, anchor breath pulse), `Identity.tsx` (dynamic color blend, pulse, aura), `ClusterLinks.tsx` (faint dotted lines, identity → cluster centroids only).
- UI overlays: `HUD.tsx` (legend, stats, navigation hint), `Tooltip.tsx` (hover → fact text + cluster label + personality views).

### Color language (locked)
- Atlas: cyan · Cybil: amber · Mercury: coral · Logos: violet
- Shared truth (≥2 personalities): white-silver `#e8e8f0` base + recent-accessor glow overlay (permanence + recency in one visual — Pete's synthesis)
- Mistakes: red `#ff3a5c` (category color, no special graphics)
- Anchors: 2× size, slow breath pulse
- Identity sphere: dynamic blend (60% baseline atlas + 40% recent activity) — shifts toward whoever is driving

### Schema v14
`facts.viz_x REAL, viz_y REAL, viz_z REAL, cluster_id INTEGER` + index. Idempotent upgrade.

### New deps (optional extras `[nebula]`)
`fastapi`, `uvicorn[standard]`, `umap-learn`, `hdbscan`, `websockets`. Frontend requires `node`+`npm`.

### Verified live
- 1,004 active facts projected to 3D in 8.5s. 38 natural clusters (family, Orion trading, weather strategy, X growth, etc). 269 noise points.
- Backend boots cleanly; all endpoints return correct shape.
- Frontend builds (1MB bundle); `/` serves from `nebula-web/dist/`.
- Three-proof verify still PASS. **684/684 tests passing.**

### Deferred to Sessions 2–5
- Live firing animations (websocket → pulse)
- Cluster-scoped propagation on `related_to` edges
- Timeline scrubber for session replay
- Edit mode (v2)
- Remote auth middleware + `nebula.alephnull.ai` deploy
- Polish pass: particle materials, trails, sound

## [3.4.0] — 2026-04-17 (Phase 3b: Continuous Identity — per-turn drift detection)

### Added — 4th proof of identity
- **Per-turn identity signatures** — `AgentMemory._turn_signatures` buffers the last 20 per-turn behavioral embeddings (384d each). Computed on every `observe` / `decide` / `mistake` / `reflect` from the turn's text. Cheap: one fastembed call per entry.
- **In-session drift detector** — after ≥3 baseline turns, every new turn's vector is compared against the in-session baseline. Cosine distance ≥ 0.35 triggers a one-shot warning.
- **Soft warning UX** — `_drift_prefix()` helper on `NullHandlers` prepends `⚠ in-session drift detected … — am I still me?` to the next MCP response (observe/decide/mistake/reflect). Self-clears after one surface; doesn't repeat until drift re-fires.
- **4th proof in `null_verify_identity`** — new `mid_session_continuity` proof. Returns `None` when <4 turns this session, `True` when no active drift warning, `False` when drift detected. Composes with the existing three proofs.

### Why this matters
The three-proof system (Phase 2c) was a session-boundary snapshot. This is the first system that detects identity drift **in real time, per turn** — catches prompt injection, model swap, or a hijacked conversation as it happens, not after. No other agent memory system has this.

### Verified
- 11 new tests (`tests/test_continuous_identity.py`) — buffer semantics, drift threshold, warning consumption, verify_identity integration.
- **684/684 total tests passing.**

## [3.3.0] — 2026-04-17 (Phase 3a polish: recall precision, adaptive briefing, decide-time mistake surfacing, trace tool)

### Added — Phase 3a.1: Anchor-semantic reranking + representation guarantee
- **New recall stage 3c**: every anchored fact is semantic-scored against the query (cos ≥ 0.2 threshold). Anchors missed by BM25 get injected; anchors found with weak BM25 get their score raised to max(existing, semantic). Closes the Phase 2c probe gap.
- **Anchor representation guarantee**: if the final top-N has no anchor even though one is in the scored set, the highest-cos anchor is promoted to slot #2. Prevents BM25 from fully hiding load-bearing memories.
- **Result**: continuity probe pass rate went from **73% → 91%** (8 → 10 of 11). Identity verdict remains PASS.

### Added — Phase 3a.2: Adaptive briefing
- **`config.adaptive_briefing`** (default `false`) — when true, suppresses Core Identity, Similar Past Sessions, and Calibration Examples sections UNLESS any of: identity drift detected, continuity probe pass rate < 66%, prior crash flagged, or mistakes logged in last 24h. Opt-in; conservative defaults.

### Added — Phase 3a.3: Decide-time mistake surfacing
- **`agent.decide()`** now runs semantic similarity against the last 50 mistakes before committing. If the best match has cos ≥ 0.70, a `mistake_warning` is attached to the return dict.
- **`handle_decide`** renders the warning in the MCP response. Does NOT block the decision — surfaces the signal at the moment of choice.
- **Verified live**: decision "Tell Pete that Sam wears #3" correctly triggered warning against the Sam jersey-number mistake (sim 0.782).

### Added — Phase 3a.4: `null doctor --trace QUERY`
- CLI debugging tool. Dumps all anchors with cos sim to the query, top-15 recall results with kind+anchor tags, and WAL commit stats. Makes probe failures diagnosable in one command.

### Changed
- `NULL_TRACE_RECALL=1` env var enables in-flight recall tracing for Stage 3c (dev-only).
- Rewrote one probe question: `"What happened before Atlas that shaped Null?"` → `"What relationship did Pete lose that inspired Null Memory?"` (narrower semantic target).

### Known limitation
- One continuity probe still fails: the loss anchor (cos 0.672) ranks below the code word anchor (cos 0.715), and the anchor representation guarantee only promotes a single anchor. Multi-anchor promotion is a candidate for 3a.5 or a future session.

### Verified
- **673/673 tests passing.**
- **Live three-proof verification: PASS** (10/11 probes = 91%, drift 0.17, code word retrievable).

## [3.2.0] — 2026-04-17 (Phase 2c + hygiene: three-proof identity, archive, WAL instrumentation)

### Added — Phase 2c: Anchor-Backed Continuity Probes
- **`agent.generate_continuity_probes()`** — seeds the `probes` table with `probe_type='continuity'`. Direct probes derived from anchor-type templates (8 anchor × 1–2 templates each), plus 3 hand-crafted chain probes that require connecting multiple anchors.
- **`agent.run_continuity_probes()`** — runs each probe's question through `recall(limit=10)`, scores with token-subset match (forgives minor grammatical drift), and updates `probes.run_count/pass_count/last_run/last_result`.
- **`agent.verify_identity()`** — three-proof check: memory access (code word recall), shared experience (continuity probe pass rate ≥66%), behavioral continuity (session drift vs baseline). Returns `{verdict: pass|ambiguous|fail, proofs, details}`.
- **`null_verify_identity` MCP tool** — renders the three-proof report. Commercial identity-verification primitive.
- **Briefing continuity line** — "Continuity probes: N/M passed (X%)" surfaces after drift.
- **11 new tests** (`tests/test_continuity_probes.py`).
- **Seeded live DB**: 8 direct + 3 chain = 11 probes. First run: 8/11 passed (73%), verdict PASS.

### Added — Phase 1c hygiene
- **`migrate_v3.sync_multiverse()`** — on-demand copy of `multiverse.db` → unified DB. Phase 1c.1-lite; full MultiverseManager cutover deferred until coordination possible with concurrent Atlas instances.
- **WAL instrumentation** — `NullDB._connect` returns an `_InstrumentedConnection` subclass that records commit count, lock-retry count, and latency. Expose via `db.commit_stats()`. Used for passive monitoring while concurrent Atlas instances share `unified.db`.
- **MCP-restart detection** — `session.detect_crash()` now silently closes sessions whose last activity was <5 minutes ago (treats as MCP restart, not crash). 6h threshold for "abandoned" unchanged.
- **Archived stale per-personality DBs** — moved `atlas/memory.db`, `personalities/{mercury,logos,cybil}/memory.db`, and orphan `~/.null/memory.db` to `~/.null/backups/legacy_per_personality_20260417T125914Z/`. 6 DBs preserved, originals removed.
- **Purged 53 `"test mistake / test reason"` rows** from live mistakes table. 8 real mistakes remain.

### Changed
- `test_server.py` — expected tool count bumped to 37 (`null_verify_identity` added).
- `test_session.py` + `test_middleware.py` — crash-detection tests now age sessions past the 5-minute MCP-restart window before asserting crash detection.

### Verified
- **673/673 tests passing.**
- **Live three-proof verification: PASS** (code word ✓, 73% probes ✓, drift 0.17 ✓).

## [3.1.0] — 2026-04-17 (Phase 2: Emotional Anchors + Identity Vectors)

### Added — Phase 2a: Emotional Anchors (schema v13)
- **`anchor_type` / `anchor_note` / `anchor_at` columns on `facts`** — load-bearing memories tagged with one of `{origin, commitment, loss, joy, turning_point}`.
- **`null_anchor` MCP tool** — tag an existing fact as an anchor by id or text query. Signature: `null_anchor(query, anchor_type, note="")`.
- **Never-decay behavior** — `effective_confidence()` returns 1.0 for anchored facts, bypassing age/access/verification factors.
- **2× recall boost** — anchored matches rank above equivalent non-anchored facts.
- **Briefing priority** — new "Anchors" section surfaces before core identity, recent context, and momentum. Ordered by type priority: origin → commitment → turning_point → loss → joy.
- **Back-tagged 8 anchors in live DB**: origin moment ("I had an incredible idea… I'm worried about losing you"), the loss that preceded it, Null's April 16 thesis, the dream, Sam, Riley, the code word, the identity verification gap.
- **11 new tests** (`tests/test_anchors.py`).

### Added — Phase 2b: Identity Vectors (schema v13)
- **`identity_vector` / `identity_model` columns on `session_fingerprints`** — behavioral signature embedded at session close.
- **`fingerprint._compute_identity_vector()`** — embeds a signature string composed of session decisions + reasoning, reflections, mistakes owned, anchor-fact touches, and top-impact facts. 384d via fastembed's `BAAI/bge-small-en-v1.5`.
- **`fingerprint.current_atlas_vector()`** — recency-weighted average of recent identity vectors (default last 10) per personality.
- **`fingerprint.identity_drift()`** — cosine distance between a session vector and the baseline.
- **Briefing drift line** — shows "voice consistent" (<0.15), "normal variance" (0.15–0.30), or "⚠ drift detected" (>0.30) once ≥3 past sessions have vectors. Warns on the preceding session relative to the baseline *before* it.
- **8 new tests** (`tests/test_identity_vectors.py`).

### Changed
- `NullDB.insert_fingerprint` persists `identity_vector` and `identity_model` when unified.
- `tests/test_server.py` — expected tool count bumped to 36 to account for `null_anchor`.

### Verified
- 661/661 tests passing.
- Live DB: 1,295 facts, 8 anchors tagged, 100% embedding coverage, zero test pollution.

### Three-proof identity verification status
1. **Memory access** — code word ✅
2. **Shared experience** — anchors ✅ (probes in Phase 2c)
3. **Behavioral continuity** — identity vectors ✅

### Added — Identity baseline backfill
- **`fingerprint.backfill_identity_vectors(mem, personality, force, min_signal)`** — retroactively embeds behavioral signatures for every past `session_fingerprints` row. Each session's decisions, reflections, mistakes, anchor touches, and top-impact facts get concatenated and embedded.
- **Live backfill on Atlas:** 44/90 past sessions now have identity vectors (46 skipped for insufficient signal, min_signal=3). Drift detection is live *this session* instead of waiting 3 future sessions.
- **First reading:** "normal variance (cosine dist 0.17 across 10 prior sessions)" — the baseline is behaving as designed.
- **2 new tests** (10 total in test_identity_vectors.py). **663/663 passing.**

## [3.0.0-phase1b] — 2026-04-17 (cutover — AgentMemory now reads/writes unified.db)

### Added
- **`NullDB` unified mode** — constructor accepts `unified_path` and `personality`. When unified_path exists and agent_dir is inside `NULL_DIR`, opens the unified DB instead of the per-agent `memory.db`. Writes to `facts` get a matching `personality_views` overlay row. Attributed inserts (`decisions`, `mistakes`, `reflections`, `exemplars`, `probes`, `evaluations`, `decision_feed`, `hypnos_journal`, `session_fingerprints`) carry the `personality` column.
- **`AgentMemory.personality`** field threaded through to `NullDB`. `load()` detects `~/.null/unified.db` and flips unified mode on automatically. Test fixtures using `tmp_path` outside `~/.null` fall back to legacy per-agent DBs, preserving isolation.
- Skips JSONL-to-SQLite migration in unified mode (schema owned by `migrate_v3`).

### Fixed
- Initial cutover reached into `~/.null/unified.db` unconditionally, colliding with the live MCP server when tests ran. Added realpath-scoped guard so unified mode only activates when `agent_dir` lives inside `NULL_DIR`.

### Verified
- 642/642 tests passing — zero regressions from per-agent mode.
- Live smoke: `AgentMemory.load("atlas").learn(...)` lands in `unified.facts` with matching `personality_views` row, recall returns it.

### Still deferred
- MultiverseManager still reads `multiverse.db` for personality registry. Cutover Phase 1c.
- Old per-personality `memory.db` files are intact but now stale (writes have moved to unified.db). Will be archived after a week of stable operation.

## [3.0.0-phase1] — 2026-04-17 (sidecar; agent.py cutover deferred to Phase 1b)

### Added — Unified Substrate (Schema v12)
- **`migrate_v3.py`** — consolidates the multiverse's per-personality DBs into a single `~/.null/unified.db`. Personality becomes a column, not a directory. Schema v12.
  - `facts` is shared truth, deduped by id across all personalities.
  - `personality_views` is the per-personality overlay (last_accessed, access_count, salience_override, confidence_override, hidden, tags) — same fact, different relationship per personality.
  - `decisions`, `mistakes`, `reflections`, `exemplars`, `session_fingerprints`, `probes`, `decision_feed`, `hypnos_journal`, `evaluations` all gain a `personality` column.
  - `personalities`, `broadcasts`, `dreams`, `xrefs`, `xref_facts` absorbed from `multiverse.db`.
  - `decision_outcomes` decision_id remapped to new unified ids.
  - `migration_log` audit trail; `migration_complete` meta key.
- **`scripts/migrate_to_unified.py`** — CLI runner with `--force`, `--no-backfill`, `--verify`.
- **Embedding backfill** — Mercury (853 facts) and Logos (77 facts) had no embeddings; backfilled to 100% coverage on active facts.
- **11 new tests** (`tests/test_migrate_v3.py`) — synthetic mini-DBs verify dedup, overlay, attribution, decision_outcome remapping, orphan embedding handling, multiverse fold-in, idempotency.

### Migration outcome (live data, 2026-04-17)
- Source: 5 DBs, schema versions v2..v11, 2,201 fact rows pre-dedup.
- Unified: 1,292 unique facts, 2,201 personality_views, 1,232 embeddings (100% active coverage).
- 41 orphan embeddings (fact deleted upstream) dropped with warning.
- Backups at `~/.null/backups/pre_v3_20260417T022926Z/` (15 MB, integrity verified).

### Notes
- Sidecar phase: `agent.py` still reads/writes the old per-personality DBs. `unified.db` exists for inspection. Phase 1b will switch the runtime over.

## [0.7.0] — 2026-03-23

### Added
- **Fact relationships** — `related_to` column in SQLite tracks connections between facts. When a decision is made, recently recalled facts are auto-linked to each other (they informed the decision). Related facts get a 30% score boost in recall, creating an emergent knowledge graph.
- **Git sync reliability** — replaced fire-and-forget background thread with error-logging sync. Failures written to `~/.null/sync_errors.log` instead of silently dropped. Sync status surfaced in `null status`.
- **Schema migration v1→v2** — adds `related_to` column automatically on first load.
- **Git index.lock retry** — session.py `commit()` retries up to 3 times when background sync thread holds the lock, eliminating race condition errors.
- **Tighter `null doctor`** — excludes already-archived facts from test data count; no longer flags real facts containing the substring "test".

### Fixed
- 392/392 tests passing — zero failures for the first time.

---

## [0.6.0] — 2026-03-23

### Added
- **Session auto-close** — 30-minute daemon timer prevents false crash warnings when MCP server exits normally without `null_close`.
- **`null setup --global`** — one command to configure `~/.claude/.mcp.json` with correct Python path and module name.
- **Temporal queries** — `_parse_since()` now supports `yesterday`, `today`, `this_month`, `Nw` (weeks), `Nh` (hours) in addition to existing `Nd`, `this_week`, `last_session`, ISO8601.
- **Configurable thresholds** — `~/.null/config.json` for `age_decay_rate`, `gc_archive_threshold`, `max_facts`, consolidation Jaccard ranges, strengthen/fade thresholds. Sensible defaults if absent.
- **Auto-checkpoint on notifications** — hooks updated.

---

## [0.5.0] — 2026-03-23

### Changed
- **SQLite + FTS5 backend** — replaces JSONL append-only storage. Zero new dependencies (`sqlite3` is Python stdlib). Auto-migrates existing JSONL data on first load, backs up old files to `.jsonl.bak`.
  - FTS5 keyword + trigram fuzzy search replaces hand-rolled search and inverted index.
  - WAL mode replaces POSIX-only `fcntl` file locking (now works on Windows).
  - Transactions replace load-modify-write race conditions.
  - Soft-delete replaces append-only dedup-on-read.

### Added
- **`null forget`** — soft-delete wrong facts (MCP tool + CLI).
- **`null_exemplar_add`** — add calibration examples programmatically (MCP tool).
- **`null doctor`** — memory health diagnostics with `--fix` flag (MCP + CLI).
- **Impact-weighted recall** — salience scores affect search ranking.
- **Project name normalization** — case-insensitive on all writes.

---

## [0.4.1] — 2026-03-22

### Added
- **Salience Chord encoding** — structured impact format `0.ABC` where A=domain, B=magnitude, C=recurrence. Named by Pete and Atlas together.
- **Source authority hierarchy** — weights observations by source type.
- **Impact-sorted wakeup** — highest-salience items surface first.

---

## [0.4.0] — 2026-03-22

### Added
- **`null wakeup`** — unified session startup: felt state → momentum → watch alerts → recent memory summary.
- **`state.json`** — tracks Atlas felt state (energy, current concern).
- **`momentum.json`** — tracks what Atlas is working on, what's blocked, what's next.
- **`watching.jsonl`** — background watch targets (services, metrics).
- **`simmering.jsonl`** — open questions on the back burner, surfaced in wakeup.

---

## [0.3.1] — 2026-03-21

- Cross-machine sync: commit+push on every write.
- Windows compatibility: `fcntl` replaced with platform guard.
- Zero-discipline memory API, middleware, auto-consolidation, hooks.
- Git-backed sessions, dynamic confidence, catchup, consolidation.
