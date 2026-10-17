/**
 * Isolated capture entry for the SYSTEM page's Sessions table over the same crew the
 * sidebar harness photographs: a lead, a conductor closed under it, three workers that
 * kept running. Mounted through the real `SessionsTab`.
 *
 * WHY ISOLATED: the table samples `/api/sessions/memory` every five seconds off a live
 * gateway's /proc pass, so the figures move between frames and the crew cannot be held
 * in this shape. What stays faithful is the wire: the payload below is the one that
 * route returns, and nothing downstream is stubbed -- `buildTree` places the rows,
 * `creatorOf` picks the citation, and TanStack draws the tree.
 *
 * `parent` on each worker is what `lineage_parents` computes server-side. The conductor
 * is absent from the payload because it is closed; what the query parameter switches is
 * whether the server walked up to the lead. That the walk produces this answer is
 * pinned in `test/test_slot_payload_lineage.py`.
 *
 * Query string: ?theme=dark|light
 *               &reparent=1 -- each worker resolves to the lead with `ancestor`, so the
 *                              table nests all three under it and the citation on each
 *                              still names the closed conductor.
 *                              Without it each worker carries a null key, which is what
 *                              the table received before: three top-level rows, each
 *                              saying it was created by a session that is not running.
 *
 * Usage: node scripts/capture-sessions-table-closed-conductor.mjs [devBase] [outDir]
 */
import { useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import SessionsTab from '../src/pages/system/SessionsTab'
import type { PlaneState } from '../src/pages/SystemPage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const LEAD = 'chat-2548'
const CONDUCTOR = 'chat-2552'
const reparented = params.get('reparent') === '1'

/** One row of `/api/sessions/memory`, with the figures a quiet runtime reports. */
const row = (
  slot: string,
  title: string,
  agent: string,
  mb: number,
  over: Record<string, unknown> = {},
) => ({
  key: `dashboard:${slot}`,
  title,
  slot_key: slot,
  untitled: false,
  agent,
  channel: 'dashboard',
  pid: 4000 + Number(slot.slice(-2)),
  owns_runtime: true,
  prompts: 6,
  rss_mb: mb,
  peak_rss_mb: mb + 40,
  procs: 2,
  mcp: 1,
  cpu_cores: 0.2,
  uptime_s: 2400,
  credits: 9.1,
  turns: 5,
  parent: null,
  ...over,
})

/** The citation every worker carries: the conductor opened it and has closed. */
const cite = reparented
  ? { slot: CONDUCTOR, key: `dashboard:${LEAD}`, ancestor: true }
  : { slot: CONDUCTOR, key: null }

const PAYLOAD = {
  sessions: [
    row(LEAD, 'Remote crew lane: drive the three PRs to green', 'kirocrew-lead', 780),
    row('chat-2554', 'I1: MicroVM boot path and the lease', 'kirocrew-worker', 430, { parent: cite }),
    row('chat-2555', 'I2: Fargate task definition', 'kirocrew-worker', 388, { parent: cite }),
    row('chat-2556', 'I3: token handoff to the remote crew', 'kirocrew-worker', 402, { parent: cite }),
    // Nobody opened this one: a top-level row with no citation in either frame.
    row('chat-2531', 'Crew log retention sweep', 'kirocrew', 240),
  ],
  tasks: [],
  totals: {
    rss_mb: 2240, runtimes: 5, host_mb: 64_000, host_pct: 3.5,
    rss_is_upper_bound: false, lineage_over_cap: false, lineage_cap: 4096,
  },
  history: [{ t: 1, mb: 2100 }, { t: 2, mb: 2240 }],
}

const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/sessions/memory')) {
    return Promise.resolve(new Response(JSON.stringify(PAYLOAD), {
      status: 200, headers: { 'content-type': 'application/json' },
    }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

function Harness() {
  const planeStateRef = useRef<PlaneState>({})
  return (
    <div className="min-h-screen bg-bg p-4" data-capture-ready="">
      <SessionsTab planeStateRef={planeStateRef} />
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
