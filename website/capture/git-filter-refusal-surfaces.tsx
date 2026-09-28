/**
 * Isolated capture entry for the filter-driver REFUSAL on the two surfaces
 * outside the Git panel.
 *
 * WHY THIS EXISTS: making `/api/project/git/status` refuse for real means a
 * repository whose own config declares a `filter.*.clean` driver, which the
 * capture host does not have and should not be given. Stubbing the endpoint at
 * the fetch boundary with the body the handler actually returns -- code plus
 * `cause` -- makes the frame deterministic, and a drift in that contract shows
 * up here as a broken frame.
 *
 * WHY ISOLATED rather than a state in the dashboard harness: these two notices
 * live in the chat side panel and the Pierre Changed tree, which a screenshot
 * of the dashboard reaches only by driving several unrelated toggles. The
 * subject is how each notice READS at its own width -- the rail is 300-520px and
 * the refusal copy is two sentences, and the tree's notice is
 * `whitespace-normal` -- so each is mounted at that width.
 *
 * WHAT IS FAITHFUL: the REAL `FileBrowserRail`, the REAL
 * `PierreWorkspaceTreeImpl`, the real theme provider and the real i18n
 * catalogs. Nothing about the notices is mocked; only the endpoint is.
 *
 * `?cause=unreadable` answers the other refusal cause, which carries the
 * sentence that promises nothing about permanence.
 *
 * Query string: ?theme=dark|light&surface=rail|tree&cause=declared|unreadable
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
const cause = params.get('cause') === 'unreadable' ? 'unreadable' : 'declared'

// `ThemeProvider` is the authority: it reads `mc-theme` and applies the palette
// itself, so setting `data-theme` alone is clobbered on mount.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT = '/home/dev/acme-api'

/** The handler's own 503 body for a filter-driver refusal, both causes. */
const REFUSAL = {
  declared: {
    error:
      'Checks are off for this repository: its Git config declares a filter '
      + 'driver, so they are refused by policy.',
    code: 'git_status_filter_refused',
    cause: 'declared',
  },
  unreadable: {
    error:
      'Checks are off for this repository: its Git config could not be read, '
      + 'so they are refused by policy.',
    code: 'git_status_filter_refused',
    cause: 'unreadable',
  },
}

/** A tree, so the rail's All mode has rows behind the notice rather than a void. */
const TREE_PATHS = ['README.md', 'src/limits.py', 'src/server.py', 'tests/test_limits.py']

const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/api/project/git/status')) {
    return Promise.resolve(new Response(JSON.stringify(REFUSAL[cause]), {
      status: 503, headers: { 'Content-Type': 'application/json' },
    }))
  }
  const body = url.includes('/api/project/tree')
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
              the two-sentence refusal copy reads there. */}
          <div
            style={{ height: '100vh', display: 'flex', justifyContent: 'flex-end' }}
            className="bg-bg text-text"
          >
            {surface === 'tree' ? (
              <div style={{ width: 340, height: '100%' }}>
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
