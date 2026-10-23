/**
 * Isolated capture for issue #17111: "sidebar: label the pinned band while a tag
 * filter or status chip narrows the list".
 *
 * Scenes:
 *   ?scene=no-filter          No chip active — no caption expected.
 *   ?scene=chip-only-pinned   "In progress" chip with only the pinned row remaining
 *                             (the two unpinned rows are not running). Caption visible.
 *   ?scene=chip-with-unpinned "In progress" chip with a running unpinned row matching.
 *                             Caption visible on the divider between pinned + unpinned.
 *   ?scene=tag-filter         Tag filter narrowing the list; pinned row lacks the tag.
 *                             Caption visible; pinned row stays.
 *   &theme=dark|light
 *
 * Sessions:
 *   "alpha session" — pinned, no tag, not running
 *   "beta session"  — not pinned, tag t1, running  (for chip-with-unpinned)
 *   "gamma session" — not pinned, tag t2, not running
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
const scene = params.get('scene') || 'no-filter'

localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
// Use flat view so there is no folder complexity, matching the test PINNED_SLOTS fixture.
localStorage.setItem('mc-sidebar-lane', 'flat')
localStorage.setItem('mc-session-stale-collapse-ms', '0')
localStorage.setItem('mc-sidebar-width', '380')

// Clear any leftover filters, then apply the scene's.
localStorage.removeItem('mc-session-running-only')
localStorage.removeItem('mc-session-tag-filter')

if (scene === 'chip-only-pinned' || scene === 'chip-with-unpinned') {
  localStorage.setItem('mc-session-running-only', '1')
}
if (scene === 'tag-filter') {
  // Tag t2 — gamma has it; alpha (pinned) does not.
  localStorage.setItem('mc-session-tag-filter', JSON.stringify(['t2']))
}

const MIN = 60_000
const now = Date.now()
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

const ROWS: ChatSlot[] = [
  {
    key: 'k-alpha', title: 'alpha session',
    running: false, messages: 2,
    tags: [], pinned: true,
    agent: 'kirocrew', last_ts: at(5 * MIN), last_message: 'Done.',
  },
  {
    key: 'k-beta', title: 'beta session',
    // running=true only for the chip-with-unpinned scene; snapshot state before render
    running: scene === 'chip-with-unpinned', messages: 2,
    tags: ['t1'],
    agent: 'kirocrew', last_ts: at(10 * MIN), last_message: 'Working...',
  },
  {
    key: 'k-gamma', title: 'gamma session',
    running: false, messages: 2,
    tags: ['t2'],
    agent: 'kirocrew', last_ts: at(20 * MIN), last_message: 'Done.',
  },
] as unknown as ChatSlot[]

function Harness() {
  useEffect(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots(ROWS))
  }, [])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={ROWS}
        activeSlot={null}
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
