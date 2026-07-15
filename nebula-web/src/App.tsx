import { useEffect, useRef } from 'react'
import { Canvas } from '@react-three/fiber'
import { OrbitControls, Stars } from '@react-three/drei'
import { EffectComposer, Bloom } from '@react-three/postprocessing'

import { useNebulaStore } from './store'
import { Points } from './scene/Points'
import { Identity, ClusterLinks } from './scene/Identity'
import { Traces } from './scene/Traces'
import { Shockwaves } from './scene/Shockwaves'
import { CameraFocuser } from './scene/CameraFocuser'
import { Tooltip } from './ui/Tooltip'
import { HUD } from './ui/HUD'
import { Toast } from './ui/Toast'
import { EventLog } from './ui/EventLog'
import { TriggersPanel } from './ui/TriggersPanel'
import { useLiveEvents } from './hooks/useLiveEvents'

export default function App() {
  const loadAll = useNebulaStore((s) => s.loadAll)
  const loading = useNebulaStore((s) => s.loading)
  const points = useNebulaStore((s) => s.points)
  const controlsRef = useRef<any>(null)

  useEffect(() => {
    loadAll()
  }, [loadAll])

  // Pause browser audio/video-style when tab is hidden — no rendering
  // while you're not looking. Saves battery significantly when Nebula
  // is open in a background tab.
  useEffect(() => {
    const onVisChange = () => {
      // No-op; frameloop="demand" naturally stops when nothing invalidates.
      // When tab returns to focus, a render is forced.
      if (document.visibilityState === 'visible') {
        // Force one frame on return
        document.dispatchEvent(new Event('resize'))
      }
    }
    document.addEventListener('visibilitychange', onVisChange)
    return () => document.removeEventListener('visibilitychange', onVisChange)
  }, [])

  // Subscribe to live firing events via websocket
  useLiveEvents()

  return (
    <div style={{ width: '100vw', height: '100vh', position: 'relative', background: '#000' }}>
      <Canvas
        camera={{ position: [0, 0, 45], fov: 60 }}
        // Battery-friendly: render on demand. useFrame + OrbitControls
        // damping + event arrivals all call invalidate() to request frames.
        frameloop="demand"
        gl={{
          antialias: true,
          alpha: false,
          powerPreference: 'low-power',
        }}
        onCreated={({ raycaster }) => {
          if (raycaster.params.Points) raycaster.params.Points.threshold = 0.4
          if (raycaster.params.Line) raycaster.params.Line.threshold = 0
        }}
      >
        <color attach="background" args={['#000005']} />
        {/* Ambient only — points & identity are self-emissive now,
            lighting would wash out colors rather than reveal them. */}
        <ambientLight intensity={0.4} />
        <Stars radius={220} depth={80} count={4000} factor={4} saturation={0} fade speed={0.3} />

        <Points />
        <Traces />
        <Shockwaves />
        <ClusterLinks />
        <Identity />

        <OrbitControls
          ref={controlsRef}
          enableDamping
          dampingFactor={0.08}
          rotateSpeed={0.5}
          zoomSpeed={0.8}
          panSpeed={0.6}
          minDistance={3}
          maxDistance={200}
        />
        <CameraFocuser controlsRef={controlsRef} />

        {/* Bloom pass — the galaxy aesthetic in one component. */}
        <EffectComposer>
          <Bloom
            intensity={0.9}
            luminanceThreshold={0.15}
            luminanceSmoothing={0.6}
            mipmapBlur
          />
        </EffectComposer>
      </Canvas>

      <HUD />
      <Tooltip />
      <EventLog />
      <TriggersPanel />
      <Toast />

      {loading && (
        <div
          style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            color: '#7a828f',
            fontFamily: 'Menlo, monospace',
            fontSize: 14,
          }}
        >
          loading nebula…
        </div>
      )}
      {!loading && points.length === 0 && (
        <div
          style={{
            position: 'absolute',
            top: '50%',
            left: '50%',
            transform: 'translate(-50%, -50%)',
            color: '#ff3a5c',
            fontFamily: 'Menlo, monospace',
            fontSize: 14,
          }}
        >
          no points — is the backend running on 8787?
        </div>
      )}
    </div>
  )
}
