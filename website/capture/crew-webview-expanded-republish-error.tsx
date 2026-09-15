/** Isolated capture entry for the EXPANDED view after a REPUBLISH whose re-mint fails.
 *
 * WHY A THIRD FAILURE ENTRY: the mint-error sibling re-mints the SAME record (a
 * theme change), so its bar chip keeps the plain "Published" label. This entry
 * reaches the one state the "New version published" label exists for: the crew
 * republishes, the drawer's panel read returns the NEWER record, its mint fails,
 * and `useSandboxDoc` keeps the OLDER document on screen. Two versions now exist
 * on one surface -- the chip dates the new record, the band dates the shown
 * document -- and UX review asked to see that arrangement rather than infer it.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, the real `useSandboxDoc` retention
 * rule, the real `ErrorNotice` and `webview_retry` control, and a real panel
 * refetch through react-query's own invalidation (which is what any live
 * refresh of the drawer does). Two things are stubbed and both are the gateway:
 * the panel read answers with the fixture record the first time and a record
 * published three days LATER with different html the second time, and the mint
 * POST answers with a same-origin stand-in document once and HTTP 500 after that.
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

/** The republished record: three days after the fixture's instant, so the two
 *  relative ages the shot must show cannot collide, and with different html so
 *  `srcdoc` changes and the hook re-mints for the reason it does in production. */
const REPUBLISHED_AT = new Date(
  new Date(PANEL_META.published_at).getTime() + 3 * 86400_000,
).toISOString()
const PANEL_META_V2 = {
  ...PANEL_META,
  published_at: REPUBLISHED_AT,
  data: { ...PANEL_META.data, subtitle: 'Cycle 42', sources_read: 152 },
}
const PANEL_HTML_V2 = PANEL_HTML.replace('Research crew dashboard', 'Research crew dashboard v2')

const json = (body: unknown, status = 200) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )

/** Fetch stub at the API boundary. The first panel read is the fixture record
 *  and its mint lands; the second read is the republished record and every mint
 *  from then on refuses, which is what leaves the OLD document on screen under a
 *  NEWER record. */
let panelReads = 0
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
  if (url.includes('/panel')) {
    panelReads += 1
    if (panelReads === 1) return json({ html: PANEL_HTML, panel: PANEL_META })
    return json({ html: PANEL_HTML_V2, panel: PANEL_META_V2 })
  }
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

/** The republish trigger the capture clicks: it invalidates the drawer's panel
 *  query, which is exactly what a live refresh does, so the component refetches
 *  through the stub above and receives the newer record. Parked off-screen so it
 *  cannot obstruct the shot. */
function RepublishSwitch() {
  return (
    <button
      type="button"
      data-testid="capture-republish"
      onClick={() => void qc.invalidateQueries({ queryKey: ['member-panel', SLUG, MEMBER] })}
      style={{ position: 'fixed', left: -9999, top: -9999 }}
    >
      republish
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
      <RepublishSwitch />
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
