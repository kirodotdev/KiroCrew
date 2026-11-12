/**
 * Isolated capture entry for Settings -> Browser.
 *
 * WHY ISOLATED: /settings/browser through the full SPA needs a live gateway, a
 * dashboard credential and a Browser Harness in a chosen state; without them the
 * shell renders its prerequisite gate. This mounts the REAL BrowserPanel against
 * the real stylesheet, theme tokens and i18n catalog, under the page's own title
 * and description, with the gateway's answers replaced by fixtures on the same
 * `api` methods the panel calls.
 *
 * Query string:
 *   ?state=connected|installing|waiting_allow|error|install_failed
 *   &theme=dark|light
 *   &cred=applied|needs_refresh   (an edition sign-in provider is present)
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { api, type BrowserStatusData } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import { store } from '../src/store'
import { BrowserPanel } from '../src/pages/settings/BrowserPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const state = params.get('state') || 'connected'
document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

const base: BrowserStatusData = {
  installed: true,
  version: '0.4.2',
  browser: { kind: 'user_chrome', headless: false },
  connection: 'connected',
  detail: null,
  chromium: { downloaded: true, version: '131' },
  install_job: null,
  platform: 'darwin',
}

const STATES: Record<string, BrowserStatusData> = {
  connected: base,
  installing: {
    ...base, installed: false, version: null, browser: null, connection: 'disconnected',
    chromium: { downloaded: false, version: null }, install_job: { status: 'running', stage: 'chromium' },
  },
  waiting_allow: { ...base, connection: 'waiting_allow' },
  error: { ...base, connection: 'error', detail: 'Chrome closed the debugging connection (ECONNRESET).' },
  install_failed: {
    ...base, installed: false, version: null, browser: null, connection: 'disconnected',
    chromium: { downloaded: false, version: null },
    install_job: { status: 'failed', stage: 'chromium', error_detail: 'Downloading Chromium timed out (network).' },
  },
}
const cred = params.get('cred')
const CREDENTIALS: Record<string, BrowserStatusData['credentials']> = {
  applied: {
    provider: 'Example SSO', enabled: true, domains: ['example.com'],
    state: 'applied', reason: null, applied: 12, provider_status: null,
  },
  needs_refresh: {
    provider: 'Example SSO', enabled: true, domains: ['example.com'],
    state: 'needs_refresh', reason: 'source_expired', applied: 0,
    provider_status: { needs_refresh: true, message: 'Run example-login again to renew the sign-in.' },
  },
}
const status: BrowserStatusData = {
  ...(STATES[state] ?? base),
  ...(cred && CREDENTIALS[cred] ? { browser: { kind: 'kiro_chromium', headless: true }, credentials: CREDENTIALS[cred] } : {}),
}

const config = {
  browser: {
    choice: 'auto',
    live: { enabled: true, quality: 'auto', max_fps: 60, mask_passwords: true },
    credentials: { enabled: null, domains: cred ? ['example.com', 'example.org'] : [] },
  },
  dashboard: { browser_local_origins: ['localhost:5173', '127.0.0.1:3000'] },
}

// No gateway here: every call the panel makes answers from the fixtures.
const stub = api as unknown as Record<string, unknown>
stub.getBrowserStatus = async () => status
stub.kirocrewConfig = async () => config
stub.patchConfig = async () => ({ ok: true })
stub.installBrowserHarness = async () => status
stub.testBrowser = async () => ({ ok: true, connection: status.connection, detail: null })
stub.disconnectBrowser = async () => status

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div data-capture-root className="bg-bg text-text min-h-screen px-4 py-5 sm:p-8">
          <div className="max-w-[720px]">
            <h2 className="text-xl font-semibold text-text-strong m-0">{i18nT('settings.tabs.browser.label')}</h2>
            <p className="text-[13px] text-muted mt-1 mb-4">{i18nT('settings.tabs.browser.description')}</p>
            <BrowserPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
