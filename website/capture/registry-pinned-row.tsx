/**
 * Isolated capture entry for the build-pinned registry row.
 *
 * WHY ISOLATED: RegistryManager lives inside the Apps page, which needs the app
 * shell, a live websocket and a seeded app list to render; a half-stubbed shell
 * renders its error boundary instead, and a screenshot of the wrong thing is
 * worse evidence than none.
 *
 * The stub replaces the BACKEND, not the component: `api.listRegistries` answers
 * with the same `{registries, pinned}` shape `GET /api/apps/registries` now
 * returns, so the rendered rows are the ones production renders. What the
 * screenshots have to show is the DIFFERENCE between the two row kinds — a
 * pinned row carries the badge and no remove control, an operator row is fully
 * editable — so both are present in one frame.
 *
 * Theme comes from the query string: ?theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n — without calling it, every label in the frame is blank, which
// silently produces screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n'
import RegistryManager from '../src/components/RegistryManager'
import { api } from '../src/api/client'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The two row kinds, in the shape the endpoint sends them. `pinned` is reported
// separately from `registries` precisely because PUT replaces the latter
// verbatim, so the component must never fold one into the other.
//
// Two pinned rows, one per tier, because the ICON is tier-dependent: the shield
// is reserved for `owner` (the only tier that clones with this machine's
// credentials) and a pin marks the default `index` tier. One row of each is what
// shows that distinction rather than asserting it.
const PINNED = [
  {
    name: 'Platform Apps',
    repo: 'https://forge.internal.example/platform/app-registry.git',
    branch: 'main',
    trust: 'index',
  },
  {
    name: 'Trusted Platform',
    repo: 'https://forge.internal.example/platform/trusted.git',
    branch: 'main',
    trust: 'owner',
  },
]
const OPERATOR = [
  {
    name: 'My Team',
    repo: 'https://forge.internal.example/my-team/apps.git',
    branch: 'main',
    trust: 'index',
  },
]

api.listRegistries = async () => ({ registries: OPERATOR, pinned: PINNED })
api.refreshRegistries = async () => ({
  ok: true,
  refreshed: [],
  failed: [],
  results: [],
  apps: 0,
  lastSyncedAt: new Date().toISOString(),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// Width follows the VIEWPORT rather than a fixed pixel value: the narrow scene
// exists to show the row wrapping at 320px, and a hard-coded width would make
// that frame identical to the wide one while still appearing to prove it.
const App = () => (
  <QueryClientProvider client={qc}>
    <div data-capture-root style={{ padding: 16, width: '100%', boxSizing: 'border-box' }}>
      <RegistryManager />
    </div>
  </QueryClientProvider>
)

initI18n('en')
createRoot(document.getElementById('root')!).render(<App />)
