/** Isolated capture entry for the crew webview LOADING (skeleton) state.
 *
 * WHY ISOLATED: the loading state is what the drawer shows while the
 * member-panel read is in flight. Here the real `CrewWebview` mounts over a
 * fetch stub that never resolves the panel read, so React Query stays
 * `isLoading` and the component's `crew-webview-loading` skeleton branch is the
 * one that renders -- the pulsing placeholder the UX review said appeared in no
 * screenshot.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, its real React Query read, and the
 * real skeleton markup. Only the fetch boundary is stubbed, and only to hold the
 * read pending the way a slow gateway would.
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

/** Fetch stub: the panel read never resolves, holding React Query in its
 *  in-flight (`isLoading`) state; other reads answer empty. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/panel')) return new Promise<Response>(() => { /* never resolves */ })
  return Promise.resolve(new Response(JSON.stringify({}), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

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
