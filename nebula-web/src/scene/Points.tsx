import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { useFrame, useThree } from '@react-three/fiber'
import { useNebulaStore, type Point, type FireEvent, FIRE_PULSES } from '../store'

/**
 * Galaxy point rendering — two passes:
 *   1. Non-anchor points   — small, per-vertex colored
 *   2. Anchor points       — larger, brighter
 *
 * Uses THREE.Points with a circle sprite texture + per-vertex color
 * attribute + additive blending. Bloom bleeds halos from bright colors.
 *
 * Live firing (Session 2): firing points scale up and brighten over the
 * duration of their event envelope. Points are addressed by fact_id via
 * the fires Map in the store.
 */
export function Points() {
  const all = useNebulaStore((s) => s.points)
  const visible = useNebulaStore((s) => s.visible)
  const setHovered = useNebulaStore((s) => s.setHovered)
  const requestFocus = useNebulaStore((s) => s.requestFocus)
  const invalidate = useThree((s) => s.invalidate)

  // Wake the renderer whenever a new fire lands (demand-mode optimization)
  useEffect(() => {
    const unsub = useNebulaStore.subscribe((state, prev) => {
      if (state.fires !== prev.fires) invalidate()
    })
    return unsub
  }, [invalidate])

  const { regular, anchors, mistakes } = useMemo(() => {
    const filtered = all.filter((p) => {
      if (p.type === 'mistake') return visible.mistake
      const isShared = p.personalities.length >= 2
      const byPersonality = p.personalities.some(
        (pp) => (visible as any)[pp] === true,
      )
      const bySharedFlag = isShared && visible.shared
      return byPersonality || bySharedFlag
    })
    const facts = filtered.filter((p) => p.type !== 'mistake')
    return {
      regular: facts.filter((p) => !p.anchor_type),
      anchors: facts.filter((p) => !!p.anchor_type),
      // Phase 5.3 — mistakes render in their own pass with smaller base
      // size and lower opacity floor so they sit subtly in the galaxy
      // until a fire highlights one.
      mistakes: filtered.filter((p) => p.type === 'mistake'),
    }
  }, [all, visible])

  return (
    <>
      <PointCloud
        points={regular}
        size={0.7}
        opacityFloor={0.4}
        onHover={setHovered}
        onClick={(id) => id && requestFocus(id)}
      />
      <PointCloud
        points={anchors}
        size={1.8}
        opacityFloor={0.9}
        onHover={setHovered}
        onClick={(id) => id && requestFocus(id)}
      />
      <PointCloud
        points={mistakes}
        size={0.5}
        opacityFloor={0.3}
        onHover={setHovered}
        onClick={(id) => id && requestFocus(id)}
      />
    </>
  )
}

/** Radial alpha mask so square point sprites render as soft circles. */
function buildCircleTexture(): THREE.Texture {
  const size = 128
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')!
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
  g.addColorStop(0, 'rgba(255,255,255,1)')
  g.addColorStop(0.25, 'rgba(255,255,255,1)')
  g.addColorStop(0.55, 'rgba(255,255,255,0.55)')
  g.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, size, size)
  const tex = new THREE.CanvasTexture(canvas)
  tex.needsUpdate = true
  return tex
}

/** Base color per event kind — used to tint firing points briefly.
 *  Returns null = keep point's own color (just scale pulse). */
function fireTint(ev: FireEvent): THREE.Color | null {
  switch (ev.kind) {
    case 'mistake':
      return new THREE.Color('#ff3a5c')
    case 'anchor':
      return new THREE.Color('#ffd36b') // golden
    case 'outreach':
      return new THREE.Color('#ffa54a') // warm amber — Atlas reaching out
    // Phase 5.4 — Hypnos maintenance kinds (distinct from user activity).
    case 'consolidate':
      return new THREE.Color('#8affc1') // mint green — two becoming one
    case 'strengthen':
      return new THREE.Color('#6aa3ff') // steel blue — bond forming
    case 'demote':
      return new THREE.Color('#7a828f') // dim grey — fading into the floor
    case 'pontificate':
      return new THREE.Color('#c9a8ff') // soft lavender — a passing thought
    default:
      return null
  }
}

/**
 * Continuous-rhythm envelope — steady repeating pulses that keep
 * breathing until the event duration expires (or a new event replaces
 * it on the same fact). No growth phase, no afterglow — just pulse.
 *
 * All event kinds share the same rhythm so rings + points + traces
 * pulse in sync.
 */
function fireEnvelope(t: number, kind: string, elapsedSec: number): number {
  if (t <= 0 || t >= 1) return 0
  const pulseSec = kind === 'observe' || kind === 'learn' ? 1.8 : 1.4
  const phase = (elapsedSec / pulseSec) % 1
  if (phase < 0.25) return phase / 0.25
  if (phase < 0.55) return 1.0
  return Math.max(0, 1 - (phase - 0.55) / 0.45)
}

function PointCloud({
  points,
  size,
  opacityFloor,
  onHover,
  onClick,
}: {
  points: Point[]
  size: number
  opacityFloor: number
  onHover: (id: string | null) => void
  onClick: (id: string | null) => void
}) {
  const ref = useRef<THREE.Points>(null)
  const geomRef = useRef<THREE.BufferGeometry>(null)
  const sprite = useMemo(() => buildCircleTexture(), [])
  const tmpColor = useMemo(() => new THREE.Color(), [])

  // Precompute base positions + base colors
  const { positions, baseColors } = useMemo(() => {
    const n = points.length
    const positions = new Float32Array(n * 3)
    const baseColors = new Float32Array(n * 3)
    const c = new THREE.Color()
    for (let i = 0; i < n; i++) {
      const p = points[i]
      positions[i * 3] = p.x
      positions[i * 3 + 1] = p.y
      positions[i * 3 + 2] = p.z
      c.set(p.color)
      const scale = opacityFloor + (1 - opacityFloor) * p.opacity
      baseColors[i * 3] = c.r * scale
      baseColors[i * 3 + 1] = c.g * scale
      baseColors[i * 3 + 2] = c.b * scale
    }
    return { positions, baseColors }
  }, [points, opacityFloor])

  // Working color buffer — per-frame mutated
  const liveColors = useMemo(() => new Float32Array(baseColors), [baseColors])

  // Map id → index for fast lookup when applying fire events
  const idToIndex = useMemo(() => {
    const m = new Map<string, number>()
    for (let i = 0; i < points.length; i++) m.set(points[i].id, i)
    return m
  }, [points])

  useFrame((state) => {
    if (!geomRef.current) return
    const fires = useNebulaStore.getState().fires
    const now = performance.now()

    // Fast path: no active fires → ensure buffer is base colors, stop
    // requesting frames. Canvas goes idle until new event or user input.
    if (fires.size === 0) {
      if (liveColors.some((v, i) => v !== baseColors[i])) {
        liveColors.set(baseColors)
        ;(geomRef.current.attributes.color as THREE.BufferAttribute).needsUpdate = true
      }
      return
    }

    // Active fires exist → request next frame to keep pulses animating.
    // In demand mode this is what keeps the loop running.
    state.invalidate()

    // Start from base every frame (cheap — 3*N floats)
    liveColors.set(baseColors)

    // Apply each active fire
    for (const ev of fires.values()) {
      const t = (now - ev.startMs) / ev.durationMs
      if (t < 0 || t >= 1) continue
      const elapsedSec = (now - ev.startMs) / 1000
      const env = fireEnvelope(t, ev.kind, elapsedSec) * ev.intensity
      // Primary fact
      if (ev.fact_id) {
        const idx = idToIndex.get(ev.fact_id)
        if (idx !== undefined) applyFire(liveColors, baseColors, idx, env, ev, tmpColor)
      }
      // Related fact pulses (recall / decide) — slightly dimmer
      const secondaryEnv = env * 0.6
      for (const rid of ev.related_ids) {
        const idx = idToIndex.get(rid)
        if (idx !== undefined) applyFire(liveColors, baseColors, idx, secondaryEnv, ev, tmpColor)
      }
    }

    ;(geomRef.current.attributes.color as THREE.BufferAttribute).needsUpdate = true
  })

  if (points.length === 0) return null

  const handleOver = (e: any) => {
    e.stopPropagation()
    const idx = e.index
    const id = points[idx]?.id
    if (id) onHover(id)
    document.body.style.cursor = 'pointer'
  }
  const handleOut = () => {
    onHover(null)
    document.body.style.cursor = 'default'
  }
  const handleClick = (e: any) => {
    e.stopPropagation()
    const idx = e.index
    const id = points[idx]?.id
    if (id) onClick(id)
  }

  return (
    <points
      ref={ref}
      onPointerOver={handleOver}
      onPointerOut={handleOut}
      onClick={handleClick}
    >
      <bufferGeometry ref={geomRef}>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
        <bufferAttribute attach="attributes-color" args={[liveColors, 3]} />
      </bufferGeometry>
      <pointsMaterial
        size={size}
        sizeAttenuation
        vertexColors
        toneMapped={false}
        transparent
        opacity={1}
        depthWrite={false}
        blending={THREE.AdditiveBlending}
        map={sprite}
        alphaTest={0.02}
      />
    </points>
  )
}

/** Brighten or tint a single point index by `env` magnitude. Amplitudes
 *  tuned so a firing point is unmistakable against its dim neighbors —
 *  bloom amplifies the bright channel and bleeds a halo. */
function applyFire(
  live: Float32Array,
  base: Float32Array,
  idx: number,
  env: number,
  ev: FireEvent,
  tmp: THREE.Color,
) {
  if (env <= 0) return
  const tint = fireTint(ev)
  if (tint) {
    // Crossfade from base toward tint by env. Boost tint 2.4× so bloom
    // picks it up strongly.
    const br = base[idx * 3], bg = base[idx * 3 + 1], bb = base[idx * 3 + 2]
    live[idx * 3] = br + (tint.r * 2.4 - br) * env
    live[idx * 3 + 1] = bg + (tint.g * 2.4 - bg) * env
    live[idx * 3 + 2] = bb + (tint.b * 2.4 - bb) * env
  } else {
    // Brighten by (1 + env * 5) — at peak env=1, points are 6× base.
    // Loud enough to pop against the galaxy; bloom bleeds hard.
    const k = 1 + env * 5.0
    live[idx * 3] = base[idx * 3] * k
    live[idx * 3 + 1] = base[idx * 3 + 1] * k
    live[idx * 3 + 2] = base[idx * 3 + 2] * k
  }
}
