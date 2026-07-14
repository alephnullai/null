// Per-launch auth token plumbing.
//
// The Nebula backend generates a token at every launch and prints the
// full URL (`/?token=...`) to the console. We read it once from the URL,
// persist it to sessionStorage so SPA navigation / refresh survives, and
// attach it to every API request + websocket connect.

const TOKEN_KEY = 'nebula_token'

function readToken(): string | null {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('token')
    if (fromUrl) {
      sessionStorage.setItem(TOKEN_KEY, fromUrl)
      return fromUrl
    }
    return sessionStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export const authToken: string | null = readToken()

/** fetch() wrapper that attaches `Authorization: Bearer <token>`. */
export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers)
  if (authToken) headers.set('Authorization', `Bearer ${authToken}`)
  return fetch(path, { ...init, headers })
}

/** Build a ws:// URL for `path`, appending `?token=` (ws has no headers). */
export function wsUrl(path: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const base = `${proto}://${window.location.host}${path}`
  return authToken ? `${base}?token=${encodeURIComponent(authToken)}` : base
}
