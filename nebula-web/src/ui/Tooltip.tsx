import { useEffect, useState } from 'react'
import { apiFetch } from '../api'
import { useNebulaStore } from '../store'

type FactDetail = {
  id: string
  fact: string
  project: string
  tier: string
  anchor_type: string | null
  anchor_note: string | null
  cluster_id: number | null
  cluster_label: string[]
  personality_views: { personality: string; last_accessed: string | null; access_count: number }[]
  related: { id: string; preview: string }[]
  confidence: number
  access_count: number
  created_at?: string | null
  last_accessed?: string | null
  source?: string | null
  type?: 'fact' | 'mistake'
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return iso.slice(0, 19)
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mm = String(d.getMinutes()).padStart(2, '0')
    return `${y}-${m}-${day} ${hh}:${mm}`
  } catch {
    return iso.slice(0, 19)
  }
}

function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return ''
  try {
    const then = new Date(iso).getTime()
    if (isNaN(then)) return ''
    const diffSec = Math.max(0, (Date.now() - then) / 1000)
    if (diffSec < 60)      return 'just now'
    if (diffSec < 3600)    return `${Math.floor(diffSec / 60)}m ago`
    if (diffSec < 86400)   return `${Math.floor(diffSec / 3600)}h ago`
    if (diffSec < 86400 * 30) return `${Math.floor(diffSec / 86400)}d ago`
    if (diffSec < 86400 * 365) return `${Math.floor(diffSec / (86400 * 30))}mo ago`
    return `${Math.floor(diffSec / (86400 * 365))}y ago`
  } catch {
    return ''
  }
}

export function Tooltip() {
  const hoveredId = useNebulaStore((s) => s.hoveredId)
  const selectedId = useNebulaStore((s) => s.selectedId)
  const setSelected = useNebulaStore((s) => s.setSelected)
  const points = useNebulaStore((s) => s.points)
  const [detail, setDetail] = useState<FactDetail | null>(null)

  // Hover takes priority — moving the mouse onto a different point
  // shows that one. When no hover, fall back to the click-pinned
  // selection so EventLog clicks (and any other click-to-pin source)
  // surface the same data the hover panel would.
  const displayId = hoveredId || selectedId
  // Click-pinned (no live hover) — render an extra hint that Esc clears it
  const isPinned = !hoveredId && !!selectedId

  useEffect(() => {
    if (!displayId) {
      setDetail(null)
      return
    }
    let cancelled = false
    apiFetch(`/nebula/fact/${displayId}`)
      .then((r) => r.json())
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [displayId])

  // Escape clears a pinned selection so the tooltip can be dismissed
  // without clicking on something else.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isPinned) setSelected(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isPinned, setSelected])

  if (!displayId) return null
  const point = points.find((p) => p.id === displayId)
  const label =
    detail?.anchor_type
      ? `Anchor [${detail.anchor_type}]`
      : detail?.cluster_label?.length
      ? `Cluster: ${detail.cluster_label.join(' · ')}`
      : `Fact (${detail?.tier ?? point?.tier ?? 'contextual'})`

  return (
    <div
      style={{
        position: 'absolute',
        bottom: 24,
        left: 24,
        maxWidth: 460,
        background: 'rgba(10, 10, 16, 0.92)',
        border: '1px solid rgba(232, 232, 240, 0.25)',
        borderRadius: 8,
        padding: '14px 18px',
        color: '#e8e8f0',
        fontSize: 13,
        lineHeight: 1.45,
        fontFamily: 'Menlo, Monaco, "SF Mono", monospace',
        pointerEvents: 'none',
        zIndex: 10,
      }}
    >
      <div style={{
        display: 'flex', alignItems: 'baseline', justifyContent: 'space-between',
        color: '#9aa0ae', fontSize: 11, marginBottom: 6, letterSpacing: 0.5,
        gap: 12,
      }}>
        <span>
          {label.toUpperCase()}
          {isPinned && (
            <span style={{
              color: '#7fe4ff', marginLeft: 6, fontSize: 9,
              letterSpacing: 0.5,
            }} title="Pinned by click — press Esc to dismiss">
              · PINNED
            </span>
          )}
        </span>
        <span style={{ color: '#5a616f', fontSize: 10, letterSpacing: 0 }}>
          {displayId}
        </span>
      </div>
      <div style={{ marginBottom: 8, whiteSpace: 'pre-wrap' }}>
        {detail?.fact || point?.fact_preview || '…'}
      </div>
      {detail?.anchor_note ? (
        <div style={{ color: '#cfcfd8', fontStyle: 'italic', marginBottom: 6 }}>
          {detail.anchor_note}
        </div>
      ) : null}
      {/* Phase 5.6 — date + attribution metadata. Surfaces what Null
          already knows (created_at, last_accessed, access_count, source)
          so hovering is a real inspection surface, not a preview. */}
      <div style={{
        color: '#7a828f', fontSize: 10.5, marginBottom: 4,
        display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '1px 8px',
      }}>
        {detail?.created_at && (
          <>
            <span style={{ color: '#5a616f' }}>recorded</span>
            <span title={detail.created_at}>
              {fmtDate(detail.created_at)}
              <span style={{ color: '#5a616f' }}> · {fmtRelative(detail.created_at)}</span>
            </span>
          </>
        )}
        {detail?.last_accessed && (
          <>
            <span style={{ color: '#5a616f' }}>accessed</span>
            <span title={detail.last_accessed}>
              {fmtDate(detail.last_accessed)}
              <span style={{ color: '#5a616f' }}> · {fmtRelative(detail.last_accessed)}</span>
            </span>
          </>
        )}
        {detail?.source && (
          <>
            <span style={{ color: '#5a616f' }}>source</span>
            <span>{detail.source}</span>
          </>
        )}
        {detail?.project && detail.project !== 'global' && (
          <>
            <span style={{ color: '#5a616f' }}>project</span>
            <span>{detail.project}</span>
          </>
        )}
      </div>
      <div style={{ color: '#7a828f', fontSize: 11 }}>
        {detail?.personality_views?.length
          ? detail.personality_views.map((v) => v.personality).join(' · ')
          : point?.personalities?.join(' · ') || ''}
        {detail?.confidence !== undefined
          ? ` · confidence ${Math.round(detail.confidence * 100)}%`
          : ''}
        {detail?.access_count !== undefined && detail.access_count > 0
          ? ` · ${detail.access_count} accesses`
          : ''}
      </div>
    </div>
  )
}
