/**
 * Capture page: the Crew Mode Workers panel's search box over a long crew.
 *
 * The list is the real component; only the worker list read is a fixture, with
 * English and Chinese titles and most workers finished (folded under Done).
 * Type in the box to filter. `?theme=light` for the light palette,
 * `?lang=zh-CN` for Chinese.
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
localStorage.setItem(CREW_FINISHED_OPEN_KEY, '0')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const W = (key: string, title: string, status: string, group: string, min: number, summary = '') =>
  ({ key, title, agent: 'kirocrew-worker', status, group, last_activity: ago(min), ledger_item: '', created_by: 'root', summary })

const WORKERS = [
  W('w1', 'Search past workers', 'working', 'working', 1, 'Writing the panel tests'),
  W('w2', 'Dispatch hint', 'needs_approval', 'approval', 3),
  W('w3', 'Status strip', 'idle', 'waiting', 9),
  W('d1', 'Fold finished workers', 'idle', 'done', 40, 'Merged'),
  W('d2', '修复登录页面', 'closed', 'done', 60, '已合并'),
  W('d3', 'Crew tab by default', 'idle', 'done', 80, 'Merged'),
  W('d4', '团队面板搜索框', 'closed', 'done', 100, '已合并'),
  W('d5', 'Panel worker search spike', 'closed', 'done', 140, 'Closed'),
]
Object.assign(api, { crewWorkers: async () => ({ root: 'root', workers: WORKERS }) })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n(params.get('lang') === 'zh-CN' ? 'zh-CN' : 'en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div style={{ width: 360, height: 640 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
        <CrewWorkerList root="root" viewing="root" active />
      </div>
    </QueryClientProvider>
  </Provider>,
)
