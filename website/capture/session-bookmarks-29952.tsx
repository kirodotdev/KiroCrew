/**
 * Isolated capture entry for #29952's session bookmarks.
 *
 * Mounts the REAL `ActivityViewer` on its Links view, with two transcript links,
 * so the Bookmarks section renders where the side panel shows it. Only the
 * `/api/chat/bookmarks` routes are answered here, from an in-memory list that
 * implements the same list / create / delete contract as the gateway.
 *
 * Query string: ?theme=dark|light, &seed=empty|populated
 */
import { createRoot } from 'react-dom/client'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import ActivityViewer from '../src/pages/chat/ActivityViewer'
import chatReducer from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import type { ChatBookmark } from '../src/api/bookmarks'
import '../src/index.css'

const SLOT = 'dashboard:chat-7-1700000000'
const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const bookmarks: ChatBookmark[] = params.get('seed') === 'populated'
  ? [
      { id: 'b1', slot_key: SLOT, url: 'https://github.com/kirodotdev/KiroCrew/pull/29985', title: 'Bookmarks PR', origin: 'agent', created_at: '2026-11-04T21:40:00Z' },
      { id: 'b2', slot_key: SLOT, url: 'https://github.com/kirodotdev/KiroCrew/issues/29952', title: 'Per-session bookmarks issue', origin: 'user', created_at: '2026-11-04T21:41:00Z' },
      { id: 'b3', slot_key: SLOT, url: 'https://example.com/design/bookmarks', title: '', origin: 'user', created_at: '2026-11-04T21:42:00Z' },
    ]
  : []

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()
  if (url.includes('/api/chat/bookmarks')) {
    if (method === 'GET') return json({ bookmarks })
    if (method === 'POST') {
      const body = JSON.parse(String(init?.body || '{}')) as { url: string; title?: string }
      const row: ChatBookmark = { id: `b${bookmarks.length + 10}`, slot_key: SLOT, url: body.url, title: body.title || '', origin: 'user', created_at: new Date().toISOString() }
      bookmarks.push(row)
      return json(row, 201)
    }
    if (method === 'DELETE') {
      const id = decodeURIComponent(url.split('/').pop() || '')
      const i = bookmarks.findIndex(b => b.id === id)
      if (i >= 0) bookmarks.splice(i, 1)
      return json({ ok: true })
    }
  }
  if (url.includes('/api/')) return json({})
  return realFetch(input, init)
}) as typeof globalThis.fetch

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
})

await initI18n()

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const navLinks = [
  { url: 'https://docs.github.com/en/pull-requests', type: 'other' as const, label: 'About pull requests', msgIdx: 0 },
  { url: 'https://playwright.dev/docs/screenshots', type: 'other' as const, label: 'Playwright screenshots', msgIdx: 1 },
]

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div data-capture-root style={{ display: 'flex', height: '100vh', background: 'var(--bg)' }}>
          <div data-capture-panel style={{ width: 400, display: 'flex', flexDirection: 'column', borderLeft: '1px solid var(--border)' }}>
            <ActivityViewer subagents={{}} toolLog={[]} open onToggle={() => {}} slot={SLOT} view="links" navLinks={navLinks} />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
