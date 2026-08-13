/**
 * Isolated capture entry for the side panel's oversize-question refusal.
 *
 * WHY ISOLATED: the side panel lives inside a chat session, which needs a live
 * gateway and an open slot — neither exists in a capture run. This mounts the
 * REAL SideChat against the real stylesheet and theme tokens with `fetch`
 * stubbed at the network seam; the refusal under review fires entirely
 * client-side (the byte guard runs before any request), so the code path the
 * screenshot documents is exactly the shipped one.
 *
 * Language + theme come from the query string: ?lang=zh-CN&theme=dark
 * The capture script types the oversize question itself, so each screenshot is
 * the component refusing real input rather than a hand-posed error string.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import SideChat from '../src/pages/chat/SideChat'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n(lang)
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
