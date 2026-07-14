import { create } from 'zustand'
import { apiFetch } from './api'

export type PointType = 'fact' | 'mistake'

export type Point = {
  id: string
  x: number
  y: number
  z: number
  cluster_id: number | null
  color: string
  glow: string | null
  size: number
  opacity: number
  anchor_type: string | null
  tier: string
  personalities: string[]
  last_accessed: string | null
  access_count: number
  project: string
  fact_preview: string
  // Phase 5.3 — 'mistake' points render with a distinct dim-red aesthetic.
  // Defaults to 'fact' so older snapshots without the field still work.
  type?: PointType
  // Client-only timestamp used to reconcile live websocket points with
  // slightly stale snapshots during reconnect/refetch races.
  _localUpdateMs?: number
}

export type ClusterCentroid = {
  cluster_id: number
  x: number
  y: number
  z: number
  size: number
  label: string[]
}

export type Identity = {
  center: { x: number; y: number; z: number }
  size: number
  base_color: string
  blend: Record<string, number>
  pulse_seconds: number
  last_fingerprint_at: string | null
  drift: number | null
  clusters: ClusterCentroid[]
}

export type Meta = {
  palette: Record<string, string>
  shared_color: string
  mistake_color: string
  total_points: number
  cluster_count: number
  anchor_count: number
  cluster_labels: Record<number, string[]>
  server_version?: string
  server_boot_time?: number
}

export type ConnectionStatus = 'connecting' | 'live' | 'reconnecting' | 'down'

// Classification for a point: 'shared' if ≥2 personalities in views,
// otherwise the single personality's lowercase name.
// Core personality classes always present; user-defined personalities
// come through as arbitrary strings registered via identity.json.
export type PointClass = string

export function classifyPoint(p: Point): PointClass {
  if (p.type === 'mistake') return 'mistake'
  if (p.personalities.length >= 2) return 'shared'
  return (p.personalities[0] || 'atlas') as PointClass
}

// ── Live firing events (Session 2) ─────────────────────────────────────

export type FireKind =
  | 'observe'
  | 'recall'
  | 'learn'
  | 'decide'
  | 'mistake'
  | 'anchor'
  | 'drift'
  | 'outreach'
  // Phase 5.4 — Hypnos Live actions split from recall/mistake reuse.
  // Each has its own color + pulse so you can tell maintenance apart
  // from user-driven activity.
  | 'consolidate'
  | 'strengthen'
  | 'demote'
  // Phase 5.5 — template-based self-observation (commentary, no mutation)
  | 'pontificate'

export type FireEvent = {
  kind: FireKind
  fact_id: string | null
  personality: string | null
  related_ids: string[]
  intensity: number
  at: string
  // Client-side fields
  startMs: number     // when we received it (performance.now())
  durationMs: number  // how long the animation lasts
}

// Fire duration envelopes per kind (ms).
// Rhythm is a steady repeating pulse (see fireEnvelope). A fire keeps
// pulsing UNTIL another event on the same point replaces it — giving
// you all the time you need to investigate. Duration is a safety cap
// (effectively "forever" for normal sessions — 30 min).
//
// The fires Map in the store is keyed by fact_id (or 'identity' for
// drift). pushFire() overwrites the key, so any new event on the same
// point naturally supersedes the old one. Points that no longer have
// events just... stay lit with their last fire until session end.
const THIRTY_MIN = 30 * 60 * 1000
export const FIRE_DURATIONS: Record<FireKind, number> = {
  observe: THIRTY_MIN,
  recall:  THIRTY_MIN,
  learn:   THIRTY_MIN,
  decide:  THIRTY_MIN,
  mistake: THIRTY_MIN,
  anchor:  THIRTY_MIN,
  drift:   THIRTY_MIN,
  outreach: THIRTY_MIN,
  consolidate: THIRTY_MIN,
  strengthen:  THIRTY_MIN,
  demote:      THIRTY_MIN,
  pontificate: THIRTY_MIN,
}

// Per-kind pulse shape: number of visible pulses + what fraction of the
// total duration is "active pulsing" before the afterglow takes over.
// Each pulse = (duration * pulseFrac) / n. Slower pulses are easier to
// see; multiple pulses forgive glancing away.
//
// Example (recall): 15000ms * 0.35 / 3 = ~1.75s per slow pulse × 3 = 5.25s
// active pulsing, then 9.75s of gentle afterglow keeping the point marked.
export const FIRE_PULSES: Record<FireKind, { n: number; pulseFrac: number }> = {
  observe: { n: 3, pulseFrac: 0.30 },   // 3 × 2.2s pulses, 15.4s long fade
  recall:  { n: 3, pulseFrac: 0.35 },   // 3 × 1.4s pulses, 7.8s afterglow
  learn:   { n: 3, pulseFrac: 0.30 },   // 3 × 2.2s pulses, 15.4s long fade
  decide:  { n: 3, pulseFrac: 0.35 },   // 3 × 1.75s pulses, 9.75s afterglow
  mistake: { n: 3, pulseFrac: 0.30 },   // 3 × 1.2s pulses, 8.4s afterglow
  anchor:  { n: 3, pulseFrac: 0.35 },   // 3 × 2.1s pulses, 11.7s afterglow
  drift:   { n: 4, pulseFrac: 0.40 },   // 4 × 2.5s pulses, 15s afterglow
  outreach: { n: 3, pulseFrac: 0.35 },  // 3 × 2.1s pulses (warm/confident rhythm)
  // Phase 5.4 — Hypnos maintenance actions use quicker pulses (it's
  // background work, not user-facing signals — shouldn't demand attention).
  consolidate: { n: 2, pulseFrac: 0.25 }, // 2 slower pulses, long afterglow
  strengthen:  { n: 2, pulseFrac: 0.25 }, // 2 pulses along an edge
  demote:      { n: 2, pulseFrac: 0.20 }, // 2 dim pulses + longest fade
  pontificate: { n: 4, pulseFrac: 0.35 }, // thoughtful 4-pulse rhythm
}

// Phase 7.3 — outreach trigger management types
export type TriggerEntry = {
  id: number
  name: string
  kind: string
  payload: Record<string, unknown>
  enabled: 0 | 1
  cooldown_hours: number
  urgency: number
  last_fired_at: string | null
  last_fired_detail: string | null
  state: 'ready' | 'cooling' | 'disabled'
  next_eligible_at: string | null
}

export type OutreachEntry = {
  id: number
  trigger_id: number | null
  trigger_name: string | null
  trigger_kind: string | null
  personality: string | null
  channel: string
  subject: string | null
  body: string
  urgency: number
  delivered: 0 | 1
  sent_at: string
  acknowledged_at: string | null
}

type NebulaStore = {
  points: Point[]
  identity: Identity | null
  meta: Meta | null
  hoveredId: string | null
  selectedId: string | null
  loading: boolean
  // Visibility toggles — each legend entry maps to a PointClass.
  visible: Record<PointClass, boolean>
  // Active firing animations — key is fact_id (for point pulses) or
  // special keys like 'identity' for drift.
  fires: Map<string, FireEvent>
  // Event log for future event panel (capped)
  eventLog: FireEvent[]
  // Connection + freshness state
  connection: ConnectionStatus
  bootTimeSeenAt: number | null   // first /nebula/meta boot_time we saw
  serverRestartedToast: boolean   // toggled on when we detect a new boot
  // Focus request — incremented whenever a click wants to recenter the camera
  focusTick: number
  // Phase 7.3 — outreach trigger panel state
  triggers: TriggerEntry[]
  outreaches: OutreachEntry[]
  setHovered: (id: string | null) => void
  setSelected: (id: string | null) => void
  toggleClass: (cls: PointClass) => void
  setAllVisible: (on: boolean) => void
  pushFire: (ev: Omit<FireEvent, 'startMs' | 'durationMs'>) => void
  pruneFires: () => void
  upsertPoint: (p: Point) => void
  requestFocus: (factId: string) => void
  setConnection: (c: ConnectionStatus) => void
  checkServerFreshness: () => Promise<void>
  dismissRestartToast: () => void
  loadAll: () => Promise<void>
  // Phase 7.3 actions
  loadTriggersAndOutreaches: () => Promise<void>
  setTriggerEnabled: (triggerId: number, enabled: boolean) => Promise<void>
  acknowledgeOutreach: (outreachId: number) => Promise<void>
}

const LIVE_POINT_GRACE_MS = 10000

export function mergeSnapshotPoints(
  snapshotPoints: Point[],
  localPoints: Point[],
  fetchStartMs: number,
  nowMs: number = Date.now(),
): Point[] {
  const keepLocalAfterMs = Math.max(0, fetchStartMs - LIVE_POINT_GRACE_MS)
  const localById = new Map(localPoints.map((p) => [p.id, p]))
  const merged = snapshotPoints.map((sp) => {
    const lp = localById.get(sp.id)
    if (lp?._localUpdateMs && lp._localUpdateMs >= keepLocalAfterMs) {
      return lp
    }
    return sp
  })
  const mergedIds = new Set(merged.map((p) => p.id))
  for (const lp of localPoints) {
    if (mergedIds.has(lp.id)) continue
    if (lp._localUpdateMs && nowMs - lp._localUpdateMs <= LIVE_POINT_GRACE_MS) {
      merged.push(lp)
    }
  }
  return merged
}

export const useNebulaStore = create<NebulaStore>((set, get) => ({
  points: [],
  identity: null,
  meta: null,
  hoveredId: null,
  selectedId: null,
  loading: false,
  // Visibility is dynamic — any personality name we see via the snapshot
  // or palette is auto-added via setDefaultVisibleForPersonality. We
  // seed only the non-personality toggles (shared, mistake).
  visible: {
    shared: true,
    mistake: true,
  } as Record<string, boolean>,
  setHovered: (id) => set({ hoveredId: id }),
  setSelected: (id) => set({ selectedId: id }),
  toggleClass: (cls) =>
    set((state) => ({
      visible: { ...state.visible, [cls]: !state.visible[cls] },
    })),
  setAllVisible: (on) =>
    set((state) => ({
      visible: Object.fromEntries(
        Object.keys(state.visible).map((k) => [k, on]),
      ) as Record<string, boolean>,
    })),
  fires: new Map<string, FireEvent>(),
  eventLog: [],
  connection: 'connecting',
  bootTimeSeenAt: null,
  serverRestartedToast: false,
  focusTick: 0,
  triggers: [],
  outreaches: [],
  setConnection: (c) => set({ connection: c }),
  checkServerFreshness: async () => {
    try {
      const r = await apiFetch('/nebula/meta')
      if (!r.ok) return
      const m = await r.json()
      set((state) => {
        if (state.bootTimeSeenAt == null) {
          return {
            bootTimeSeenAt: m.server_boot_time || 0,
            meta: { ...(state.meta || m), ...m },
          }
        }
        const newBoot = m.server_boot_time || 0
        if (newBoot > state.bootTimeSeenAt + 1) {
          // Server restarted since we loaded → new boot time
          return {
            serverRestartedToast: true,
            bootTimeSeenAt: newBoot,
            meta: { ...(state.meta || m), ...m },
          }
        }
        return { meta: { ...(state.meta || m), ...m } }
      })
    } catch {
      // swallow — network errors are handled via ws status
    }
  },
  dismissRestartToast: () => set({ serverRestartedToast: false }),
  // Phase 7.3 — fetch triggers + outreaches into the store. Called by
  // loadAll() and on a 30s interval from the panel itself.
  loadTriggersAndOutreaches: async () => {
    try {
      const [tRes, oRes] = await Promise.all([
        apiFetch('/nebula/triggers'),
        apiFetch('/nebula/outreaches?limit=20'),
      ])
      const tBody = tRes.ok ? await tRes.json() : { triggers: [] }
      const oBody = oRes.ok ? await oRes.json() : { outreaches: [] }
      set({
        triggers: tBody.triggers || [],
        outreaches: oBody.outreaches || [],
      })
    } catch {
      // Non-fatal: keep stale data if fetch fails
    }
  },
  setTriggerEnabled: async (triggerId, enabled) => {
    try {
      const r = await apiFetch(`/nebula/triggers/${triggerId}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ enabled: enabled ? 1 : 0 }),
      })
      if (!r.ok) return
      // Optimistic local update + reload for canonical state
      set((state) => ({
        triggers: state.triggers.map((t) =>
          t.id === triggerId ? { ...t, enabled: enabled ? 1 : 0,
            state: enabled ? (t.last_fired_at ? t.state : 'ready') : 'disabled' } : t,
        ),
      }))
    } catch { /* non-fatal */ }
  },
  acknowledgeOutreach: async (outreachId) => {
    try {
      const r = await apiFetch(`/nebula/outreaches/${outreachId}/acknowledge`, {
        method: 'POST',
      })
      if (!r.ok) return
      const nowIso = new Date().toISOString()
      set((state) => ({
        outreaches: state.outreaches.map((o) =>
          o.id === outreachId ? { ...o, acknowledged_at: nowIso } : o,
        ),
      }))
    } catch { /* non-fatal */ }
  },
  pushFire: (ev) => {
    const durationMs = FIRE_DURATIONS[ev.kind] ?? 800
    const full: FireEvent = {
      ...ev,
      startMs: performance.now(),
      durationMs,
    }
    set((state) => {
      const fires = new Map(state.fires)
      // Key: fact_id for point fires, 'identity' for drift events
      const key = ev.kind === 'drift' ? 'identity' : ev.fact_id || ''
      if (key) fires.set(key, full)
      // Cap event log at 200
      const eventLog = [full, ...state.eventLog].slice(0, 200)
      return { fires, eventLog }
    })
  },
  pruneFires: () => {
    const now = performance.now()
    set((state) => {
      let changed = false
      const fires = new Map(state.fires)
      for (const [k, ev] of fires.entries()) {
        if (now - ev.startMs > ev.durationMs) {
          fires.delete(k)
          changed = true
        }
      }
      return changed ? { fires } : {}
    })
  },
  upsertPoint: (p) =>
    set((state) => {
      const pWithMs = { ...p, _localUpdateMs: Date.now() }
      const idx = state.points.findIndex((x) => x.id === pWithMs.id)
      if (idx >= 0) {
        const next = state.points.slice()
        next[idx] = { ...next[idx], ...pWithMs }
        return { points: next }
      }
      return { points: [...state.points, pWithMs] }
    }),
  requestFocus: (factId) => {
    // Selects the fact, bumps focus tick (triggers camera recenter),
    // and re-fires the last logged event on this fact so the pulse
    // attacks fresh — making "clicked event" visually obvious.
    set((state) => {
      const relevant = state.eventLog.find((ev) => ev.fact_id === factId)
      const fires = new Map(state.fires)
      if (relevant) {
        const refreshed: FireEvent = {
          ...relevant,
          startMs: performance.now(),
        }
        fires.set(factId, refreshed)
      }
      return {
        selectedId: factId,
        focusTick: state.focusTick + 1,
        fires,
      }
    })
  },
  loadAll: async () => {
    set({ loading: true })
    const fetchStartMs = Date.now()
    try {
      const [snapRes, identRes, metaRes, recentRes] = await Promise.all([
        apiFetch('/nebula/snapshot'),
        apiFetch('/nebula/identity'),
        apiFetch('/nebula/meta'),
        apiFetch('/nebula/recent-events').catch(() => null),
      ])
      const snap = await snapRes.json()
      const ident = await identRes.json()
      const meta = await metaRes.json()
      // Phase 5.3b — seed the EventLog from server-side recent events so
      // refreshing doesn't blank out the history panel. Silent fallback
      // if the endpoint isn't available (older server build).
      let recentLog: FireEvent[] = []
      if (recentRes && recentRes.ok) {
        try {
          const recent = await recentRes.json()
          const nowMs = Date.now()
          recentLog = (recent.events || []).map((ev: any): FireEvent => {
            const kind = ev.kind as FireKind
            const defaultDuration = FIRE_DURATIONS[kind] ?? FIRE_DURATIONS.recall
            return {
              kind,
              fact_id: ev.fact_id ?? null,
              personality: ev.personality ?? null,
              related_ids: Array.isArray(ev.related_ids) ? ev.related_ids : [],
              intensity: typeof ev.intensity === 'number' ? ev.intensity : 1,
              at: ev.at ?? new Date().toISOString(),
              startMs: nowMs - defaultDuration,  // already expired — log-only
              durationMs: defaultDuration,
            }
          })
        } catch { /* non-fatal */ }
      }
      // Auto-register visibility for any personality the backend knows
      // about (core + user-defined). Preserves existing toggles; new
      // personalities default to visible.
      const paletteKeys = Object.keys(meta.palette || {})
      set((state) => {
        const vis = { ...state.visible }
        for (const k of paletteKeys) {
          if (!(k in vis)) vis[k] = true
        }
        const mergedPoints = mergeSnapshotPoints(
          snap.points,
          state.points,
          fetchStartMs,
        )
        return {
          points: mergedPoints,
          identity: ident,
          meta,
          eventLog: recentLog,
          loading: false,
          bootTimeSeenAt: meta.server_boot_time || null,
          visible: vis,
        }
      })
      // Phase 7.3 — pull triggers + recent outreaches alongside the
      // initial snapshot so the panel renders without a second wait.
      void get().loadTriggersAndOutreaches()
    } catch (e) {
      console.error('Nebula load failed', e)
      set({ loading: false })
    }
  },
}))
