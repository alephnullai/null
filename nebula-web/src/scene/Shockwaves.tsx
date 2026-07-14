import { useMemo, useRef } from 'react'
import * as THREE from 'three'
import { useFrame } from '@react-three/fiber'
import { Billboard } from '@react-three/drei'
import { useNebulaStore, FIRE_PULSES, type FireEvent } from '../store'

/**
 * Shockwave rings — a camera-facing expanding circle for each active
 * firing event. Emanates from the firing fact's position, grows 1× → 3×
 * during a pulse, opacity decays. Far more visible than a simple
 * brightness bump because your eye tracks motion.
 *
 * One ring per active fire. Pulses N times (matching the point envelope)
 * so you can't miss it even on a quick glance.
 */
export function Shockwaves() {
  const points = useNebulaStore((s) => s.points)
  const identity = useNebulaStore((s) => s.identity)

  // Id → position map (includes identity center for drift events)
  const positionsById = useMemo(() => {
    const m = new Map<string, [number, number, number]>()
    for (const p of points) m.set(p.id, [p.x, p.y, p.z])
    if (identity) {
      m.set('identity', [identity.center.x, identity.center.y, identity.center.z])
    }
    return m
  }, [points, identity])

  // Subscribe reactively to fires so the component re-renders when
  // fires are added or pruned (not just on point changes).
  const fires = useNebulaStore((s) => s.fires)
  const activeList = useMemo(() => {
    const out: {
      key: string
      ev: FireEvent
      pos: [number, number, number]
      isPrimary: boolean
    }[] = []
    for (const [key, ev] of fires.entries()) {
      // Primary ring on the event's main fact_id
      const primaryPos = positionsById.get(
        key === 'identity' ? 'identity' : ev.fact_id || ''
      )
      if (primaryPos) {
        out.push({ key, ev, pos: primaryPos, isPrimary: true })
      }
      // For recall/decide: mini rings on each related fact too, so the
      // cascade reads as distinct flashes across the galaxy.
      if (ev.kind === 'recall' || ev.kind === 'decide') {
        for (const rid of ev.related_ids) {
          const rp = positionsById.get(rid)
          if (rp) {
            out.push({ key: `${key}:${rid}`, ev, pos: rp, isPrimary: false })
          }
        }
      }
    }
    return out
  }, [fires, positionsById])

  return (
    <>
      {activeList.map(({ key, ev, pos, isPrimary }) => (
        <Shockwave key={key} ev={ev} position={pos} scaleMult={isPrimary ? 1 : 0.55} />
      ))}
    </>
  )
}

/** Ring texture — thin bright circle, additive-friendly. */
function buildRingTexture(): THREE.Texture {
  const size = 256
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')!
  ctx.clearRect(0, 0, size, size)
  const cx = size / 2
  const cy = size / 2
  const outer = size * 0.48
  const inner = size * 0.40
  // Soft gradient ring: bright edge, fades inward and outward
  const img = ctx.getImageData(0, 0, size, size)
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - cx
      const dy = y - cy
      const r = Math.sqrt(dx * dx + dy * dy)
      let a = 0
      if (r < inner) a = Math.max(0, 1 - (inner - r) / (inner * 0.45))
      else if (r < outer) a = 1
      else a = Math.max(0, 1 - (r - outer) / (size * 0.1))
      const i = (y * size + x) * 4
      img.data[i] = 255
      img.data[i + 1] = 255
      img.data[i + 2] = 255
      img.data[i + 3] = Math.round(a * 255)
    }
  }
  ctx.putImageData(img, 0, 0)
  const tex = new THREE.CanvasTexture(canvas)
  tex.needsUpdate = true
  return tex
}

// Shared module-level texture — all shockwaves reuse it
let _ringTex: THREE.Texture | null = null
function ringTexture(): THREE.Texture {
  if (!_ringTex) _ringTex = buildRingTexture()
  return _ringTex
}

function ringColor(ev: FireEvent): THREE.Color {
  switch (ev.kind) {
    case 'mistake':     return new THREE.Color('#ff3a5c')
    case 'anchor':      return new THREE.Color('#ffd36b')
    case 'decide':      return new THREE.Color('#ffc870')
    case 'drift':       return new THREE.Color('#ff8844')
    case 'outreach':    return new THREE.Color('#ffa54a') // warm amber
    case 'consolidate': return new THREE.Color('#8affc1') // mint — merge
    case 'strengthen':  return new THREE.Color('#6aa3ff') // steel blue — bond
    case 'demote':      return new THREE.Color('#7a828f') // dim grey — fade
    case 'pontificate': return new THREE.Color('#c9a8ff') // soft lavender
    default:            return new THREE.Color('#7fe4ff') // cyan: observe/learn/recall
  }
}

function Shockwave({
  ev,
  position,
  scaleMult = 1,
}: {
  ev: FireEvent
  position: [number, number, number]
  scaleMult?: number
}) {
  const matRef = useRef<THREE.MeshBasicMaterial>(null)
  const groupRef = useRef<THREE.Group>(null)
  const color = useMemo(() => ringColor(ev), [ev])
  const tex = ringTexture()

  // Birth events (observe/learn) — slightly larger baseline so new
  // points are findable, but not overwhelming.
  const isBirth = ev.kind === 'observe' || ev.kind === 'learn'
  const BASE = (isBirth ? 1.4 : 1.0) * scaleMult
  // Seconds per pulse — all event kinds now share a steady rhythm.
  const PULSE_SECONDS = isBirth ? 1.8 : 1.4
  // Peak opacity per pulse (brighter for birth so they stand out).
  const PEAK_OPACITY = isBirth ? 0.85 : 0.6

  useFrame((state) => {
    const mesh = groupRef.current
    const mat = matRef.current
    if (!mesh || !mat) return
    const now = performance.now()
    const elapsed = (now - ev.startMs) / 1000   // seconds
    const t = (now - ev.startMs) / ev.durationMs
    if (t < 0 || t >= 1) {
      mat.opacity = 0
      return
    }
    // Keep the demand loop alive while this ring is animating
    state.invalidate()
    // Continuous rhythmic pulses — small rings that keep breathing until
    // the event expires (or is replaced by a new event on this fact).
    // No afterglow / no growth phase — just steady pulse-pulse-pulse.
    const pulsePhase = (elapsed / PULSE_SECONDS) % 1
    // Attack 0→0.25, sustain 0.25→0.55, decay 0.55→1.0
    let env: number
    if (pulsePhase < 0.25) env = pulsePhase / 0.25
    else if (pulsePhase < 0.55) env = 1
    else env = Math.max(0, 1 - (pulsePhase - 0.55) / 0.45)

    // Small scale variance so each pulse feels alive (1.0 → 1.25 through
    // the breath). No runaway growth.
    const scale = 1.0 + env * 0.25
    mesh.scale.setScalar(BASE * scale * (ev.intensity || 1))
    mat.opacity = env * PEAK_OPACITY
  })

  return (
    <Billboard position={position}>
      <group ref={groupRef}>
        <mesh>
          <planeGeometry args={[1, 1]} />
          <meshBasicMaterial
            ref={matRef}
            map={tex}
            color={color}
            transparent
            opacity={0}
            toneMapped={false}
            depthWrite={false}
            blending={THREE.AdditiveBlending}
            side={THREE.DoubleSide}
          />
        </mesh>
      </group>
    </Billboard>
  )
}
