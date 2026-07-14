import { useMemo, useRef } from 'react'
import * as THREE from 'three'
import { useFrame } from '@react-three/fiber'
import { Line } from '@react-three/drei'
import { useNebulaStore } from '../store'

/**
 * Central identity sphere — dynamic color blend across active personalities.
 * Pete's spec: Q3 dynamic — sphere shifts toward whichever personality is
 * actively writing. Uses slow drift (60% baseline + 40% recent activity) to
 * breathe without hard-swinging.
 */
export function Identity() {
  const identity = useNebulaStore((s) => s.identity)
  const meta = useNebulaStore((s) => s.meta)
  const meshRef = useRef<THREE.Mesh>(null)

  // Precompute the blended color from the /nebula/identity blend weights.
  const targetColor = useMemo(() => {
    if (!identity || !meta) return new THREE.Color('#00d4ff')
    const baseline = new THREE.Color(identity.base_color)
    baseline.multiplyScalar(0.6)
    const tint = new THREE.Color('#000000')
    for (const [personality, weight] of Object.entries(identity.blend)) {
      const hex = meta.palette[personality]
      if (!hex) continue
      const c = new THREE.Color(hex).multiplyScalar(weight)
      tint.add(c)
    }
    tint.multiplyScalar(0.4)
    baseline.add(tint)
    return baseline
  }, [identity, meta])

  useFrame((state) => {
    if (!meshRef.current || !identity) return
    const t = state.clock.getElapsedTime()
    const breath = 1 + 0.08 * Math.sin(t / identity.pulse_seconds)
    meshRef.current.scale.setScalar(identity.size * 0.5 * breath)
    // Identity sphere is always breathing — keep demand loop alive at
    // a modest cadence (every ~4 frames at vsync = ~15fps min)
    if (Math.floor(t * 15) !== Math.floor((t - 0.01) * 15)) state.invalidate()
  })

  if (!identity) return null

  // Scale identity smaller now that points are visible — bloom will make
  // it feel large through halo rather than raw geometry.
  const coreRadius = identity.size * 0.5

  return (
    <>
      <mesh ref={meshRef} position={[identity.center.x, identity.center.y, identity.center.z]}>
        <sphereGeometry args={[1, 40, 40]} />
        <meshBasicMaterial color={targetColor} toneMapped={false} />
      </mesh>
      {/* Soft aura ring — larger, very faint; bloom bleeds it outward */}
      <mesh position={[identity.center.x, identity.center.y, identity.center.z]}>
        <sphereGeometry args={[coreRadius * 2.2, 20, 20]} />
        <meshBasicMaterial
          color={targetColor}
          transparent
          opacity={0.08}
          toneMapped={false}
          side={THREE.BackSide}
        />
      </mesh>
    </>
  )
}

/**
 * Faint dotted lines from identity center to each cluster centroid.
 * Per Pete's spec: identity NOT connected to every point, only to cluster groups.
 */
export function ClusterLinks() {
  const identity = useNebulaStore((s) => s.identity)

  const segments = useMemo(() => {
    if (!identity) return []
    return identity.clusters.map((c) => ({
      id: c.cluster_id,
      points: [
        [identity.center.x, identity.center.y, identity.center.z] as [number, number, number],
        [c.x, c.y, c.z] as [number, number, number],
      ],
    }))
  }, [identity])

  if (!identity) return null

  return (
    <>
      {segments.map(({ id, points }) => (
        <Line
          key={id}
          points={points}
          color="#e8e8f0"
          transparent
          opacity={0.12}
          lineWidth={1}
        />
      ))}
    </>
  )
}
