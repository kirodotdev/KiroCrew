/**
 * Isolated capture entry for the Clean up sessions panel's inactivity picker.
 *
 * Renders the REAL `ChatSidebar` in the flat list lane over seeded rows of
 * varying age; the harness opens the header ⋮ menu and picks "Clean up
 * sessions", so the panel photographed is the shipped one. Only the preview
 * read (`/api/chat/slots/cleanup` with `dry_run`) is answered locally, from the
 * seeded ages, so each window shows how many sessions it would archive.
 *
 * Query string:
 *   ?width=<px>         -> sidebar width (default 260, the sidebar's default)
 *   ?theme=dark|light   -> theme
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected, sseSlots } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const width = Number(params.get('width') || 260)
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
localStorage.setItem('mc-sidebar-lane', 'flat')
localStorage.setItem('mc-session-stale-collapse-ms', '0')
localStorage.setItem('mc-sidebar-width', String(width))

declare global { interface Window { __cleanupPreviews: number[] } }
window.__cleanupPreviews = []

const DAY = 86_400_000
const ago = (days: number) => new Date(Date.now() - days * DAY).toISOString()

interface Row {
  key: string
  title: string
  messages: number
  running: boolean
  agent: string
  last_ts: string
  last_message: string
  pinned?: boolean
}
const row = (over: Partial<Row> & Pick<Row, 'key' | 'title' | 'last_ts' | 'last_message'>): Row => ({
  messages: 6, running: false, agent: 'kirocrew', ...over,
})
const ROWS: Row[] = [
  row({ key: 'chat-active', title: 'TCM Game Day Design', last_ts: ago(0.1), last_message: 'Draft is ready for review.' }),
  row({ key: 'chat-2d', title: 'Pipeline triage', last_ts: ago(2), last_message: 'Two targets retried to green.' }),
  row({ key: 'chat-5d', title: 'Alarm tuning', last_ts: ago(5), last_message: 'Threshold raised to 1500.' }),
  row({ key: 'chat-10d', title: 'Rust PoC benchmark', last_ts: ago(10), last_message: 'Keep Java; numbers attached.' }),
  row({ key: 'chat-20d', title: 'ORR pre-read', last_ts: ago(20), last_message: 'v3 shared with reviewers.' }),
  row({ key: 'chat-45d', title: 'Pentest teardown', last_ts: ago(45), last_message: 'All fixtures removed.' }),
  row({ key: 'chat-pinned', title: 'Status report template', pinned: true, last_ts: ago(60), last_message: 'Template frozen.' }),
]
const ACTIVE = 'chat-active'

// Gateway-free: answer the dry-run preview from the seeded ages (pinned kept,
// the active slot skipped). Every other read falls through to the dev server,
// which the sidebar already tolerates (its other capture pages stub nothing).
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/chat/slots/cleanup')) {
    const body = JSON.parse(String(init?.body || '{}')) as { max_inactive_days?: number; active_slot?: string }
    const maxDays = body.max_inactive_days ?? 3
    window.__cleanupPreviews.push(maxDays)
    const keys = ROWS
      .filter(s => !s.pinned && s.key !== body.active_slot && Date.now() - Date.parse(s.last_ts) > maxDays * DAY)
      .map(s => s.key)
    return Promise.resolve(new Response(JSON.stringify({ ok: true, archived: 0, keys, failed: [], dry_run: true, active_is_stale: false }), { status: 200, headers: { 'content-type': 'application/json' } }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const slots = ROWS as unknown as ChatSlot[]

function Harness() {
  useEffect(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots(slots))
  }, [])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={slots}
        activeSlot={ACTIVE}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent="kirocrew"
        installedAgents={[{ name: 'kirocrew', source: 'builtin' }]}
      />
    </div>
  )
}

initI18n()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
