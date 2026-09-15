/** Isolated capture entry for the crew webview EMPTY state.
 *
 * WHY ISOLATED: the empty state is what every crew's drawer shows until that
 * crew publishes a panel (its `panel_publish` MCP server is opt-in and off by
 * default), so in a live gateway reaching it means a crew with no panel record.
 * Here the real `CrewWebview` mounts over a fetch stub that answers the panel
 * read with `{ html: null }` -- the exact `!html` branch the component renders
 * as its empty state, so the copy in the shot is the shipped copy, not a mock.
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, its real React Query read through
 * the real `api.memberPanel` layer, and the real `webview_empty` catalog
 * string. Nothing about the component or the string is changed for the camera;
 * the fetch boundary only supplies the no-panel record that produces the state.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
// Initialise i18next exactly as main.tsx does -- without it the empty-state
// sentence is a blank key and the screenshot misrepresents the real UI.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import CrewWebview from '../src/pages/members/CrewWebview'
import { i18nT } from '../src/i18n/t'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
// ThemeProvider is the authority: seed the preference it reads, and set the
// attribute for the pre-effect first paint.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'research'
const MEMBER = 'research'

/** Fetch stub at the API boundary: the panel read answers a no-panel record
 *  (`html: null`), which is the empty-state branch; every other dashboard read
 *  answers an empty payload so no code path hangs on a gateway that is absent. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  // The member-panel read: a crew that has published nothing yet.
  if (url.includes('/panel')) return json({ html: null, panel: null })
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    // A right-dock-sized frame, the width the crew drawer occupies beside the
    // members list -- the empty state's real home.
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
