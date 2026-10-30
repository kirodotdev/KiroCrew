/**
 * Capture page: the Crew Mode Workers panel with its Done group folded.
 *
 * Finished workers sit under one Done toggle, closed by default, so the live
 * groups stay on top. `?open=1` starts it open; `?viewing=<key>` puts that
 * worker on screen (a folded Done still shows it, marked current).
 * `?theme=light` for the light palette. The list is the real component; only the
 * worker list read is a fixture.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import CrewWorkerList, { CREW_FINISHED_OPEN_KEY } from '../src/pages/chat/command-center/CrewWorkerList'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')
localStorage.setItem(CREW_FINISHED_OPEN_KEY, params.get('open') === '1' ? '1' : '0')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const W = (key: string, title: string, status: string, group: string, min: number, summary = '') =>
  ({ key, title, agent: 'kirocrew-worker', status, group, last_activity: ago(min), ledger_item: '', created_by: 'root', summary })

const WORKERS = [
  W('w1', 'Fold finished workers', 'working', 'working', 1, 'Writing the panel tests'),
  W('w2', 'Dispatch hint', 'needs_approval', 'approval', 3),
  W('w3', 'Status strip', 'idle', 'with_conductor', 6, 'Asked which counts to show'),
  W('w4', 'Back to conductor', 'idle', 'waiting', 9),
  ...Array.from({ length: 9 }, (_, i) => W(`d${i}`, `Finished task ${i + 1}`, i % 3 ? 'idle' : 'closed', 'done', 30 + i * 20, 'Merged')),
]
Object.assign(api, { crewWorkers: async () => ({ root: 'root', workers: WORKERS }) })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n(params.get('lang') === 'zh-CN' ? 'zh-CN' : 'en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div style={{ width: 360, height: 760 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
        <CrewWorkerList root="root" viewing={params.get('viewing') || 'root'} active />
      </div>
    </QueryClientProvider>
  </Provider>,
)
