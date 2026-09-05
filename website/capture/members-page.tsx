/**
 * Isolated capture entry for the Crew Members page.
 *
 * Mounts the REAL MembersPage against the real stylesheet, theme tokens and
 * live i18n catalog. API responses come from the capture script's route
 * interception (gateway-free); this entry only seeds what the page reads
 * from the store — the live `slots` frames that drive the presence dots.
 *
 * Scenes via query string: ?theme=dark|light — the script drives the page
 * itself (clicking a REAL roster row opens the thread), so a frame documents
 * the shipped wiring, not forced component state.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import MembersPage from '../src/pages/members/MembersPage'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Live presence rides the WS `slots` frames; seed the same shape so the
// Radar dot renders "working" from the store, not from the roster snapshot.
// The worker rows carry `created_by: 'member-radar'` — the durable birth
// attribution the drawer's "Driving sessions" block filters on — in each of
// the four states its status dot distinguishes, plus a fifth-and-beyond to
// exercise the fold (DRIVING_VISIBLE = 5). `other` belongs to another
// member and must NOT appear under radar.
const now = Date.now()
const iso = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString()
store.dispatch(
  sseSlots([
    {
      key: 'member-radar',
      title: 'Radar',
      messages: 4,
      running: true,
      mode: 'member',
      agent: 'radar',
    },
    { key: 'chat-1-w1', title: 'Fix #4213 · sidebar drop misses folder', messages: 41, running: true, pending_approval: true, created_by: 'member-radar', created: iso(50), last_turn_ts: iso(1) },
    { key: 'chat-1-w2', title: 'Triage: seven fresh candidates', messages: 18, running: true, created_by: 'member-radar', created: iso(40), last_turn_ts: iso(3) },
    { key: 'chat-1-w3', title: 'Investigate #4187 disposition enforcement', messages: 9, running: false, needs_input: true, created_by: 'member-radar', created: iso(35), last_turn_ts: iso(12) },
    { key: 'chat-1-w4', title: 'Re-triage needs-investigation backlog', messages: 27, running: false, created_by: 'member-radar', created: iso(120), last_turn_ts: iso(45) },
    { key: 'chat-1-w5', title: 'Bundle size gate unblocker', messages: 6, running: false, created_by: 'member-radar', created: iso(180), last_turn_ts: iso(90) },
    { key: 'chat-1-w6', title: 'Weekly fix-loop analysis', messages: 12, running: false, created_by: 'member-radar', created: iso(600), last_turn_ts: iso(400) },
    { key: 'chat-1-w7', title: 'Cleanup: stale worktrees', messages: 3, running: false, created_by: 'member-radar', created: iso(900), last_turn_ts: iso(800) },
    { key: 'chat-1-other', title: 'Draft the release notes', messages: 5, running: true, created_by: 'member-scribe', created: iso(20), last_turn_ts: iso(2) },
  ] as never),
)

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="h-screen bg-bg text-text" data-capture-root>
            <MembersPage />
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
