/**
 * Isolated capture entry for the Files rail's Name/Content search toggle.
 *
 * WHY ISOLATED: the subject is what the rail renders for a CONTENT search over a
 * project that contains both code and documents. Producing that against a live
 * gateway means seeding a repo with a .pptx and an .xlsx whose text happens to
 * match — reproducible only by accident. Stubbing `/api/file-grep` makes the
 * frame deterministic, and the payload is exactly the endpoint's own response
 * shape, so a drift in that contract shows up here as a broken frame.
 *
 * WHAT IS FAITHFUL: the REAL `FileBrowserRail`, the real Pierre tree, the real
 * theme provider and the real i18n catalogs. The mode toggle is CLICKED by the
 * driver rather than seeded, so the frame shows the state a user reaches.
 *
 * `?arm=capped` answers with a truncated, python-engine, documents-skipped
 * payload, which is the status line's other arm — the one that says the search
 * stopped rather than that the text is absent.
 *
 * Query string: ?theme=dark&arm=ok|capped|empty|error
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank and the screenshot misrepresents the real UI.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import FileBrowserRail from '../src/pages/chat/FileBrowserRail'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
// `arm` selects which answer the stub gives; the driver passes one of
// ok|capped|empty|error and anything else falls back to the ok arm.
const arm = params.get('arm') || 'ok'

// `ThemeProvider` is the authority: it reads `mc-theme` and applies the palette
// itself, so setting `data-theme` alone is clobbered on mount (an unset
// preference resolves to `system`, which is LIGHT in headless Chromium).
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT = '/home/dev/acme-api'

/** A tree with the files the search hits, so Name mode is a real frame too. */
const TREE_PATHS = [
  'README.md',
  'docs/architecture.pptx',
  'docs/budget.xlsx',
  'src/limits.py',
  'src/server.py',
  'tests/test_limits.py',
]

/**
 * Exactly `/api/file-grep`'s response shape: four text hits and three document
 * hits, each document naming the place inside itself that matched.
 *
 * Every preview contains the QUERY VERBATIM, because it has to: the backend
 * matches a literal, so a payload whose preview does not contain "rate limit" is
 * one the endpoint could never return — and the row would then render with no
 * highlight, which reads as "a match means something different here".
 */
const GREP_OK = {
  results: [
    { file: `${PROJECT}/src/limits.py`, line: 42, preview: 'DEFAULT_RATE_LIMIT = 600  # the rate limit, per API key' },
    { file: `${PROJECT}/src/server.py`, line: 118, preview: '    raise RateLimitExceeded("rate limit reached", retry_after=delay)' },
    { file: `${PROJECT}/tests/test_limits.py`, line: 7, preview: '    """the rate limit is counted per API key, not per IP."""' },
    { file: `${PROJECT}/README.md`, line: 64, preview: '- Rate limit: 600 requests per minute per API key.' },
    { file: `${PROJECT}/docs/architecture.pptx`, line: 0, preview: 'The edge rate limit lives in the gateway, not the service', label: 'slide 7' },
    { file: `${PROJECT}/docs/budget.xlsx`, line: 0, preview: 'rate limit overage\t$1,240\tQ3', label: 'Costs · row 12' },
    // A Word hit, which carries NO tag: a .docx has no location inside it to name.
    // On screen so the tag-less row and the note that explains it are both tested.
    { file: `${PROJECT}/docs/decision.docx`, line: 0, preview: 'we set the rate limit per API key', label: null },
  ],
  truncated: false,
  engine: 'rg',
  skipped_docs: 0,
  root: PROJECT,
}

const GREP_CAPPED = {
  ...GREP_OK,
  results: GREP_OK.results.slice(0, 4),
  truncated: true,
  engine: 'python',
  skipped_docs: 6,
}

/** A search that ran and found nothing. Distinct from the hint state (query too
 *  short) and from a failure: without a frame each, all three review alike,
 *  because none of them had a frame. */
const GREP_EMPTY = { results: [], truncated: false, engine: 'rg', skipped_docs: 0, root: PROJECT }

/** Fetch stub at the API boundary, so nothing waits on a gateway that is not
 *  here. Only the three reads this rail makes are answered specifically. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  // The error arm answers the coded 503 the probe pool really returns, so the
  // frame shows the rail's own failure rendering rather than a mocked-up red box.
  if (url.includes('/api/file-grep') && arm === 'error') {
    return Promise.resolve(new Response(
      JSON.stringify({ error: 'the file index is busy', code: 'probe_busy' }),
      { status: 503, headers: { 'Content-Type': 'application/json' } },
    ))
  }
  const body = url.includes('/api/file-grep')
    ? (arm === 'capped' ? GREP_CAPPED : arm === 'empty' ? GREP_EMPTY : GREP_OK)
    : url.includes('/api/project/tree')
      ? { root: PROJECT, paths: TREE_PATHS, repo: true }
      : url.includes('/api/project/git/status')
        ? { repo: true, files: [{ path: 'src/limits.py', status: 'M', staged: false }] }
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
          {/* The rail's own dock width, so the capture IS the rail rather than a
              sliver of a mostly-empty page. */}
          <div style={{ height: '100vh', display: 'flex', justifyContent: 'flex-end' }} className="bg-bg text-text">
            <FileBrowserRail projectDir={PROJECT} onFileOpen={() => {}} />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
