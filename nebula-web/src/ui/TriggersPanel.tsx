import { useEffect, useState } from 'react'
import { useNebulaStore, type TriggerEntry, type OutreachEntry } from '../store'

const STATE_COLORS: Record<TriggerEntry['state'], string> = {
  ready:    '#34d399',
  cooling:  '#ffb020',
  disabled: '#5a616f',
}

function fmtRel(iso: string | null | undefined): string {
  if (!iso) return ''
  try {
    const then = new Date(iso).getTime()
    const diffSec = Math.max(0, (Date.now() - then) / 1000)
    if (diffSec < 60)        return 'just now'
    if (diffSec < 3600)      return `${Math.floor(diffSec / 60)}m ago`
    if (diffSec < 86400)     return `${Math.floor(diffSec / 3600)}h ago`
    if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`
    return new Date(iso).toLocaleDateString()
  } catch { return '' }
}

function fmtCountdown(iso: string | null): string {
  if (!iso) return ''
  try {
    const target = new Date(iso).getTime()
    const diffSec = (target - Date.now()) / 1000
    if (diffSec <= 0) return ''
    if (diffSec < 3600)  return `${Math.ceil(diffSec / 60)}m`
    if (diffSec < 86400) return `${Math.ceil(diffSec / 3600)}h`
    return `${Math.ceil(diffSec / 86400)}d`
  } catch { return '' }
}

/**
 * Phase 7.3 — outreach trigger management + recent fires.
 *
 * Top-right collapsible panel. Mirrors the HUD's visual language
 * (transparent dark background, mono font, narrow column) but lives in
 * the opposite corner so it doesn't fight the personality legend.
 */
export function TriggersPanel() {
  const triggers = useNebulaStore((s) => s.triggers)
  const outreaches = useNebulaStore((s) => s.outreaches)
  const setTriggerEnabled = useNebulaStore((s) => s.setTriggerEnabled)
  const acknowledgeOutreach = useNebulaStore((s) => s.acknowledgeOutreach)
  const loadTriggersAndOutreaches = useNebulaStore(
    (s) => s.loadTriggersAndOutreaches,
  )
  const [expanded, setExpanded] = useState(true)
  const [expandedOutreach, setExpandedOutreach] = useState<number | null>(null)

  // Light periodic refresh so cooldown countdowns don't go stale and
  // newly-fired outreaches appear without a manual reload.
  useEffect(() => {
    const id = window.setInterval(() => {
      void loadTriggersAndOutreaches()
    }, 30_000)
    return () => window.clearInterval(id)
  }, [loadTriggersAndOutreaches])

  if (!triggers.length && !outreaches.length) return null

  const unackCount = outreaches.filter((o) => !o.acknowledged_at).length

  return (
    <div
      style={{
        position: 'absolute',
        top: 20,
        right: 24,
        width: 320,
        maxHeight: 'calc(100vh - 60px)',
        overflowY: 'auto',
        background: 'rgba(8,8,14,0.88)',
        border: '1px solid rgba(232,232,240,0.12)',
        borderRadius: 6,
        padding: '10px 12px',
        color: '#cfcfd8',
        fontFamily: 'Menlo, Monaco, "SF Mono", monospace',
        fontSize: 11,
        lineHeight: 1.45,
        zIndex: 10,
        pointerEvents: 'auto',
      }}
    >
      <div
        onClick={() => setExpanded((e) => !e)}
        style={{
          color: '#7a828f',
          fontSize: 10,
          letterSpacing: 1,
          marginBottom: 6,
          cursor: 'pointer',
          display: 'flex',
          justifyContent: 'space-between',
        }}
      >
        <span>OUTREACH · {triggers.length} TRIGGERS{unackCount > 0 ? ` · ${unackCount} UNREAD` : ''}</span>
        <span>{expanded ? '▾' : '▸'}</span>
      </div>

      {expanded && (
        <>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {triggers.map((t) => (
              <TriggerRow
                key={t.id}
                trigger={t}
                onToggle={(en) => void setTriggerEnabled(t.id, en)}
              />
            ))}
            {triggers.length === 0 && (
              <div style={{ color: '#5a616f', fontStyle: 'italic' }}>
                No triggers seeded yet — run: null outreach seed
              </div>
            )}
          </div>

          <div style={{ marginTop: 12, color: '#7a828f', fontSize: 10, letterSpacing: 1 }}>
            RECENT FIRES
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 4 }}>
            {outreaches.slice(0, 8).map((o) => (
              <OutreachRow
                key={o.id}
                outreach={o}
                expanded={expandedOutreach === o.id}
                onToggle={() => setExpandedOutreach(
                  expandedOutreach === o.id ? null : o.id,
                )}
                onAck={() => void acknowledgeOutreach(o.id)}
              />
            ))}
            {outreaches.length === 0 && (
              <div style={{ color: '#5a616f', fontStyle: 'italic' }}>
                Nothing fired yet.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function TriggerRow({
  trigger,
  onToggle,
}: {
  trigger: TriggerEntry
  onToggle: (enabled: boolean) => void
}) {
  const dot = STATE_COLORS[trigger.state]
  const cool = trigger.state === 'cooling'
    ? `cools ${fmtCountdown(trigger.next_eligible_at)}`
    : ''
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '3px 0',
      }}
      title={trigger.name}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0, flex: 1 }}>
        <span
          style={{
            display: 'inline-block',
            width: 8, height: 8, borderRadius: '50%',
            background: dot, boxShadow: `0 0 6px ${dot}`,
            flexShrink: 0,
          }}
        />
        <span style={{
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          color: trigger.enabled ? '#e8e8f0' : '#7a828f',
        }}>
          {trigger.name}
        </span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10 }}>
        {cool && <span style={{ color: '#7a828f' }}>{cool}</span>}
        <button
          onClick={() => onToggle(!trigger.enabled)}
          style={{
            background: 'transparent',
            border: '1px solid #3a404b',
            color: trigger.enabled ? '#34d399' : '#7a828f',
            padding: '1px 8px',
            borderRadius: 3,
            cursor: 'pointer',
            fontFamily: 'inherit',
            fontSize: 10,
          }}
        >
          {trigger.enabled ? 'on' : 'off'}
        </button>
      </div>
    </div>
  )
}

function OutreachRow({
  outreach,
  expanded,
  onToggle,
  onAck,
}: {
  outreach: OutreachEntry
  expanded: boolean
  onToggle: () => void
  onAck: () => void
}) {
  const acked = !!outreach.acknowledged_at
  return (
    <div style={{
      borderLeft: acked ? '2px solid #3a404b' : '2px solid #ffa54a',
      paddingLeft: 6,
      opacity: acked ? 0.5 : 1,
    }}>
      <div
        onClick={onToggle}
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          gap: 8,
          cursor: 'pointer',
        }}
      >
        <span style={{
          flex: 1,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: expanded ? 'normal' : 'nowrap',
        }}>
          {outreach.subject || outreach.body.slice(0, 60)}
        </span>
        <span style={{ color: '#5a616f', fontSize: 10, flexShrink: 0 }}>
          {fmtRel(outreach.sent_at)}
        </span>
      </div>
      {expanded && (
        <div style={{
          marginTop: 4, padding: '4px 6px',
          background: 'rgba(255,255,255,0.03)', borderRadius: 3,
          whiteSpace: 'pre-wrap', color: '#9aa0ae', fontSize: 10.5,
        }}>
          {outreach.body}
          {!acked && (
            <button
              onClick={(e) => { e.stopPropagation(); onAck() }}
              style={{
                display: 'block', marginTop: 6,
                background: 'transparent', border: '1px solid #3a404b',
                color: '#7fe4ff', padding: '2px 10px', borderRadius: 3,
                cursor: 'pointer', fontSize: 10, fontFamily: 'inherit',
              }}
            >
              acknowledge
            </button>
          )}
          {acked && (
            <div style={{ color: '#5a616f', marginTop: 4 }}>
              acknowledged {fmtRel(outreach.acknowledged_at)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
