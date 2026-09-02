/**
 * Isolated capture entry for the Crew Members page.
 *
 * Mounts the REAL MembersPage against the real stylesheet, theme tokens and
 * live i18n catalog. API responses come from the capture script's route
 * interception (gateway-free); this entry only seeds what the page reads
 * from the store — the live `slots` frames that drive the presence dots.
 *
 * Scenes via query string: ?theme=dark|light — the script drives the page
 * itself (clicking a REAL roster row opens the thread), so a frame documents
 * the shipped wiring, not forced component state.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import MembersPage from '../src/pages/members/MembersPage'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Live presence rides the WS `slots` frames; seed the same shape so the
// Radar dot renders "working" from the store, not from the roster snapshot.
store.dispatch(
  sseSlots([
    {
      key: 'member-radar',
      title: 'Radar',
      messages: 4,
      running: true,
      mode: 'member',
      agent: 'radar',
    },
  ] as never),
)

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="h-screen bg-bg text-text" data-capture-root>
            <MembersPage />
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
