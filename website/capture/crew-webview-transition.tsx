/** Isolated capture entry for the docked-to-expanded TRANSITION.
 *
 * WHY ISOLATED: the transition is the one thing a still cannot show -- the docked
 * summary card swapping to the `fixed inset-0` full-window subtree and back. This
 * entry is the successful path end to end: the panel read answers with a document
 * and the mint always lands, so the recording is expand, the rendered dashboard,
 * and collapse, with no error state in the frame.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, its real React Query read, the real
 * `useSandboxDoc` mint, the real expand and collapse controls, and the real
 * docked summary the shared fixture produces. Only the gateway is stubbed: the
 * panel read answers from the fixture and the mint answers with a same-origin
 * stand-in document, which loads in the same `allow-scripts` opaque origin a
 * minted document does.
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

/** Fetch stub at the API boundary: the panel read answers with a document and
 *  every mint lands, so the recording stays on the successful path. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/api/sandbox-doc')) return json({ url: '/capture/crew-webview-doc.html' })
  if (url.includes('/panel')) return json({ html: PANEL_HTML, panel: PANEL_META })
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    // A right-dock column inside a full viewport: the docked card has to be at
    // its real width for the swap to read as a swap, and the expanded view is
    // `fixed inset-0`, so the window has to be the whole frame.
    <div style={{ width: '100%', height: '100vh', display: 'flex' }} className="bg-bg text-text">
      <div
        style={{
          width: 320,
          marginLeft: 'auto',
          borderLeft: '1px solid var(--border)',
          display: 'flex',
          flexDirection: 'column',
          padding: 16,
        }}
      >
        <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5">
          {i18nT('pages.membersPage.webview_heading')}
        </div>
        <CrewWebview slug={SLUG} member={MEMBER} />
      </div>
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
