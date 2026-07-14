import { useNebulaStore } from '../store'

/** One-off overlay: warns when we detect the backend restarted while
 *  the tab was open. Prompts a reload so the user gets fresh bundle + state. */
export function Toast() {
  const show = useNebulaStore((s) => s.serverRestartedToast)
  const dismiss = useNebulaStore((s) => s.dismissRestartToast)
  if (!show) return null
  return (
    <div
      style={{
        position: 'absolute',
        top: 20,
        right: 20,
        background: 'rgba(20, 16, 24, 0.95)',
        border: '1px solid rgba(255, 180, 60, 0.6)',
        borderRadius: 8,
        padding: '14px 18px',
        color: '#ffd36b',
        fontFamily: 'Menlo, Monaco, monospace',
        fontSize: 12,
        lineHeight: 1.5,
        maxWidth: 320,
        zIndex: 20,
        boxShadow: '0 4px 24px rgba(0,0,0,0.5), 0 0 20px rgba(255,180,60,0.15)',
      }}
    >
      <div style={{ color: '#ffd36b', fontSize: 11, letterSpacing: 1, marginBottom: 6 }}>
        SERVER RESTARTED
      </div>
      <div style={{ color: '#cfcfd8', marginBottom: 10 }}>
        The Nebula backend restarted since this page loaded. Reload to sync state.
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <button
          onClick={() => window.location.reload()}
          style={{
            background: '#ffd36b',
            color: '#1a1a1f',
            border: 'none',
            borderRadius: 4,
            padding: '4px 12px',
            cursor: 'pointer',
            fontFamily: 'inherit',
            fontSize: 11,
            fontWeight: 600,
          }}
        >
          reload
        </button>
        <button
          onClick={dismiss}
          style={{
            background: 'transparent',
            color: '#7a828f',
            border: '1px solid #3a404b',
            borderRadius: 4,
            padding: '4px 12px',
            cursor: 'pointer',
            fontFamily: 'inherit',
            fontSize: 11,
          }}
        >
          dismiss
        </button>
      </div>
    </div>
  )
}
