/**
 * Isolated capture entry for a Remote Crew row whose connection method this
 * build has no copy for, beside an ordinary SSH row.
 *
 * WHY ISOLATED: an unmapped method only reaches the panel from a gateway that is
 * newer than the frontend (or a hand-edited registry), which no stock dev setup
 * produces. The two gateway reads the crew list needs (`GET /api/instances`,
 * `GET /api/cloud/launch`) are stubbed; everything else is real: the REAL
 * RemoteCrewPanel, the REAL stylesheet and theme tokens, and the InstanceView
 * shape the gateway emits.
 *
 * The shooter hovers the unmapped badge so the native tooltip is not needed: the
 * frame shows the badge, and the shooter records its `title` text beside it.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { RemoteCrewPanel } from '../src/pages/settings/RemoteCrewPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const BASE = {
  ssm_target: '',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
}
const INSTANCES = [
  {
    ...BASE,
    id: 'm1',
    name: 'dev-box-1',
    connection_method: 'ssh',
    ssh_host: 'dev-box-1',
    status: { instance_id: 'm1', state: 'disconnected' },
  },
  {
    // A method a newer gateway knows and this build does not.
    ...BASE,
    id: 'u1',
    name: 'future-crew',
    connection_method: 'outbound',
    ssh_host: '',
    status: { instance_id: 'u1', state: 'disconnected' },
  },
]

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

// A capture page has no gateway behind it.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  if (url.includes('/api/instances')) {
    return json({ active: true, warm_set_cap: 5, sso: {}, instances: INSTANCES })
  }
  if (url.includes('/api/cloud/launch')) return json({ jobs: [] })
  if (url.includes('/api/')) return json({})
  return realFetch(input, init)
}) as typeof window.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }} data-capture-root>
          <div style={{ maxWidth: 820 }}>
            <RemoteCrewPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
