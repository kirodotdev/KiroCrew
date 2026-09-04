/**
 * Isolated capture entry for Dev Fleet's two checkout-discovery states.
 *
 * WHY ISOLATED: reaching /dev-fleet through the full SPA needs a live gateway,
 * and the states under review are precisely the ones where no checkout is
 * readable — impossible to stage against a real backend without breaking it.
 * This mounts the REAL DevFleetPage against the real stylesheet and theme
 * tokens with `fetch` stubbed at the network seam, so the branch that picks the
 * state executes exactly as it does in production; the stub replaces the
 * backend, not the component.
 *
 * Scene + theme come from the query string: ?scene=setup&theme=dark
 *   setup — no checkout was found anywhere: a setup prompt, not a failure
 *   error — a checkout WAS named and git cannot read it: the error banner,
 *           which still names the path because the user chose it
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import DevFleetPage from '../src/pages/DevFleetPage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'setup'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const FLEET = scene === 'error'
  ? {
    worktrees: [],
    error: 'main checkout not found: /opt/checkouts/kirocrew is missing or not a git '
      + 'checkout. It is set by the KIROCREW_DEVFLEET_REPO environment variable.',
  }
  : { worktrees: [], needs_setup: true }

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/fleet')) {
    return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dev-fleet']}>
        <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
          <DevFleetPage />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
