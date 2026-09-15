/**
 * Isolated capture entry for the desktop auto-download opt-out in Settings >
 * About.
 *
 * WHY ISOLATED, and why this one cannot be captured any other way: the toggle
 * renders only when `window.updateAPI.setAutoDownload` exists, i.e. inside the
 * packaged Electron shell. A browser visiting the real dashboard has no
 * `updateAPI` at all, so it takes the gateway branch and never renders this row
 * — the surface is unreachable from a plain SPA session by construction, not by
 * fixture laziness.
 *
 * So the desktop bridge is stubbed and everything else is real: the REAL
 * AboutPanel, the REAL stylesheet and theme tokens, and the same `getInfo()`
 * payload shape the main process returns. What differs between the two scenes is
 * exactly one field of that payload, `autoDownload`, which is the whole contract
 * under test.
 *
 * Scene + theme come from the query string: ?scene=on&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only DEFINES
// initI18n, and without calling it every label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { AboutPanel } from '../src/pages/settings/AboutPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
// "on" is the shipped default; "off" is the opt-out the toggle exists to offer.
const scene = params.get('scene') || 'on'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

const noop = async () => ({ ok: true })

;(window as unknown as { updateAPI?: unknown }).updateAPI = {
  onState: () => () => {},
  check: noop,
  download: noop,
  install: noop,
  getInfo: async () => ({
    version: '0.4.0',
    channel: 'stable',
    stampedChannel: 'stable',
    channelSwitchable: true,
    channelPreference: '',
    platform: 'darwin-arm64',
    packaged: true,
    autoDownload: scene !== 'off',
  }),
  setChannel: noop,
  // Presence of this bridge is what makes the row render at all.
  setAutoDownload: noop,
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }}
          data-capture-root
        >
          <div style={{ maxWidth: 720 }}>
            <AboutPanel />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
