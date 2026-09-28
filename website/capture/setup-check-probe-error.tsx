/**
 * Isolated capture entry for KiroPrerequisiteGate's probe diagnostic
 * (PR #9646): when the gateway's kiro-cli probe raises, the backend backstop
 * degrades it to a 200 not-ready body carrying `probe_error` / `probe_status`,
 * and the "Setup check unavailable" screen now names the failing probe and its
 * exit status instead of showing no reason at all.
 *
 * Mounts the REAL gate with the same providers the app gives it. The
 * `/api/kiro-prerequisite` response comes from the capture script's route
 * interception (gateway-free) — see scripts/capture-9646-closeout.mjs for the
 * status fixture. The children are a stand-in dashboard that must NOT render
 * (the gate blocks on the diagnostic).
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import KiroPrerequisiteGate from '../src/components/KiroPrerequisiteGate'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div className="h-screen bg-bg text-text" data-capture-root>
          <KiroPrerequisiteGate>
            <div data-capture-dashboard>Dashboard loaded</div>
          </KiroPrerequisiteGate>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
