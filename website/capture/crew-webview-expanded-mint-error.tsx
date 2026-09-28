/** Isolated capture entry for the EXPANDED view's MINT-FAILURE overlay.
 *
 * WHY ISOLATED: the question this shot answers is whether the failure bar stays
 * legible ON TOP OF a rendered document, so the frame must already hold content
 * when the failure arrives. That is a real sequence, not a contrived one:
 * `useSandboxDoc` keeps `url` when a LATER mint fails (its own doc says "`url`
 * may still hold a working document"), and a theme change rebuilds `srcdoc` and
 * re-mints. So the first mint lands, the reader switches theme, the second mint
 * fails, and the bar appears over the document the first mint produced.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, the real `useSandboxDoc` retention
 * rule, the real `ErrorNotice` and `webview_retry` control, and a real theme
 * change through the app's own `useTheme().setTheme`. Two things are stubbed and
 * both are the gateway: the panel read answers from a fixture, and the mint POST
 * answers with a same-origin stand-in document the first time and HTTP 500 after
 * that. The stand-in stands in only for the minted URL's CONTENT -- it loads in
 * the same `allow-scripts` opaque origin a minted document does.
 *
 * Query string: ?theme=dark|light  (the START theme; the capture switches it)
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider, useTheme } from '../src/hooks/useTheme'
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

/** Fetch stub at the API boundary. The mint answers ONCE with the stand-in
 *  document's URL and refuses every attempt after it, which is what produces
 *  `failed` while `url` still holds the landed document. */
let mints = 0
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/api/sandbox-doc')) {
    mints += 1
    if (mints === 1) return json({ url: '/capture/crew-webview-doc.html' })
    return json({ error: 'document mint unavailable' }, 500)
  }
  if (url.includes('/panel')) return json({ html: PANEL_HTML, panel: PANEL_META })
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** The theme switch the capture clicks to trigger the second mint. It is the
 *  app's own setter, so the re-mint happens for the reason it happens in
 *  production rather than because the harness poked the hook. Positioned off the
 *  expanded overlay's stacking context so the shot is not obstructed. */
function ThemeSwitch() {
  const { setTheme } = useTheme()
  return (
    <button
      type="button"
      data-testid="capture-theme-switch"
      onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}
      style={{ position: 'fixed', left: -9999, top: -9999 }}
    >
      switch theme
    </button>
  )
}

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
      <ThemeSwitch />
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
