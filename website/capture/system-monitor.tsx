/**
 * Isolated capture entry for the System Monitor page (/monitor).
 *
 * WHY ISOLATED: the page polls GET /api/system/chat-resources, which needs a
 * live gateway walking real /proc trees. The snapshot is stubbed with the exact
 * JSON shape the handler serializes; everything else is real: the REAL
 * SystemMonitorPage, the REAL stylesheet and theme tokens, the REAL confirm
 * dialog.
 *
 * Scenes (?scene=):
 *   busy         a loaded 8-core box: five chats, one dedicated subagent, one
 *                worker and the gateway itself, with the agents-slice gauge
 *                near its ceiling. The two heaviest rows carry the top-consumer
 *                highlight.
 *   unsupported  a platform without per-process sampling: the header strip
 *                still shows host headroom, the table is replaced by a notice.
 *   confirm      the Stop confirmation for the heaviest chat is open.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import SystemMonitorPage from '../src/pages/SystemMonitorPage'
import type { MonitorEntry, MonitorSnapshot } from '../src/pages/SystemMonitorPage'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'busy'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

function entry(over: Partial<MonitorEntry>): MonitorEntry {
  return {
    kind: 'chat',
    session_key: '',
    label: '',
    agent: 'kirocrew',
    pid: 0,
    proc_count: 1,
    rss_mb: null,
    cpu_pct: null,
    uptime_s: null,
    slot: '',
    subagent_id: '',
    truncated: false,
    instance: '',
    stop_pending: false,
    ...over,
  }
}

const BUSY: MonitorSnapshot = {
  entries: [
    entry({ label: 'descriptor-harness PR babysit', slot: 'chat-14', pid: 3052208, proc_count: 9, rss_mb: 6412, cpu_pct: 348, uptime_s: 5765 }),
    entry({ label: 'chat-resource-monitor build', slot: 'chat-11', pid: 3053246, proc_count: 7, rss_mb: 2210, cpu_pct: 41, uptime_s: 5710 }),
    entry({ kind: 'subagent', label: 'Implement task 2.4: host context and caching', agent: 'claude-opus-5', pid: 3167291, proc_count: 4, rss_mb: 1480, cpu_pct: 96, uptime_s: 412, subagent_id: 'agent-2f9c', session_key: 'chat-11' }),
    entry({ label: 'folder-steering PR #11827', slot: 'chat-9', pid: 3052482, proc_count: 5, rss_mb: 912, cpu_pct: 3, uptime_s: 5690 }),
    entry({ label: 'slice-admission PR #11807', slot: 'chat-7', pid: 3053843, proc_count: 5, rss_mb: 640, cpu_pct: 0, uptime_s: 5680 }),
    entry({ label: 'Untitled chat', slot: 'chat-22', pid: 3081770, proc_count: 3, rss_mb: 402, cpu_pct: null, uptime_s: 61 }),
    entry({ kind: 'worker', label: 'review pool', agent: 'kirocrew-lite', pid: 3058319, proc_count: 2, rss_mb: 335, cpu_pct: 1, uptime_s: 5650 }),
    entry({ kind: 'gateway', label: 'gateway', agent: '', pid: 3048067, proc_count: 6, rss_mb: 3418, cpu_pct: 12, uptime_s: 5820 }),
  ],
  posture: 'tight',
  available_gb: 3.6,
  host_total_gb: 30.0,
  cpu_count: 8,
  cgroup_used_gb: 19.4,
  cgroup_limit_gb: 23.0,
  sampling_supported: true,
  captured_at: Date.now() / 1000,
  interval_s: 2.0,
}

const UNSUPPORTED: MonitorSnapshot = {
  ...BUSY,
  entries: [],
  posture: 'ample',
  available_gb: 21.3,
  host_total_gb: 32.0,
  cpu_count: 10,
  cgroup_used_gb: null,
  cgroup_limit_gb: null,
  sampling_supported: false,
}

const SNAPSHOT = scene === 'unsupported' ? UNSUPPORTED : BUSY

// The page's own fetches: a capture page has no gateway behind it.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  if (url.includes('/api/system/chat-resources')) {
    return new Response(JSON.stringify(SNAPSHOT), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  if (url.includes('/api/')) {
    return new Response(JSON.stringify({ ok: true }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  return realFetch(input, init)
}) as typeof window.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/monitor']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', minHeight: '100vh' }}
          data-capture-root
        >
          <SystemMonitorPage />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
