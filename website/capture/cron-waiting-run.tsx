/**
 * Isolated capture entry for the Schedule page's "waiting to start" state.
 *
 * WHY ISOLATED: a job is in this state only between its scheduled minute and the
 * end of its random start delay, on a live gateway, so it cannot be reached on
 * demand. The page reads the state from `GET /api/crons` alone (`is_running`
 * with `waiting_until`), so stubbing that response renders the real page: real
 * classes, real Tailwind output, real theme tokens.
 *
 * Two scenes: `rows` is the job list with one job waiting to start, one
 * executing and one idle; `panel` is the job panel's banner in the waiting and
 * the executing state.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseConnected } from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import SchedulePage from '../src/pages/SchedulePage'
import JobLogsView from '../src/components/JobLogsView'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') === 'panel' ? 'panel' : 'rows'
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The jobs' scheduled minute, four minutes before the shot, and the end of the
// waiting job's start delay. The shooter pins the zone.
const DUE = Math.floor(Date.now() / 60000) * 60 - 4 * 60
const STARTS_AT = DUE + 27 * 60
const DUE_EXPR = `${new Date(DUE * 1000).getUTCMinutes()} ${new Date(DUE * 1000).getUTCHours()} * * *`
const NEXT_RUN = DUE + 86400

const job = (id: string, name: string, extra: Record<string, unknown>) => ({
  id,
  name,
  message: '',
  command: 'sleep 180',
  enabled: true,
  schedule: `cron ${DUE_EXPR}`,
  cron_expr: DUE_EXPR,
  created_ts: DUE - 3600,
  last_status: null,
  last_run_ts: null,
  next_run_ts: NEXT_RUN,
  has_slot: false,
  is_running: false,
  running_since: null,
  waiting_until: null,
  ...extra,
})

const JOBS = [
  job('wait0001', 'nightly-export', { is_running: true, running_since: DUE, waiting_until: STARTS_AT }),
  job('run00001', 'nightly-backup', { is_running: true, running_since: DUE }),
  job('idle0001', 'nightly-digest', { last_status: 'ok', last_run_ts: DUE - 86400 + 600 }),
]

// The list, the folders and the history are stubbed. Every other gateway read
// the page makes answers an empty object, since no gateway is behind this page.
const json = (body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }))
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const href = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(href, location.origin).pathname
  if (path === '/api/crons') return json({ jobs: JOBS, server_tz: 'UTC', default_tz: 'UTC', host_tz: 'UTC' })
  if (path === '/api/cron-folders') return json([])
  if (path.startsWith('/api/crons/') && path.includes('/history')) return json({ runs: [], total: 0 })
  if (path.startsWith('/api/')) return json({})
  return realFetch(input as RequestInfo, init)
}) as typeof globalThis.fetch

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
})
store.dispatch(sseConnected())

function Scene() {
  if (scene === 'panel') {
    return (
      <div data-testid="scene-panel" className="flex flex-col gap-4 bg-bg p-6 text-text" style={{ width: 640 }}>
        <JobLogsView jobId="wait0001" isRunning runningSince={DUE} waitingUntil={STARTS_AT} onCancel={() => {}} />
        <JobLogsView jobId="run00001" isRunning runningSince={DUE} waitingUntil={null} onCancel={() => {}} />
      </div>
    )
  }
  return <div data-testid="scene-rows" className="h-screen bg-bg text-text"><SchedulePage /></div>
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}><MemoryRouter><QueryClientProvider client={client}>
    <ThemeProvider><Scene /></ThemeProvider>
  </QueryClientProvider></MemoryRouter></Provider>,
)
