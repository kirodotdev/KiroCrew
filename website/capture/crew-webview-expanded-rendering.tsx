/** Isolated capture entry for the EXPANDED view's pre-mint rendering state.
 *
 * WHY ISOLATED: this is the state every first expand passes through. The panel
 * read has already answered with a document, so `srcdoc` exists and
 * `useSandboxDoc` posts it to be minted -- but until that POST settles `url` is
 * null, and the expanded frame area renders the `webview_rendering` line
 * ("Rendering the dashboard...") instead of an iframe. Here the real
 * `CrewWebview` mounts over a fetch stub that answers the panel read and then
 * never settles the mint, so the component holds in that branch for the camera.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, its real React Query read through
 * `api.memberPanel`, the real `useSandboxDoc` hook, the real expand control and
 * the real `webview_rendering` catalog string. Only the fetch boundary is
 * stubbed, and only to hold the mint in flight the way a slow gateway would.
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

const json = (body: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )

/** Fetch stub at the API boundary: the panel read answers with a document, so
 *  the card renders and expanding builds a `srcdoc`; the mint POST never
 *  settles, which is what holds `url` at null and keeps the rendering line on
 *  screen. Every other read answers empty so no other path hangs. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/api/sandbox-doc')) {
    return new Promise<Response>(() => {
      /* never settles: the mint stays in flight */
    })
  }
  if (url.includes('/panel')) return json({ html: PANEL_HTML, panel: PANEL_META })
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    // Full-viewport: the expanded view is `fixed inset-0`, so a dock-width frame
    // would show the overlay at the wrong size. The docked column still renders
    // underneath, which is where the expand control lives.
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
