import { useMemo } from 'react'
import { useNebulaStore, type PointClass } from '../store'

const STATUS_COLORS: Record<string, string> = {
  connecting: '#ffb020',
  live: '#34d399',
  reconnecting: '#ffb020',
  down: '#ff3a5c',
}
const STATUS_LABELS: Record<string, string> = {
  connecting: 'connecting',
  live: 'live',
  reconnecting: 'reconnecting',
  down: 'disconnected',
}

export function HUD() {
  const meta = useNebulaStore((s) => s.meta)
  const points = useNebulaStore((s) => s.points)
  const visible = useNebulaStore((s) => s.visible)
  const toggleClass = useNebulaStore((s) => s.toggleClass)
  const setAllVisible = useNebulaStore((s) => s.setAllVisible)
  const connection = useNebulaStore((s) => s.connection)
  const restartToast = useNebulaStore((s) => s.serverRestartedToast)
  const dismissRestart = useNebulaStore((s) => s.dismissRestartToast)

  // Count how many points are visible under the current filter
  const visibleCount = useMemo(() => {
    return points.filter((p) => {
      if (p.type === 'mistake') return visible.mistake
      const isShared = p.personalities.length >= 2
      const byPersonality = p.personalities.some(
        (pp) => (visible as any)[pp] === true,
      )
      const bySharedFlag = isShared && visible.shared
      return byPersonality || bySharedFlag
    }).length
  }, [points, visible])

  if (!meta) return null

  // Shared legend swatch: show a mini spectrum to hint that blends encode
  // which personalities share a fact. Two-personality blends = pair of
  // halves; 3+ = silver/grey.
  // Core personalities always appear in the legend. User-defined
  // personalities (registered via ~/.null/personalities/<name>/identity.json
  // with a color field) are merged by the backend into meta.palette
  // and rendered here alongside the core ones.
  const CORE = ['atlas', 'cybil', 'mercury', 'logos']
  const items: { cls: PointClass; label: string; color: string; sub?: string }[] = [
    ...CORE
      .filter((k) => meta.palette[k])
      .map((k) => ({
        cls: k as PointClass,
        label: k.charAt(0).toUpperCase() + k.slice(1),
        color: meta.palette[k],
      })),
    ...Object.entries(meta.palette)
      .filter(([k]) => !CORE.includes(k))
      .map(([k, color]) => ({
        cls: k as PointClass,
        label: k.charAt(0).toUpperCase() + k.slice(1),
        color,
        sub: 'user personality',
      })),
    { cls: 'shared', label: 'Shared', color: meta.shared_color,
      sub: 'color blends by mix' },
    { cls: 'mistake', label: 'Mistakes', color: '#b32a3a',
      sub: 'red dots, never pruned' },
  ]

  const allOn = Object.values(visible).every(Boolean)
  const noneOn = Object.values(visible).every((v) => !v)

  return (
    <div
      style={{
        position: 'absolute',
        top: 20,
        left: 24,
        color: '#cfcfd8',
        fontFamily: 'Menlo, Monaco, "SF Mono", monospace',
        fontSize: 12,
        lineHeight: 1.6,
        zIndex: 10,
        pointerEvents: 'auto',
      }}
    >
      <div style={{ fontSize: 16, color: '#e8e8f0', letterSpacing: 2, marginBottom: 4, display: 'flex', alignItems: 'center', gap: 10 }}>
        NULL · NEBULA
        <span
          title={`websocket: ${STATUS_LABELS[connection]}`}
          style={{
            display: 'inline-block',
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: STATUS_COLORS[connection] || '#7a828f',
            boxShadow: `0 0 8px ${STATUS_COLORS[connection] || '#7a828f'}`,
          }}
        />
      </div>
      <div style={{ fontSize: 10, color: '#5a616f', marginBottom: 8, letterSpacing: 1 }}>
        v{meta.server_version ?? '?'} · {STATUS_LABELS[connection]}
      </div>
      <div style={{ color: '#7a828f' }}>
        {visibleCount === meta.total_points
          ? `${meta.total_points} points · ${meta.cluster_count} clusters · ${meta.anchor_count} anchors`
          : `${visibleCount} / ${meta.total_points} points visible`}
      </div>
      {visibleCount === 0 && (
        <div
          style={{
            color: '#ff7a5c',
            fontSize: 11,
            marginTop: 4,
            fontStyle: 'italic',
          }}
        >
          no points match filter — try enabling another personality or Shared
        </div>
      )}
      <div style={{ marginTop: 12 }}>
        {items.map(({ cls, label, color, sub }) => {
          const on = visible[cls]
          return (
            <button
              key={cls}
              onClick={() => toggleClass(cls)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                background: 'transparent',
                border: 'none',
                color: on ? '#e8e8f0' : '#555a65',
                cursor: 'pointer',
                padding: '3px 0',
                fontFamily: 'inherit',
                fontSize: 'inherit',
                lineHeight: 'inherit',
                textAlign: 'left',
                opacity: on ? 1 : 0.55,
                transition: 'opacity 0.18s, color 0.18s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.color = '#ffffff'
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.color = on ? '#e8e8f0' : '#555a65'
              }}
            >
              <span
                style={{
                  display: 'inline-block',
                  width: 10,
                  height: 10,
                  borderRadius: '50%',
                  background: on ? color : 'transparent',
                  border: on ? 'none' : `1px solid ${color}`,
                  boxShadow: on ? `0 0 8px ${color}` : 'none',
                  transition: 'all 0.18s',
                }}
              />
              <span>
                {label}
                {sub && (
                  <span style={{ color: '#5a616f', fontSize: 10, marginLeft: 6 }}>
                    ({sub})
                  </span>
                )}
              </span>
            </button>
          )
        })}
        {/* Quick all-on / all-off controls */}
        <div style={{ marginTop: 8, display: 'flex', gap: 10 }}>
          <button
            onClick={() => setAllVisible(true)}
            disabled={allOn}
            style={{
              background: 'transparent',
              border: '1px solid #3a404b',
              color: allOn ? '#3a404b' : '#7a828f',
              cursor: allOn ? 'default' : 'pointer',
              padding: '3px 10px',
              fontFamily: 'inherit',
              fontSize: 11,
              borderRadius: 3,
            }}
          >
            all
          </button>
          <button
            onClick={() => setAllVisible(false)}
            disabled={noneOn}
            style={{
              background: 'transparent',
              border: '1px solid #3a404b',
              color: noneOn ? '#3a404b' : '#7a828f',
              cursor: noneOn ? 'default' : 'pointer',
              padding: '3px 10px',
              fontFamily: 'inherit',
              fontSize: 11,
              borderRadius: 3,
            }}
          >
            none
          </button>
        </div>
      </div>
      <div style={{ marginTop: 12, color: '#5a616f', fontSize: 11, pointerEvents: 'none' }}>
        click legend to filter · drag to rotate · scroll to zoom
      </div>
    </div>
  )
}
