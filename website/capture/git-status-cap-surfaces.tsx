/**
 * Isolated capture entry for the CAPPED `/api/project/git/status` listing on the
 * two surfaces outside the Git panel: the chat rail's Changed badge and the
 * Pierre Changed tree.
 *
 * WHY THIS EXISTS: the flag under test is only set once a working tree has more
 * than 500 changed files, which the capture host does not have and should not be
 * made to have. Stubbing the endpoint at the fetch boundary with the body the
 * handler actually returns -- 500 entries plus `truncated: true` -- makes the
 * frame deterministic, and a drift in that contract shows up here as a badge
 * that stops carrying the marker.
 *
 * WHY ISOLATED rather than a state in the dashboard harness: both surfaces live
 * behind unrelated toggles, and the subject is how a floor READS at each
 * surface's own dock width -- the rail badge is a 10px tabular glyph pair and the
 * tree notice is a single muted row above a long list.
 *
 * WHAT IS FAITHFUL: the REAL `FileBrowserRail`, the REAL
 * `PierreWorkspaceTreeImpl`, the real theme provider and the real i18n catalogs.
 * Nothing about the badge or the notice is mocked; only the endpoint is.
 *
 * `?capped=false` answers the complete-listing case, which is the control: the
 * same surface with the same number and no marker.
 *
 * Query string: ?theme=dark|light&surface=rail|tree&capped=true|false
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
// Initialise i18next exactly as main.tsx does -- without it every label in the
// frame is blank and the screenshot misrepresents the real UI.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import FileBrowserRail from '../src/pages/chat/FileBrowserRail'
import { PierreWorkspaceTreeImpl } from '../src/pierre/PierreWorkspaceTreeImpl'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const surface = params.get('surface') === 'tree' ? 'tree' : 'rail'
const capped = params.get('capped') !== 'false'

// `ThemeProvider` is the authority: it reads `mc-theme` and applies the palette
// itself, so setting `data-theme` alone is clobbered on mount.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT = '/home/dev/acme-api'

/** The handler's own cap. A capped listing is exactly this long and says so. */
const CAP = 500

/** Plausible changed paths, so the tree reads as a real working tree rather
 *  than 500 rows of one repeated name. */
const AREAS = ['api', 'auth', 'billing', 'cache', 'core', 'db', 'jobs', 'limits', 'mail', 'search']
const KINDS = ['handlers', 'models', 'routes', 'schema', 'service']

const FILES = Array.from({ length: CAP }, (_, i) => ({
  path: `src/${AREAS[i % AREAS.length]}/${KINDS[Math.floor(i / AREAS.length) % KINDS.length]}_${
    String(i).padStart(3, '0')
  }.py`,
  status: i % 7 === 0 ? 'A' : i % 5 === 0 ? 'D' : 'M',
  staged: i % 3 === 0,
  additions: (i % 40) + 1,
  deletions: i % 17,
}))

/** The status body: the cap's own length, and `truncated` only when capped. */
const STATUS = {
  repo: true,
  repoRoot: PROJECT,
  branch: 'main',
  ahead: 0,
  behind: 0,
  ...(capped ? { truncated: true } : {}),
  files: FILES,
}

/** A tree, so the rail's All mode has rows rather than a void behind the badge. */
const TREE_PATHS = FILES.slice(0, 40).map(f => f.path)

const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const body = url.includes('/api/project/git/status')
    ? STATUS
    : url.includes('/api/project/tree')
      ? { root: PROJECT, paths: TREE_PATHS, repo: true }
      : {}
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          {/* Each surface at its own dock width, because the question is whether
              a floor reads as a floor there. */}
          <div
            style={{ height: '100vh', display: 'flex', justifyContent: 'flex-end' }}
            className="bg-bg text-text"
          >
            {surface === 'tree' ? (
              <div style={{ width: 340, height: '100%', display: 'flex', flexDirection: 'column' }}>
                <PierreWorkspaceTreeImpl projectDir={PROJECT} mode="changed" onFileOpen={() => {}} />
              </div>
            ) : (
              <FileBrowserRail projectDir={PROJECT} onFileOpen={() => {}} />
            )}
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
