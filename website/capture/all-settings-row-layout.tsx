/**
 * Isolated capture entry for Settings -> Advanced -> All settings rows that hold
 * a long current value: a link row, a read-only row and an edit row.
 *
 * WHY ISOLATED: the page needs a live gateway for `GET /api/config/schema` and
 * the config view. This mounts the REAL AllSettingsList against the real
 * stylesheet, theme tokens and i18n catalog, with the schema and config answers
 * replaced by fixtures. Component render, not the live page.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { api } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { AllSettingsList } from '../src/components/allSettings/AllSettingsList'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
// ?pane=<px> narrows the settings pane inside a wide window, as the app rail and
// the settings nav do on a mid-size desktop.
const pane = Number(params.get('pane')) || 860
document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

const APPS = [
  'personal-shopper', 'design-critique', 'meeting-notes', 'travel-planner', 'code-review-sage',
  'expense-tracker', 'recipe-box', 'standup-bot', 'reading-list', 'spec-builder',
]

const ENTRIES = [
  { path: 'agent.apps_trusted', type: 'array', label: 'Trusted Apps', help: 'Apps whose tools run without asking each time.', defaultValue: [], allSettings: { mode: 'control', page: { tab: 'security' } } },
  { path: 'agent.sandbox_extra_paths', type: 'array', label: 'Sandbox extra paths', help: 'Folders the sandbox lets the agent write to.', defaultValue: [], allSettings: { mode: 'readonly', reason: 'safety' } },
  { path: 'dashboard.url', type: 'string', label: 'Dashboard URL', help: 'Public URL for the dashboard (used in Slack links).', defaultValue: '', allSettings: { mode: 'readonly', reason: 'text' } },
  { path: 'mcp_gateway.keep_alive_servers', type: 'array', label: 'Keep-alive servers', help: 'Servers to keep running.', defaultValue: [], allSettings: { mode: 'edit', control: 'str_list', resettable: true } },
  { path: 'agent.streaming', type: 'boolean', label: 'Streaming', help: '', defaultValue: true, allSettings: { mode: 'edit', control: 'bool', resettable: true } },
]

const CONFIG = {
  agent: {
    apps_trusted: APPS,
    sandbox_extra_paths: ['/home/me/projects', '/home/me/.cache/build-tools', '/mnt/shared/datasets/2026', '/opt/tools/bin', '/srv/scratch', '/var/tmp/builds', '/home/me/notes'],
    streaming: true,
  },
  dashboard: { url: 'https://crew-dashboard.example-company-internal-network.com/a/very/long/path/that/keeps/going' },
  mcp_gateway: { keep_alive_servers: ['github', 'slack'] },
}

// No gateway here: the schema request and every api call answer from the fixtures.
const realFetch = window.fetch.bind(window)
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/config/schema')) return new Response(JSON.stringify({ entries: ENTRIES }), { status: 200 })
  return realFetch(input, init)
}) as typeof fetch
const stub = api as unknown as Record<string, unknown>
stub.kirocrewConfig = async () => CONFIG
stub.patchConfig = async () => CONFIG

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div data-capture-root className="bg-bg text-text min-h-screen px-4 py-5 sm:p-8">
          <div style={{ maxWidth: pane }}>
            <p className="text-[12px] text-muted mb-3">Component render, not the live page</p>
            <AllSettingsList />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
