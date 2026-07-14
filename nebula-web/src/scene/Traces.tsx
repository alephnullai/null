import { useMemo, useRef } from 'react'
import * as THREE from 'three'
import { useFrame } from '@react-three/fiber'
import { useNebulaStore, FIRE_PULSES } from '../store'

/**
 * Animated trace lines — drawn briefly from a primary fact to each of
 * its related facts during `recall` and `decide` events.
 *
 * Implementation: one LineSegments mesh. Every frame, we rebuild the
 * vertex buffer from current fires. Opacity is driven per-segment by
 * the fire envelope. Additive blending + bloom gives the trace an
 * ethereal feel without a custom shader.
 */
export function Traces() {
  const points = useNebulaStore((s) => s.points)
  const geomRef = useRef<THREE.BufferGeometry>(null)
  const materialRef = useRef<THREE.LineBasicMaterial>(null)

  // Build an id→position map once per `points` change
  const positionsById = useMemo(() => {
    const m = new Map<string, [number, number, number]>()
    for (const p of points) m.set(p.id, [p.x, p.y, p.z])
    return m
  }, [points])

  // Pre-allocated buffer — cap at 5000 segments (10000 vertices).
  const MAX_SEGMENTS = 5000
  const positionsArr = useMemo(() => new Float32Array(MAX_SEGMENTS * 2 * 3), [])
  const colorsArr = useMemo(() => new Float32Array(MAX_SEGMENTS * 2 * 3), [])

  useFrame((state) => {
    if (!geomRef.current) return
    const fires = useNebulaStore.getState().fires
    const now = performance.now()

    // Phase 5.4 — traces render for any kind that has related_ids
    // meaning "this point connects to these others right now."
    const traceKinds = new Set(['recall', 'decide', 'consolidate', 'strengthen'])

    let hasActiveTrace = false
    for (const ev of fires.values()) {
      if (traceKinds.has(ev.kind)) {
        hasActiveTrace = true
        break
      }
    }
    if (hasActiveTrace) state.invalidate()

    let segIdx = 0
    for (const ev of fires.values()) {
      if (!traceKinds.has(ev.kind)) continue
      if (!ev.fact_id) continue
      const start = positionsById.get(ev.fact_id)
      if (!start) continue
      const t = (now - ev.startMs) / ev.durationMs
      if (t < 0 || t >= 1) continue
      // Same continuous-rhythm envelope as points and rings — all in sync.
      const elapsedSec = (now - ev.startMs) / 1000
      const pulseSec = 1.4
      const phase = (elapsedSec / pulseSec) % 1
      let env: number
      if (phase < 0.25) env = phase / 0.25
      else if (phase < 0.55) env = 1
      else env = Math.max(0, 1 - (phase - 0.55) / 0.45)
      // Traces stay readable indefinitely — each event persists until a
      // new event replaces it on the same fact. No time-based fade.
      const alpha = env * (ev.intensity || 1)
      const tint: [number, number, number] =
        ev.kind === 'decide'       ? [1.00, 0.84, 0.42] : // warm gold
        ev.kind === 'consolidate'  ? [0.54, 1.00, 0.76] : // mint (#8affc1)
        ev.kind === 'strengthen'   ? [0.42, 0.64, 1.00] : // steel blue
                                     [0.40, 0.82, 1.00]   // cool cyan (recall)

      for (const rid of ev.related_ids) {
        if (segIdx >= MAX_SEGMENTS) break
        const end = positionsById.get(rid)
        if (!end) continue
        const base = segIdx * 6
        positionsArr[base + 0] = start[0]
        positionsArr[base + 1] = start[1]
        positionsArr[base + 2] = start[2]
        positionsArr[base + 3] = end[0]
        positionsArr[base + 4] = end[1]
        positionsArr[base + 5] = end[2]
        const c0 = segIdx * 6
        colorsArr[c0 + 0] = tint[0] * alpha
        colorsArr[c0 + 1] = tint[1] * alpha
        colorsArr[c0 + 2] = tint[2] * alpha
        colorsArr[c0 + 3] = tint[0] * alpha
        colorsArr[c0 + 4] = tint[1] * alpha
        colorsArr[c0 + 5] = tint[2] * alpha
        segIdx++
      }
    }

    // Zero out unused tail so we don't render stale segments
    for (let i = segIdx * 6; i < MAX_SEGMENTS * 6; i++) {
      positionsArr[i] = 0
      colorsArr[i] = 0
    }

    const posAttr = geomRef.current.attributes.position as THREE.BufferAttribute
    const colAttr = geomRef.current.attributes.color as THREE.BufferAttribute
    posAttr.needsUpdate = true
    colAttr.needsUpdate = true
    // Only render the active segment range for perf
    geomRef.current.setDrawRange(0, segIdx * 2)
  })

  return (
    <lineSegments>
      <bufferGeometry ref={geomRef}>
        <bufferAttribute
          attach="attributes-position"
          args={[positionsArr, 3]}
        />
        <bufferAttribute
          attach="attributes-color"
          args={[colorsArr, 3]}
        />
      </bufferGeometry>
      <lineBasicMaterial
        ref={materialRef}
        vertexColors
        transparent
        toneMapped={false}
        blending={THREE.AdditiveBlending}
        depthWrite={false}
      />
    </lineSegments>
  )
}
