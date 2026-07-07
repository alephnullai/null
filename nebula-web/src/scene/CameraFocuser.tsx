import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { useFrame, useThree } from '@react-three/fiber'
import { useNebulaStore } from '../store'

/**
 * Watches focusTick + selectedId. When it changes, smoothly lerps the
 * OrbitControls target toward the selected fact's position over ~1.2s.
 *
 * Camera stays put; target moves. View rotates to look at the fact.
 * User can still pan/zoom freely after the animation settles.
 */
export function CameraFocuser({
  controlsRef,
}: {
  controlsRef: React.MutableRefObject<any>
}) {
  const selectedId = useNebulaStore((s) => s.selectedId)
  const focusTick = useNebulaStore((s) => s.focusTick)
  const points = useNebulaStore((s) => s.points)
  const identity = useNebulaStore((s) => s.identity)
  const { camera } = useThree()

  const goalRef = useRef<THREE.Vector3 | null>(null)
  const animUntil = useRef<number>(0)
  const startDistance = useRef<number>(0)

  useEffect(() => {
    if (focusTick === 0 || !selectedId) return
    const p = points.find((x) => x.id === selectedId)
    if (!p) return
    goalRef.current = new THREE.Vector3(p.x, p.y, p.z)
    animUntil.current = performance.now() + 1200  // ~1.2s animation

    // Also nudge the camera inward if zoomed way out, so the user
    // actually gets a close look at the focused point.
    const controls = controlsRef.current
    if (controls) {
      const dist = camera.position.distanceTo(controls.target)
      startDistance.current = dist
    }
  }, [focusTick, selectedId, points, camera, controlsRef])

  useFrame(() => {
    const controls = controlsRef.current
    const goal = goalRef.current
    if (!controls || !goal) return
    const now = performance.now()
    if (now > animUntil.current) {
      // Final snap — keep target at goal so OrbitControls maintains it
      controls.target.copy(goal)
      controls.update()
      goalRef.current = null
      return
    }
    // Ease toward goal — lerp per frame at a rate that arrives in ~1s
    controls.target.lerp(goal, 0.08)
    // Also pull camera inward if it's very far away (only on initial focus)
    if (startDistance.current > 45) {
      const camToTarget = new THREE.Vector3().subVectors(camera.position, controls.target)
      const targetDist = 28 + camToTarget.length() * 0.92
      camToTarget.setLength(Math.max(12, Math.min(targetDist, camToTarget.length() * 0.96)))
      camera.position.copy(controls.target).add(camToTarget)
    }
    controls.update()
  })

  return null
}
