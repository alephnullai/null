import { useNebulaStore, type FireKind } from '../store'

const KIND_COLORS: Record<FireKind, string> = {
  observe: '#7fe4ff',
  learn:   '#7fe4ff',
  recall:  '#4ba9ff',
  decide:  '#ffd36b',
  mistake: '#ff3a5c',
  anchor:  '#ffd36b',
  drift:   '#ff8844',
  outreach: '#ffa54a',
  consolidate: '#8affc1',
  strengthen:  '#6aa3ff',
  demote:      '#7a828f',
  pontificate: '#c9a8ff',
}

const KIND_LABELS: Record<FireKind, string> = {
  observe: 'OBS',
  learn:   'LEARN',
  recall:  'RECALL',
  decide:  'DECIDE',
  mistake: 'MISTAKE',
  anchor:  'ANCHOR',
  drift:   'DRIFT',
  outreach: 'OUTREACH',
  consolidate: 'MERGE',
  strengthen:  'BOND',
  demote:      'FADE',
  pontificate: 'THINK',
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    const h = d.getHours().toString().padStart(2, '0')
    const m = d.getMinutes().toString().padStart(2, '0')
    const s = d.getSeconds().toString().padStart(2, '0')
    return `${h}:${m}:${s}`
  } catch {
    return ''
  }
}

export function EventLog() {
  const events = useNebulaStore((s) => s.eventLog).slice(0, 10)
  const selectedId = useNebulaStore((s) => s.selectedId)
  const requestFocus = useNebulaStore((s) => s.requestFocus)
  const points = useNebulaStore((s) => s.points)
  const pointIds = new Set(points.map((p) => p.id))

  // Phase 5.3b — panel is always rendered, even when empty. Previously
  // it vanished on refresh and only reappeared on new activity, which
  // made the app look broken. Empty-state gets a quiet "waiting" line.

  return (
    <div
      style={{
        position: 'absolute',
        right: 20,
        bottom: 20,
        width: 300,
        maxHeight: 300,
        background: 'rgba(8, 8, 14, 0.88)',
        border: '1px solid rgba(232, 232, 240, 0.12)',
        borderRadius: 6,
        padding: '10px 12px',
        color: '#cfcfd8',
        fontFamily: 'Menlo, Monaco, "SF Mono", monospace',
        fontSize: 11,
        lineHeight: 1.45,
        overflow: 'hidden',
        zIndex: 10,
        pointerEvents: 'auto',
      }}
    >
      <div style={{ color: '#7a828f', fontSize: 10, letterSpacing: 1, marginBottom: 6 }}>
        RECENT EVENTS  <span style={{ color: '#5a616f', fontSize: 9 }}>(click to focus)</span>
      </div>
      {events.length === 0 && (
        <div style={{ color: '#5a616f', fontSize: 11, fontStyle: 'italic', padding: '4px 6px' }}>
          waiting for activity…
        </div>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {events.map((ev, i) => {
          const color = KIND_COLORS[ev.kind] || '#7a828f'
          const label = KIND_LABELS[ev.kind] || ev.kind
          // Phase 5.3 — mistakes are now real points in the galaxy.
          // A mistake id (m_<row>) in pointIds means it's clickable.
          const isMistakeRecord = !!ev.fact_id?.startsWith('m_')
          const inCloud = !!ev.fact_id && pointIds.has(ev.fact_id)
          const hasPoint = inCloud
          const preview = ev.fact_id
            ? (isMistakeRecord
                ? `mistake ${ev.fact_id.slice(2, 10)}`
                : ev.fact_id.slice(0, 10))
            : '—'
          const rel = ev.related_ids?.length
            ? `  +${ev.related_ids.length}`
            : ''
          const isSelected = ev.fact_id === selectedId
          // Explain why a row is non-clickable (for tooltip + accessibility)
          const disabledReason = !ev.fact_id
            ? 'event has no fact id'
            : !inCloud
            ? 'fact is not in the current snapshot (may be newly archived or un-projected)'
            : ''
          return (
            <button
              key={`${ev.startMs}-${i}`}
              disabled={!hasPoint}
              title={hasPoint
                ? `Click to focus camera on ${ev.fact_id}`
                : disabledReason}
              onClick={() => hasPoint && ev.fact_id && requestFocus(ev.fact_id)}
              style={{
                display: 'grid',
                gridTemplateColumns: '54px 62px 1fr',
                gap: 6,
                alignItems: 'baseline',
                padding: '3px 6px',
                border: 'none',
                borderRadius: 4,
                background: isSelected ? 'rgba(127, 228, 255, 0.12)' : 'transparent',
                color: 'inherit',
                fontFamily: 'inherit',
                fontSize: 'inherit',
                lineHeight: 'inherit',
                textAlign: 'left',
                cursor: hasPoint ? 'pointer' : 'default',
                opacity: hasPoint ? (1 - (i / events.length) * 0.35) : 0.4,
                transition: 'background 0.15s',
              }}
              onMouseEnter={(e) => {
                if (hasPoint && !isSelected) {
                  e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)'
                }
              }}
              onMouseLeave={(e) => {
                if (!isSelected) {
                  e.currentTarget.style.background = 'transparent'
                }
              }}
            >
              <span style={{ color: '#5a616f', fontSize: 10 }}>{formatTime(ev.at)}</span>
              <span style={{ color, fontWeight: 600 }}>{label}</span>
              <span
                style={{
                  color: '#9aa0ae',
                  fontSize: 10,
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {preview}{rel}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
