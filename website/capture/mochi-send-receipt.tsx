/**
 * Isolated capture entry for Mochi's send-receipt strip (chat-core P2).
 *
 * WHY ISOLATED: a refused or unconfirmed Mochi send needs a server that binds
 * the pet's slot (`POST /api/chat/slots`) and then either refuses the turn
 * (`POST /api/chat` -> 409 "slot agent mismatch") or never answers it -- none
 * exists in a capture run. This mounts the REAL ChatPanel over the REAL
 * `mochiApi` -> `panelBridge.sendMessage` -> chat-core `sendTurn` path, with
 * fetch stubbed so the bind succeeds and the send fails the way the backend
 * would. Nothing re-implements the strip or its strings.
 *
 * FAITHFUL TO THE SHIPPED WINDOW: the panel (src/apps/mochi/panel.html +
 * panel/main.tsx) carries no utility stylesheet -- `applyTheme()` injects only
 * the core theme's VARIABLE blocks, and the theme comes from the dashboard's
 * `mc-theme` / `mc-color-theme` localStorage keys. This entry does exactly
 * that (no `index.css` import, `?theme` written to those keys before
 * `applyTheme()`), so the frames show the strips as the pet's panel renders
 * them, not as the dashboard bundle would.
 *
 * What it documents: before this branch Mochi painted the user bubble BEFORE
 * the POST and never read the reply, so a refused send left a bubble the
 * server never took and no error anywhere. Now the bubble appears only after
 * the server accepts; a refused send restores the text under an ErrorNotice
 * with the server's reason, and a send with no receipt restores it under a
 * `role=status` "delivery not confirmed" line (not an error: the send MAY have
 * landed, so "try again" would invite a duplicate).
 *
 * Query: ?theme=dark|light  ?refuse=409|late
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { ChatPanel } from '../src/apps/mochi/src/renderer/ChatPanel'
import { applyTheme } from '../src/apps/mochi/src/shared/themes'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const refuse = params.get('refuse') || '409'

// The same inputs the shipped panel reads its theme from (themes.ts
// `computeDatasetTheme`): the dashboard's stored mode and colour theme.
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
applyTheme()

// The panel reads "online" from the bridge's dashboard socket, and hides the
// composer behind a "Kiro Crew disconnected" card while offline -- so the scene
// needs a socket that opens and then says nothing. Installed BEFORE the bridge
// module evaluates (the ChatPanel import above is hoisted, but `connect()` runs
// lazily on the panel's first subscription, after this script's top level).
class OpenSocket extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readonly url: string
  readyState = 1
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: Event) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  constructor(url: string) {
    super()
    this.url = url
    queueMicrotask(() => this.onopen?.(new Event('open')))
  }
  send(): void {}
  close(): void {}
}
window.WebSocket = OpenSocket as unknown as typeof WebSocket

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.origin)
  // The bind the bridge performs before the first turn: the slot exists and is
  // the pet's own, so the send goes ahead.
  if (path.pathname === '/api/chat/slots' && init?.method === 'POST') {
    return new Response(JSON.stringify({ name: 'mochi', agent: 'mochi', effective_agent: 'mochi' }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  if (path.pathname === '/api/chat' && init?.method === 'POST') {
    // The dashboard wire is a bare fetch under the transport's deadline signal,
    // so a hung request must fail the way a real fetch does when that signal
    // fires -- an AbortError -- for `sendTurn` to read it as `response-late`.
    if (refuse === 'late') {
      return new Promise<Response>((_, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
      })
    }
    return new Response(
      JSON.stringify({ error: 'slot agent mismatch', code: 'slot_agent' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    )
  }
  // Everything else the panel asks for on mount (config, stats, history) is
  // answered empty: the scene is the strip under the composer, not the pet.
  if (path.pathname.startsWith('/api/')) {
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  return realFetch(input, init)
}

initI18n('en')

/** 320 = BASE_PANEL_WIDTH (mochiApi.ts): the shipped chat column. The body is
 *  `var(--bg)` because applyTheme() paints it so; the card wrapper is what
 *  PanelApp draws around the panel. */
createRoot(document.getElementById('root')!).render(
  <div data-capture-root style={{ background: 'var(--bg)', color: 'var(--text)', width: 320, height: 420, boxSizing: 'border-box', display: 'flex', flexDirection: 'column', margin: '12px auto', border: '1px solid var(--border)', borderRadius: 12, overflow: 'hidden' }}>
    <ChatPanel />
  </div>,
)
