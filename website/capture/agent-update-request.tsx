/**
 * Isolated capture entry for the agent update-request card in Settings › About
 * (issue #503).
 *
 * WHY ISOLATED: the state needs `window.updateAPI` — which exists only inside
 * the packaged Electron shell — and a gateway holding a live request from an
 * agent. Both are stubbed; everything else is real: the REAL AboutPanel, the
 * REAL stylesheet and theme tokens, and the same payload shapes the gateway and
 * the main process actually emit.
 *
 * Scenes (?scene=):
 *   none      a packaged install with no request. The BEFORE frame — the card
 *             must be absent, not merely empty.
 *   pending   chat-42 asked for v0.6.0. The card names version, requester and
 *             the remaining window, and offers Install & restart beside Decline.
 *   differs   the same request, but the app's updater found v0.7.0. The card
 *             says which version installing would actually deliver.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { sseStatus } from '../src/store/dashboardSlice'
import { AboutPanel } from '../src/pages/settings/AboutPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'pending'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

const REQUEST = {
  armed: true,
  managed_by: 'electron',
  version: '0.6.0',
  requested_by: 'chat-42',
  armed_at: Date.now() / 1000 - 90,
  // The production TTL is a day; the stub must produce what a user would see.
  expires_in: 24 * 60 * 60 - 90,
}

const noop = async () => ({ ok: true })
;(window as unknown as { updateAPI?: unknown }).updateAPI = {
  onState: () => () => {},
  check: noop,
  download: noop,
  install: noop,
  getInfo: async () => ({
    version: '0.5.0',
    channel: 'stable',
    stampedChannel: 'stable',
    channelSwitchable: true,
    channelPreference: 'stable',
    platform: 'darwin-arm64',
    packaged: true,
    autoDownload: true,
    laneVersion: '0.6.0',
    runningAheadOfLane: false,
    downloadUrl: 'https://download.crew.kiro.dev/desktop/stable/latest',
  }),
  setChannel: noop,
  setAutoDownload: noop,
}

store.dispatch(sseStatus({
  uptime: '4h',
  sessions: 2,
  messages: 0,
  cron_jobs: 0,
  lessons: 0,
  version: '0.5.0',
  version_display: '0.5.0',
  release_channel: 'stable',
  update_managed_by: 'electron',
  update_can_apply: false,
  update_can_arm: false,
  update_check_status: 'deferred',
} as never))

// The panel's own fetches: a capture page has no gateway behind it.
const realFetch = window.fetch
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : (input as Request).url ?? input)
  if (url.includes('/api/update/arm')) {
    return new Response(JSON.stringify(scene === 'none' ? { armed: false } : REQUEST), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  if (url.includes('/api/')) {
    return new Response(JSON.stringify({ auto_update: false }), {
      status: 200, headers: { 'content-type': 'application/json' },
    })
  }
  return realFetch(input, init)
}) as typeof window.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
if (scene === 'differs') {
  // The updater found a different version than the one requested.
  qc.setQueryData(['update-state'], { state: 'found', version: '0.7.0' })
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }}
          data-capture-root
        >
          <div style={{ maxWidth: 760 }}>
            <AboutPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
