/**
 * Isolated capture entry for the Schedule page's Last run column retry note
 * (PR #9646): a job that succeeded only after transient retries now shows a
 * small "Retried N times" line under its last-run age, read from the new
 * `last_retry_count` wire field when its `last_retry_run_ts` matches the row's
 * `last_run_ts`.
 *
 * Mounts the REAL SchedulePage against the real stylesheet, theme tokens and
 * live i18n catalog. API responses come from the capture script's route
 * interception (gateway-free) — see scripts/capture-9646-closeout.mjs for the
 * job fixture (counts 3 / 1 / 0 bound to their run, plus one count from an
 * earlier run whose stamp does not match, so plural, singular, zero and the
 * withheld case sit in one table).
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import SchedulePage from '../src/pages/SchedulePage'
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
      <MemoryRouter initialEntries={['/schedule']}>
        <div className="h-screen flex flex-col bg-bg text-text" data-capture-root>
          <div className="flex-1 min-h-0">
            <SchedulePage />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
