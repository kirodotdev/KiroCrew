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
 *   ?route=<path>   the router's initial entry (default /members), so a frame
 *                   can arrive by deep link (`/members?member=ghost`).
 *   ?nav=1          mount a one-line caption bar printing the live
 *                   pathname+search (the address bar is outside the capture),
 *                   with a Back button that pops the router's history, and a
 *                   stand-in `/elsewhere` route so a frame can show where one
 *                   Back lands after switching members on /members.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'

import MembersPage from '../src/pages/members/MembersPage'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const route = params.get('route') || '/members'
const nav = params.get('nav') === '1'
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

/** Prints the router's live URL and offers a history pop — the two things a
 *  still frame of a MemoryRouter cannot otherwise show. Capture chrome only. */
function CaptionBar() {
  const loc = useLocation()
  const go = useNavigate()
  return (
    <div
      data-capture-url
      className="shrink-0 flex items-center gap-3 px-3 h-8 border-t border-border bg-surface font-mono text-[12px] text-muted"
    >
      <button
        type="button"
        data-capture-back
        onClick={() => go(-1)}
        className="px-2 py-0.5 rounded border border-border text-text hover:bg-accent/40"
      >
        ← Back
      </button>
      <span data-capture-url-text>{loc.pathname + loc.search}</span>
    </div>
  )
}

/** Where one Back lands after leaving /members: a stand-in for any other page. */
function Elsewhere() {
  const go = useNavigate()
  return (
    <div className="h-full flex flex-col items-center justify-center gap-3 text-text" data-capture-elsewhere>
      <div className="text-lg">Another page</div>
      <button
        type="button"
        data-capture-go-members
        onClick={() => go('/members')}
        className="px-3 py-1 rounded border border-border hover:bg-accent/40"
      >
        Open Crew Members
      </button>
    </div>
  )
}

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>
          <div className="h-screen flex flex-col bg-bg text-text" data-capture-root>
            <div className="flex-1 min-h-0">
              {nav ? (
                <Routes>
                  <Route path="/elsewhere" element={<Elsewhere />} />
                  <Route path="*" element={<MembersPage />} />
                </Routes>
              ) : (
                <MembersPage />
              )}
            </div>
            {nav && <CaptionBar />}
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
