/**
 * Isolated capture entry for the crew editor's "what wakes this crew" section.
 *
 * WHY ISOLATED: the section lives inside the crew editor Dialog, which needs the
 * whole Crews roster, a live gateway and a selected crew to reach. The section
 * itself only depends on `GET /api/crons`, so stubbing that one response renders
 * the real component — real classes, real Tailwind output, real theme tokens —
 * without standing up a gateway.
 *
 * Three scenes cover the states a reviewer needs to see: a crew with clock
 * triggers, a crew with a paused one, and a crew with none (the empty state that
 * teaches a first-time user the crew only runs when they chat to it).
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import CrewWakeSection from '../src/components/CrewWakeSection'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const now = Math.floor(Date.now() / 1000)
const JOBS = [
  {
    id: 'j1', name: 'gh-autofix-dispatcher', message: '', enabled: true, last_status: 'ok',
    schedule: 'every 15m', agent: 'kirocrew-autofix',
    last_run_ts: now - 240, next_run_ts: now + 660,
  },
  {
    id: 'j2', name: 'gh-autofix-cleanup', message: '', enabled: true, last_status: 'ok',
    schedule: 'every 15m', agent: 'kirocrew-autofix',
    last_run_ts: now - 540, next_run_ts: now + 360,
  },
  {
    id: 'j3', name: 'dispatch', message: '', enabled: false, last_status: '',
    schedule: 'every 2m', agent: 'ops-triage', next_run_ts: null,
  },
  {
    id: 'j4', name: 'rotation-check', message: '', enabled: true, last_status: 'ok',
    schedule: 'every 5m', agent: 'ops-triage',
    last_run_ts: now - 60, next_run_ts: now + 240,
  },
  {
    id: 'j5', name: 'start a day', message: '', enabled: true, last_status: 'ok',
    schedule: '0 9 * * 1-5 · Asia/Shanghai', agent: '',
    last_run_ts: now - 86400, next_run_ts: now + 3600,
  },
]

// Only /api/crons is stubbed; anything else falls through so a missed dependency
// shows up as a real network error rather than silently rendering empty.
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/crons')) {
    return Promise.resolve(new Response(JSON.stringify({ jobs: JOBS }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

function Frame({ label, crew, isDefaultCrew }: { label: string; crew: string; isDefaultCrew: boolean }) {
  return (
    <div className="rounded-xl border border-border-strong bg-card p-4" style={{ width: 560 }}>
      <div className="mb-3 font-mono text-[11px] text-muted-strong">{label}</div>
      <CrewWakeSection crew={crew} isDefaultCrew={isDefaultCrew} />
    </div>
  )
}

function Scenes() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div className="flex flex-col items-start gap-5 bg-bg p-6 text-text">
          <Frame label="kirocrew-autofix — two clock triggers" crew="kirocrew-autofix" isDefaultCrew={false} />
          <Frame label="ops-triage — one paused" crew="ops-triage" isDefaultCrew={false} />
          <Frame label="default — claims the agent-less cron" crew="default" isDefaultCrew />
          <Frame label="kirocrew-lite — nothing wakes it" crew="kirocrew-lite" isDefaultCrew={false} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scenes />)
