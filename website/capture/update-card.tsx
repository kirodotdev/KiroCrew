/**
 * Isolated capture entry for the update card's download states.
 *
 * WHY ISOLATED: driving these states through the full SPA needs the whole app
 * shell to boot, and the shell reads a dozen /api fixtures plus live websocket
 * frames — stubbing it faithfully enough turned into fixture whack-a-mole, and a
 * half-stubbed shell renders its error boundary instead of the page, which is
 * WORSE evidence than none (a green-looking screenshot of the wrong thing).
 *
 * This mounts AboutPanel against the REAL stylesheet and the REAL theme tokens,
 * with the update state seeded into the same ['update-state'] query cache that
 * useUpdateSubscription writes in production. What it does NOT capture is the
 * surrounding settings-page chrome — acceptable here because this change is
 * confined to the card.
 *
 * Scene + theme come from the query string: ?scene=downloading&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n -- without calling it, i18next.t() returns empty strings and every
// label in the captured frame is blank, which silently produces screenshots that
// misrepresent the real UI.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { AboutPanel } from '../src/pages/settings/AboutPanel'
import '../src/index.css'

const VERSION = '0.1.3-nightly.20260730t061200'
// Placeholder release notes. Deliberately generic: these render in the PR
// screenshots, and inventing plausible-looking real notes makes the evidence
// read as a release that does not exist. The real card shows whatever `notes`
// the feed carries.
const NOTES = 'Sample release notes — the update card renders whatever notes the feed provides.'

const SCENES: Record<string, Record<string, unknown>> = {
  found: {
    state: 'found',
    version: VERSION,
    channel: 'nightly',
    pubDate: '2026-07-30T06:12:00Z',
    notes: NOTES,
  },
  downloading: {
    state: 'downloading',
    version: VERSION,
    channel: 'nightly',
    percent: 47.4,
    bytesPerSecond: 3.35 * 1024 * 1024,
    notes: NOTES,
  },
  downloaded: {
    state: 'downloaded',
    version: VERSION,
    channel: 'nightly',
    pubDate: '2026-07-30T06:12:00Z',
    notes: NOTES,
  },
  'download-failed': {
    state: 'error',
    phase: 'download',
    code: 'offline',
    version: VERSION,
    channel: 'nightly',
    message: 'net::ERR_INTERNET_DISCONNECTED',
    notes: NOTES,
  },
  'install-failed': {
    state: 'error',
    phase: 'install',
    code: 'unknown',
    version: VERSION,
    channel: 'nightly',
    message: 'ShipIt could not replace the application bundle.',
    notes: NOTES,
  },
  'check-failed': {
    state: 'error',
    phase: 'check',
    code: 'server',
    httpStatus: 503,
    message: 'HttpError: 503',
  },
}

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'found'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

// The desktop bridge, as the Electron preload exposes it.
;(window as unknown as { updateAPI: unknown }).updateAPI = {
  onState: () => () => {},
  check: async () => ({ ok: true }),
  download: async () => ({ ok: true }),
  install: async () => ({ ok: true }),
  getInfo: async () => ({
    version: '0.1.2-nightly.20260729t073648',
    channel: 'nightly',
    stampedChannel: 'nightly',
    channelSwitchable: false,
    channelPreference: '',
    platform: 'darwin-arm64',
    downloadUrl: 'https://download.crew.kiro.dev/desktop/nightly/latest/KiroCrew.dmg',
    packaged: true,
  }),
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
qc.setQueryData(['update-state'], SCENES[scene] ?? SCENES.found)

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div
        style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24, minHeight: '100vh' }}
        data-capture-root
      >
        <div style={{ maxWidth: 720 }}>
          <AboutPanel />
        </div>
      </div>
    </QueryClientProvider>
  </Provider>,
)
