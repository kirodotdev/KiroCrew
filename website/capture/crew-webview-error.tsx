/** Isolated capture entry for the crew webview FETCH-ERROR state.
 *
 * WHY ISOLATED: the error state is what the drawer shows when the member-panel
 * read itself fails (network error, gateway 5xx) rather than returning a
 * no-panel record. Here the real `CrewWebview` mounts over a fetch stub that
 * answers the panel read with HTTP 500, so React Query's `isError` branch is
 * the one that renders -- the shipped `webview_error` notice plus the
 * `crew-webview-error-retry` "Try again" control the UX review said appeared in
 * no screenshot.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, its real React Query read through
 * the real `api.memberPanel` layer, the real `ErrorNotice`, and the real
 * `webview_error` / `webview_retry` catalog strings. Only the fetch boundary is
 * stubbed, and only to make the read fail the way a dead gateway would.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import CrewWebview from '../src/pages/members/CrewWebview'
import { i18nT } from '../src/i18n/t'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'research'
const MEMBER = 'research'

/** Fetch stub at the API boundary: the panel read answers HTTP 500, which is
 *  the read-failed branch (`isError`); every other read answers empty so no
 *  other code path hangs on an absent gateway. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/panel')) {
    return Promise.resolve(new Response(JSON.stringify({ error: 'gateway unavailable' }), {
      status: 500, headers: { 'Content-Type': 'application/json' },
    }))
  }
  return Promise.resolve(new Response(JSON.stringify({}), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

// retry:false so the error branch renders promptly for the camera rather than
// after React Query's default back-off ladder.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    <div style={{ width: 320, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column', padding: 16 }} className="bg-bg text-text">
      <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5">
        {i18nT('pages.membersPage.webview_heading')}
      </div>
      <CrewWebview slug={SLUG} member={MEMBER} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
