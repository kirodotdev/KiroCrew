/**
 * Isolated capture of the real AppDetailPage after a failed registry install.
 * Only API reads and the streaming install result are stubbed. AppDetailPage
 * passes the result's error to its own ErrorNotice and shows Install failed.
 * ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import dashboardReducer, { sseConnected } from '../src/store/dashboardSlice'
import { initI18n } from '../src/i18n'
import { ThemeProvider } from '../src/hooks/useTheme'
import AppDetailPage from '../src/pages/AppDetailPage'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/apiError'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', `kiro-${theme}`)

// Exact gh_signed_out sentence from _gh_refusal_sentence for acme/team-apps.
const ERROR = "acme/team-apps looks private. Sign in to the GitHub CLI (gh) on the computer that runs Kiro Crew, with an account that can read acme/team-apps. Kiro Crew uses that computer's GitHub sign-in."
api.getApp = async () => { throw new ApiError(404, 'Not installed') }
api.listRegistry = async () => ({ apps: [{
  name: 'team-app', displayName: 'Team App', version: '1.0.0',
  description: 'An app from the private team repository.',
  repo: 'https://github.com/acme/team-apps', category: 'productivity',
}], serverPlatform: { os: 'linux', arch: 'x64' }, categoryOrder: [], editorialSections: [] })
api.system = async () => ({ hostname: 'crew-host', os: 'linux', arch: 'x64' })
api.appContributors = async () => ({ contributors: [] })
api.themeBoot = async () => ({ mode: theme, color: 'kiro', onboarded: true, import_onboarded: true })
api.themes = async () => ({ themes: [], installed: [] })
api.installFromRegistryStream = async () => ({ ok: false, name: 'team-app', code: 'gh_signed_out', error: ERROR })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const store = configureStore({ reducer: { dashboard: dashboardReducer } })
store.dispatch(sseConnected())

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/apps/detail/team-app']}>
          <div data-capture-root style={{ padding: 16, width: '100%', boxSizing: 'border-box' }}>
            <Routes><Route path="/apps/detail/:name" element={<AppDetailPage />} /></Routes>
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
