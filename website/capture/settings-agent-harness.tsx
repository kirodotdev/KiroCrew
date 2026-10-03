/**
 * Isolated capture entry for Settings → Agent Harness (PR #13888).
 *
 * WHY ISOLATED: reaching /settings/agent through the full SPA needs a live
 * gateway plus a dashboard credential. This mounts the REAL SettingsPage (rail,
 * tab and the real AgentBackendTab) against the real stylesheet and theme
 * tokens; every /api/ call is answered by the capture script's route
 * interception (scripts/capture-13888-agent-harness.mjs), so no gateway is used.
 *
 * Query string: ?theme=dark|light&path=/settings/agent
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import SettingsPage from '../src/pages/SettingsPage'
import DeveloperPage from '../src/pages/DeveloperPage'
import OverviewPage from '../src/pages/OverviewPage'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const path = params.get('path') || '/settings/agent'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="h-screen bg-bg text-text" data-capture-root>
          <Routes>
            <Route path="/settings/*" element={<SettingsPage />} />
            <Route path="/developer" element={<DeveloperPage />} />
            <Route path="/overview" element={<OverviewPage />} />
          </Routes>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
