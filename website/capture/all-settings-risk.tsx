/**
 * Isolated capture entry for Settings -> Advanced -> All settings, showing rows
 * the owner may change despite their risk, each with its risk line.
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
document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

const edit = (control: string, risk?: string, extra: Record<string, unknown> = {}) => ({
  mode: 'edit', control, resettable: true, ...(risk ? { risk } : {}), ...extra,
})

const ENTRIES = [
  { path: 'memory.extraction_instructions', type: 'string', label: 'Extraction instructions', help: 'Free text, in your own words, that steers what history consolidation extracts.', defaultValue: '', allSettings: edit('str', undefined, { max_len: 2000 }) },
  { path: 'agent.dangerously_skip_permissions', type: 'boolean', label: 'Dangerously Skip Permissions', help: '', defaultValue: false, requiresRestart: true, allSettings: { mode: 'readonly', reason: 'safety' } },
  { path: 'agent.sandbox_readonly_paths', type: 'array', label: 'Sandbox read-only paths', help: '', defaultValue: [], allSettings: edit('str_list', 'safety', { max_items: 64, max_item_len: 256 }) },
  { path: 'dashboard.url', type: 'string', label: 'Dashboard URL', help: 'Public URL for the dashboard (used in Slack links).', defaultValue: '', allSettings: edit('str', undefined, { max_len: 256 }) },
  { path: 'messaging.dm_scope', type: 'string', label: 'DM scope', help: '', defaultValue: 'per-channel-peer', allSettings: edit('enum', 'reach', { values: ['per-channel-peer', 'unified'] }) },
  { path: 'tunnel.enabled', type: 'boolean', label: 'Tunnel', help: 'Enable a tunnel to expose the dashboard for remote access.', defaultValue: false, requiresRestart: true, allSettings: edit('bool', 'reach') },
  { path: 'dashboard.github_hosts', type: 'array', label: 'GitHub hosts', help: '', defaultValue: [], allSettings: edit('str_list', 'reach', { max_items: 64, max_item_len: 256 }) },
  { path: 'browser.live.mask_passwords', type: 'boolean', label: 'Mask passwords', help: '', defaultValue: true, allSettings: { mode: 'edit', control: 'bool', resettable: false, risk: 'sign_in' } },
  { path: 'telemetry.traces_enabled', type: 'boolean', label: 'Traces enabled', help: '', defaultValue: false, allSettings: edit('bool', 'share') },
  { path: 'dashboard.onboarded', type: 'boolean', label: 'Onboarded', help: '', defaultValue: false, allSettings: { mode: 'readonly', reason: 'marker' } },
]

const CONFIG = {
  memory: { extraction_instructions: 'keep build commands\nnever store customer names' },
  agent: { dangerously_skip_permissions: false, sandbox_readonly_paths: ['/mnt/c'] },
  messaging: { dm_scope: 'per-channel-peer' },
  tunnel: { enabled: false },
  dashboard: { github_hosts: ['github.example.com'], onboarded: true, url: 'https://crew.example.com' },
  browser: { live: { mask_passwords: true } },
  telemetry: { traces_enabled: false },
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
          <div className="max-w-[860px]">
            <p className="text-[12px] text-muted mb-3">Component render, not the live page</p>
            <AllSettingsList />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
