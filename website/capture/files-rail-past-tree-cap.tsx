/**
 * Isolated capture entry for the Files rail on a project past the tree's
 * 10,000-row cap.
 *
 * WHY ISOLATED: the subject is a project with more than 10,000 items, which a
 * live gateway reproduces only by seeding one. Stubbing `/api/project/tree`
 * (whole and `?dir=`) and `/api/file-search` at the fetch boundary makes the
 * frames deterministic, and each payload is the endpoint's own response shape.
 *
 * WHAT IS FAITHFUL: the REAL `FileBrowserRail`, the real Pierre tree, the real
 * theme provider and the real i18n catalogs. The driver types, clicks and
 * expands; nothing is seeded into component state.
 *
 * Query string: ?theme=dark|light&arm=ok|dir-gone|dir-refused|dir-fail|search-pending|search-fail
 *   dir-gone        `?dir=packages` answers 404 `dir_not_found`: the folder is gone, and
 *                   the whole listing read after it no longer names it
 *   dir-refused     `?dir=packages` answers 403 `dir_refused`: a link or a protected folder
 *   dir-fail        `?dir=packages` answers a coded 503: the failed folder listing
 *   search-pending  `/api/file-search` never answers: the search in flight
 *   search-fail     `/api/file-search` answers 403: the tree's own matches stay
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import FileBrowserRail from '../src/pages/chat/FileBrowserRail'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const arm = params.get('arm') || 'ok'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT = '/home/dev/monorepo'

/** The whole listing, cut: `packages` lost rows to the cap, and `huge-assets`
 *  is cut even inside its own listing (a single folder over the cap). */
const WHOLE = {
  root: PROJECT,
  paths: ['README.md', 'package.json', 'packages/core/index.ts', 'packages/core/util.ts'],
  directories: ['packages', 'packages/core', 'huge-assets'],
  repo: false,
  truncated: true,
  truncatedDirectories: ['packages', 'huge-assets'],
  hiddenOnlyDirectories: [],
  unreadableDirectories: [],
  linkedDirectories: [],
}

/** The whole listing once `packages` is deleted (the `dir-gone` arm, after its 404). */
const WHOLE_WITHOUT_PACKAGES = {
  ...WHOLE,
  paths: ['README.md', 'package.json'],
  directories: ['huge-assets'],
  truncatedDirectories: ['huge-assets'],
}
let packagesGone = false

/** `?dir=packages`: the folders the cap hid, now listed. */
const PACKAGES = {
  root: PROJECT,
  dir: 'packages',
  paths: ['packages/core/index.ts', 'packages/zz-billing/invoice.ts', 'packages/zz-billing/tax.ts', 'packages/zz-search/query.ts'],
  directories: ['packages/core', 'packages/zz-billing', 'packages/zz-search'],
  repo: false,
  truncated: false,
  truncatedDirectories: [],
  hiddenOnlyDirectories: [],
  unreadableDirectories: [],
  linkedDirectories: [],
}

/** `?dir=huge-assets`: one folder over the cap on its own, still cut. */
const HUGE = {
  root: PROJECT,
  dir: 'huge-assets',
  paths: [],
  directories: [],
  repo: false,
  truncated: true,
  truncatedDirectories: ['huge-assets'],
  hiddenOnlyDirectories: [],
  unreadableDirectories: [],
  linkedDirectories: [],
}

/** `/api/file-search` for "billing", project-wide: a folder past the cap and its files. */
const SEARCH = {
  root: PROJECT,
  results: [
    { path: `${PROJECT}/packages/zz-billing`, name: 'zz-billing', size: 0, mtime: 0, kind: 'dir' },
    { path: `${PROJECT}/packages/zz-billing/invoice.ts`, name: 'invoice.ts', size: 812, mtime: 0, kind: 'file' },
    { path: `${PROJECT}/packages/zz-billing/tax.ts`, name: 'tax.ts', size: 401, mtime: 0, kind: 'file' },
  ],
}


const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), {
  status: 200, headers: { 'Content-Type': 'application/json' },
}))
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!raw.includes('/api/')) return realFetch(input, init)
  const url = new URL(raw, location.origin)
  if (url.pathname === '/api/project/tree') {
    const dir = url.searchParams.get('dir')
    if (dir === 'packages' && arm === 'dir-gone') {
      packagesGone = true
      return Promise.resolve(new Response(JSON.stringify({ error: 'Folder not found', code: 'dir_not_found' }), {
        status: 404, headers: { 'Content-Type': 'application/json' },
      }))
    }
    if (dir === 'packages' && arm === 'dir-refused') {
      return Promise.resolve(new Response(JSON.stringify({ error: 'Folder cannot be listed', code: 'dir_refused' }), {
        status: 403, headers: { 'Content-Type': 'application/json' },
      }))
    }
    if (dir === 'packages' && arm === 'dir-fail') {
      return Promise.resolve(new Response(JSON.stringify({ error: 'the file index is busy', code: 'probe_busy' }), {
        status: 503, headers: { 'Content-Type': 'application/json' },
      }))
    }
    return json(dir === 'packages' ? PACKAGES : dir === 'huge-assets' ? HUGE : packagesGone ? WHOLE_WITHOUT_PACKAGES : WHOLE)
  }
  if (url.pathname === '/api/file-search' && arm === 'search-pending') return new Promise<Response>(() => {})
  if (url.pathname === '/api/file-search' && arm === 'search-fail') {
    return Promise.resolve(new Response(JSON.stringify({ error: 'Access denied', code: 'access_denied' }), {
      status: 403, headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.pathname === '/api/file-search') {
    return json(SEARCH)
  }
  if (url.pathname === '/api/project/git/status') return json({ repo: false, files: [] })
  return json({})
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <div style={{ height: '100vh', display: 'flex', justifyContent: 'flex-end' }} className="bg-bg text-text">
            <FileBrowserRail projectDir={PROJECT} onFileOpen={() => {}} onOpenFolder={() => {}} />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
