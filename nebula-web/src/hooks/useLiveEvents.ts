import { useEffect } from 'react'
import { wsUrl } from '../api'
import { useNebulaStore, type FireKind, type Point } from '../store'

// Map personality lowercase → color. Kept in sync with backend palette.
const PERSONALITY_COLORS: Record<string, string> = {
  atlas:     '#00d4ff',
  cybil:     '#ffb020',
  mercury:   '#ff7a5c',
  logos:     '#b080ff',
  argus:     '#2ac09c',
  hermes:    '#d4b86a',
  kairos:    '#7aa2d6',
  mnemosyne: '#9a6ac4',
}
const SHARED_COLOR = '#e8e8f0'

function colorForPersonality(p: string | null): string {
  if (!p) return SHARED_COLOR
  return PERSONALITY_COLORS[p.toLowerCase()] || SHARED_COLOR
}

/**
 * Connect to /nebula/events websocket and push each incoming event into
 * the store's firing map. Backend pushes typed events written by
 * agent.py on every observe / recall / learn / decide / mistake / anchor.
 *
 * Auto-reconnects on disconnect with exponential backoff (capped at 10s).
 */
export function useLiveEvents() {
  const pushFire = useNebulaStore((s) => s.pushFire)
  const pruneFires = useNebulaStore((s) => s.pruneFires)
  const upsertPoint = useNebulaStore((s) => s.upsertPoint)
  const setConnection = useNebulaStore((s) => s.setConnection)
  const checkServerFreshness = useNebulaStore((s) => s.checkServerFreshness)
  const loadAll = useNebulaStore((s) => s.loadAll)

  useEffect(() => {
    let ws: WebSocket | null = null
    let reconnectTimer: number | null = null
    let backoffMs = 500
    let closed = false
    let missRefetchTimer: number | null = null

    const connect = () => {
      if (closed) return
      const url = wsUrl('/nebula/events')
      setConnection('connecting')
      ws = new WebSocket(url)

      ws.onopen = () => {
        backoffMs = 500 // reset backoff on healthy connect
        setConnection('live')
        // On reconnect, pull a fresh snapshot so we don't miss points
        // born while we were disconnected. Also checks server freshness.
        void loadAll()
      }

      // Debounced snapshot refetch. Called when a live event references
      // a fact we don't have in the store AND carries no point payload —
      // a safety net for upstream projection failures. Coalesces bursts
      // (e.g., a recall of 9 related ids) into a single refetch.
      const scheduleMissRefetch = () => {
        if (missRefetchTimer !== null) return
        missRefetchTimer = window.setTimeout(() => {
          missRefetchTimer = null
          void loadAll()
        }, 800)
      }

      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data)
          const events = Array.isArray(data.events) ? data.events : []
          // Current point id set for miss detection (cheap, used per-batch).
          const pointIds = new Set(
            useNebulaStore.getState().points.map((p) => p.id),
          )
          for (const ev of events) {
            // Session 3a: if event carries a new point payload, append it
            // to the store so the ring has a place to fire AND it appears
            // in the galaxy as an ongoing fact.
            if (ev.point && typeof ev.point.x === 'number') {
              const p = ev.point
              const isMistake = p.type === 'mistake'
              const isAnchor = !!p.anchor_type
              const size = isMistake
                ? 0.9                        // dim but boosted briefly
                : isAnchor ? 2.0
                : p.tier === 'core' ? 1.6
                : p.tier === 'durable' ? 1.3
                : p.tier === 'ephemeral' ? 0.8
                : 1.0
              const color = isMistake
                ? '#b32a3a'
                : colorForPersonality(ev.personality)
              // Newborn points start at full opacity + oversize so they're
              // the brightest thing in the galaxy for a minute. Pete wants
              // to be able to locate what was just added at a glance.
              const newPoint: Point = {
                id: p.id,
                x: p.x, y: p.y, z: p.z,
                cluster_id: p.cluster_id ?? null,
                color,
                glow: color,
                size: Math.max(size, isMistake ? 0.9 : 1.4),
                opacity: isMistake ? 0.85 : 1.0,
                anchor_type: p.anchor_type ?? null,
                tier: p.tier ?? 'contextual',
                personalities: ev.personality ? [ev.personality] : [],
                last_accessed: ev.at,
                access_count: 1,
                project: 'global',
                fact_preview: p.fact_preview ?? '',
                type: isMistake ? 'mistake' : 'fact',
              }
              upsertPoint(newPoint)
              // Track in-batch additions so later events in the same
              // batch don't falsely flag this id as "unknown" and queue
              // a needless refetch.
              pointIds.add(newPoint.id)
            } else if (ev.fact_id && !pointIds.has(ev.fact_id)) {
              // Event referenced an unknown fact and carried no point
              // payload — upstream projection likely failed. Refetch so
              // we catch any backfill that happened after the event.
              scheduleMissRefetch()
            }
            pushFire({
              kind: ev.kind as FireKind,
              fact_id: ev.fact_id ?? null,
              personality: ev.personality ?? null,
              related_ids: Array.isArray(ev.related_ids) ? ev.related_ids : [],
              intensity: typeof ev.intensity === 'number' ? ev.intensity : 1,
              at: ev.at ?? new Date().toISOString(),
            })
          }
        } catch {
          // swallow — stream must not break UI
        }
      }

      ws.onclose = () => {
        if (closed) return
        setConnection(backoffMs > 2000 ? 'down' : 'reconnecting')
        if (reconnectTimer !== null) return
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null
          backoffMs = Math.min(backoffMs * 2, 10000)
          connect()
        }, backoffMs)
      }

      ws.onerror = () => {
        // onclose fires after onerror — reconnect handled there
      }
    }

    connect()

    // Expire finished fires every 100ms
    const pruneInterval = window.setInterval(pruneFires, 100)

    // Stale-server check every 30s
    const freshnessInterval = window.setInterval(() => {
      void checkServerFreshness()
    }, 30000)

    return () => {
      closed = true
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
      if (missRefetchTimer !== null) window.clearTimeout(missRefetchTimer)
      window.clearInterval(pruneInterval)
      window.clearInterval(freshnessInterval)
      if (ws) ws.close()
    }
  }, [pushFire, pruneFires, setConnection, checkServerFreshness, loadAll])
}
