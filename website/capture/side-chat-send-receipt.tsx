/**
 * Isolated capture entry for SideChat's send-failure strip (chat-core P2).
 *
 * WHY ISOLATED: a refused side send needs a server that answers `/side/turn`
 * with a non-2xx (here a 409 "side turn already in flight") -- none exists in a capture
 * run. This mounts the REAL SideChat against the real store and stylesheet with
 * fetch stubbed so `/side/open` succeeds and `/side/turn` is refused, so the
 * strip that renders the failure can be captured before/after it moved onto the
 * shared ErrorNotice.
 *
 * Query: ?theme=dark|light ?lang=<locale>
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected } from '../src/store/dashboardSlice'
import SideChat from '../src/pages/chat/SideChat'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.endsWith('/side/turn')) {
    return Promise.resolve(new Response(JSON.stringify({ error: 'side turn already in flight' }), { status: 409, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n(lang)
// The composer refuses to send while the gateway reads as offline; the scene is a
// refused SEND, so the dashboard must read as connected.
store.dispatch(sseConnected())
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        {/* The side panel's real habitat is a ~420px dock on the right edge. */}
        <div style={{ width: 420, height: '100vh', marginLeft: 'auto', display: 'flex', flexDirection: 'column', borderLeft: '1px solid var(--border)', background: 'var(--bg)' }}>
          <SideChat slot="capture-slot" />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
