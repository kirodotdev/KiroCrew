/** Isolated capture entry for the EXPANDED view's FIRST-MINT failure.
 *
 * WHY A SEPARATE ENTRY: the mint-error sibling answers whether the failure band
 * stays legible OVER a document, so its gateway mints once before refusing. This
 * shot answers the other failure the component distinguishes: nothing has ever
 * rendered. `useSandboxDoc` has no `url` to keep, so the band carries the hard
 * "could not be rendered" sentence and the frame below it holds nothing -- the
 * "Rendering the dashboard" line is withheld once a mint has failed and no retry
 * is running. UX review asked for this state by name: no earlier shot showed the
 * band with an empty frame behind it.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, the real `useSandboxDoc` failure
 * path, the real `ErrorNotice` and `webview_retry` control. Two things are
 * stubbed and both are the gateway: the panel read answers from the shared
 * fixture, and EVERY mint POST answers HTTP 500, so the first expand is the
 * failure.
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
import { PANEL_HTML, PANEL_META } from './crewWebviewFixture'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'research'
const MEMBER = 'research'

const json = (body: unknown, status = 200) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )

/** Fetch stub at the API boundary. The panel read succeeds so the crew has a
 *  published record; the mint refuses every time, so the very first expand
 *  fails with no document to fall back on. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/api/sandbox-doc')) return json({ error: 'document mint unavailable' }, 500)
  if (url.includes('/panel')) return json({ html: PANEL_HTML, panel: PANEL_META })
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    // Full-viewport: the expanded view is `fixed inset-0`, so a dock-width frame
    // would show the overlay at the wrong size.
    <div
      style={{ width: '100%', height: '100vh', display: 'flex', flexDirection: 'column', padding: 16 }}
      className="bg-bg text-text"
    >
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
