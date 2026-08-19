/**
 * Isolated capture entry for the update-capability affordances.
 *
 * WHY ISOLATED: the same reason `capture/update-card.tsx` gives — driving these
 * states through the full SPA needs the whole shell to boot on a dozen /api
 * fixtures plus live websocket frames, and a half-stubbed shell renders its error
 * boundary instead of the page, which is worse evidence than none.
 *
 * Both scenes mount the REAL component against the REAL stylesheet and theme
 * tokens, with the gateway status seeded through the same `sseStatus` action the
 * WebSocket push dispatches in production — so what is captured is the component
 * reading the actual contract, not a mock of it.
 *
 * Scene + theme come from the query string: ?scene=settings-dot&theme=dark
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
import SettingsPage from '../src/pages/SettingsPage'
import '../src/index.css'

const COMMAND =
  "curl -fsSL --proto '=https' https://download.crew.kiro.dev/cli.sh | sh -s -- --channel insider"

/** A wheel install (the cli.sh managed venv) with an update waiting. */
const WHEEL_UPDATE_AVAILABLE = {
  uptime: '3h',
  sessions: 1,
  messages: 42,
  cron_jobs: 2,
  subagents: 0,
  lessons: 7,
  version: '0.2.0rc8',
  platform: 'linux',
  update_available: true,
  update_can_apply: false,
  update_check_status: 'succeeded',
  update_command: COMMAND,
  update_channel: 'insider',
}

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'settings-dot'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

store.dispatch(sseStatus(WHEEL_UPDATE_AVAILABLE as never))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const body =
  scene === 'wheel-command' ? (
    <div style={{ maxWidth: 720 }}>
      <AboutPanel />
    </div>
  ) : (
    <SettingsPage />
  )

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', minHeight: '100vh' }}
          data-capture-root
        >
          {body}
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
