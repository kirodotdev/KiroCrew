/**
 * Isolated capture entry for a private GitHub registry the GitHub CLI on the
 * gateway's computer could not read.
 *
 * Mounts the real RegistryManager with the backend stubbed: `api.listRegistries`
 * answers one registry in a private GitHub repository, and `api.refreshRegistries`
 * answers the refresh result the backend sends for a `gh_*` cause, with `repo` as
 * `owner/repo`. The banner the capture shows is the component's own rendering of
 * that result.
 *
 * ?cause=gh_signed_out|gh_no_access|gh_missing|gh_strict_mode  ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseConnected } from '../src/store/dashboardSlice'

import { initI18n } from '../src/i18n'
import RegistryManager from '../src/components/RegistryManager'
import { api } from '../src/api/client'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const CAUSES = ['gh_signed_out', 'gh_no_access', 'gh_missing', 'gh_strict_mode']
const cause = CAUSES.includes(params.get('cause') ?? '') ? params.get('cause')! : 'gh_signed_out'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const REGISTRY = { name: 'Team Apps', repo: 'https://github.com/acme/team-apps.git', branch: 'main', trust: 'index' }

api.listRegistries = async () => ({ registries: [REGISTRY], pinned: [] })
api.refreshRegistries = async () => ({
  ok: false,
  refreshed: [],
  failed: ['Team Apps'],
  results: [{ name: 'Team Apps', ok: false, reason: cause, host: 'github.com', repo: 'acme/team-apps', cached: false }],
  apps: 0,
  lastSyncedAt: new Date().toISOString(),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
// The registry controls are enabled only while the gateway reads as connected.
const store = configureStore({ reducer: { dashboard: dashboardReducer } })
store.dispatch(sseConnected())

const App = () => (
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div data-capture-root style={{ padding: 16, width: '100%', boxSizing: 'border-box' }}>
        <RegistryManager />
      </div>
    </QueryClientProvider>
  </Provider>
)

initI18n('en')
createRoot(document.getElementById('root')!).render(<App />)
