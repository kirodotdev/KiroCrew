/**
 * Isolated capture entry for the pinned-session section's ordering.
 *
 * WHY ISOLATED: the delta is which SEQUENCE pinned rows render in, which needs a
 * fixed slot set, known activity instants and a specific localStorage state --
 * none of them reachable on demand in a live session. The REAL ChatSidebar, store
 * and stylesheet render here; only the slot feed and `/api/**` are supplied.
 *
 * Scenes (?scene=):
 *   stored-not-reordered  stored order present from membership bookkeeping, never
 *                         reordered -- rows follow the sort key (charlie first).
 *   reordered             same stored order plus the manual marker -- the user's
 *                         arrangement wins (alpha first).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Importing the module only DEFINES initI18n; without calling it every label is blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseSlots, sseConnected } from '../src/store/dashboardSlice'
import {
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
} from '../src/utils/pinnedSessionOrder'
import ChatSidebar from '../src/pages/ChatSidebar'
import { ThemeProvider } from '../src/hooks/useTheme'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'stored-not-reordered'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// No gateway behind this page: answer the sidebar's reads with the empty collections
// a fresh account returns. Replaces the backend, not the component.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/')) {
    return Promise.resolve(new Response('[]', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

// Titles ascend while activity descends, so the two candidate orders are exact
// reverses of each other and the frame cannot be read ambiguously.
const SLOTS = [
  { key: 'alpha', title: 'alpha — oldest activity', messages: 4, running: false, pinned: true, last_turn_ts: '2026-03-01T09:00:00Z' },
  { key: 'bravo', title: 'bravo — middle activity', messages: 7, running: false, pinned: true, last_turn_ts: '2026-03-02T09:00:00Z' },
  { key: 'charlie', title: 'charlie — newest activity', messages: 2, running: false, pinned: true, last_turn_ts: '2026-03-03T09:00:00Z' },
]

localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['alpha', 'bravo', 'charlie']))
if (scene === 'reordered') localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')
else localStorage.removeItem(PINNED_SESSION_ORDER_MANUAL_KEY)

store.dispatch(sseConnected())
store.dispatch(sseSlots(SLOTS as never))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
qc.setQueryData(['chat-folders'], [])

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <div className="bg-bg text-text" style={{ width: 340, height: 560 }}>
            <ChatSidebar
              slots={SLOTS as never} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
