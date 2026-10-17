/**
 * Isolated capture entry for the Filter menu's HIDE control on Unread, In progress
 * and Pinned. Renders the REAL `ChatSidebar` in the flat list lane with a mix of
 * running, idle, unread and pinned rows, so every combination can be tried by hand.
 *
 * Query string: ?theme=dark|light
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected } from '../src/store/dashboardSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
localStorage.setItem('mc-theme', theme === 'light' ? 'light' : 'dark')
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
localStorage.setItem('mc-sidebar-lane', 'flat')
localStorage.setItem('mc-session-stale-collapse-ms', '0')
localStorage.setItem('mc-sidebar-width', '380')

const MIN = 60_000
const now = Date.now()
const at = (msAgo: number) => new Date(now - msAgo).toISOString()

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
  messages: 12, running: false, agent: 'kirocrew', ...over,
})

const ROWS: Row[] = [
  row({ key: 'c-build', title: 'Build the release bundle', running: true, last_ts: at(20_000), last_message: 'Running the test suite...' }),
  row({ key: 'c-review', title: 'Review CR feedback', running: true, pinned: true, last_ts: at(45_000), last_message: 'Reading comments.' }),
  row({ key: 'c-deploy', title: 'Check the beta deploy', running: true, last_ts: at(2 * MIN), last_message: 'Waiting on the pipeline.' }),
  row({ key: 'c-oncall', title: 'On-call ticket triage', last_ts: at(5 * MIN), last_message: 'Done. Two tickets need you.' }),
  row({ key: 'c-design', title: 'Design doc questions', pinned: true, last_ts: at(12 * MIN), last_message: 'Which option do you want?' }),
  row({ key: 'c-worklog', title: 'Daily worklog', last_ts: at(40 * MIN), last_message: 'Worklog posted.' }),
  row({ key: 'c-slack', title: 'Slack search: filters', last_ts: at(90 * MIN), last_message: 'Found one related thread.' }),
]

const UNREAD = ['c-build', 'c-oncall', 'c-design']

function Harness() {
  useEffect(() => { store.dispatch(sseConnected()) }, [])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready="">
      <ChatSidebar
        slots={ROWS as unknown as ChatSlot[]}
        activeSlot={null}
        unreadSlots={UNREAD}
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
