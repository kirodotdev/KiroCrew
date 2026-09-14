/**
 * Isolated capture entry for the folder panel's failure notices.
 *
 * Isolated because the interesting states are a listing and a search that never
 * settle; booting the full SPA to reach them photographs a half-stubbed shell's
 * error boundary instead.
 *
 * The deadline must NOT be faked at the api seam. These frames exist to show that
 * a timeout renders TRANSLATED copy rather than the rejection's own message, so
 * `fetch` hangs and the production deadline raises the real rejection, which the
 * real classifier keys on. Stubbing the api method would prove only that the
 * stub's own error reached the notice. The other three causes answer with the
 * status and machine code the backend now sends, so the classifier runs for real.
 *
 * Axes: ?arm=listing|search&cause=timed_out|denied|root_missing|failed&lang=en&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// The all-languages entry, not the English-only one: a `ja` scene would otherwise
// fall back to English and photograph the opposite of what it claims.
import { initI18n } from '../src/i18n/all'
import FolderPanel from '../src/pages/chat/FolderPanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const arm = params.get('arm') || 'listing'
const cause = params.get('cause') || 'timed_out'
const lang = params.get('lang') || 'en'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT = '/Demo Workspace/Product Guide'

const LISTING = {
  path: PROJECT,
  parent: '/Demo Workspace',
  dirs: [
    { name: 'src', path: `${PROJECT}/src`, mtime: 0 },
    { name: 'docs', path: `${PROJECT}/docs`, mtime: 0 },
  ],
  files: [
    { name: 'README.md', path: `${PROJECT}/README.md`, mtime: 0 },
    { name: 'pyproject.toml', path: `${PROJECT}/pyproject.toml`, mtime: 0 },
  ],
}

/**
 * Pending until the caller's signal aborts, then rejecting with that signal's
 * reason -- which is what a real `fetch` does, and the only shape that exercises
 * the deadline. `withDeadline` does not race a rejection of its own: it aborts a
 * controller and relies on the fetch to reject, so a stub that ignored the signal
 * would hang past the deadline and photograph a spinner.
 */
const hang = (init?: RequestInit) =>
  new Promise<Response>((_resolve, reject) => {
    const signal = init?.signal
    if (!signal) return
    if (signal.aborted) return reject(signal.reason)
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })

const json = (status: number, body: unknown) =>
  Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
  )

/** `failed` carries no `code`, which is what degrades it to the generic notice. */
const REFUSALS: Record<string, () => Promise<Response>> = {
  denied: () => json(403, { error: 'Access denied', code: 'access_denied' }),
  root_missing: () => json(404, { error: 'Project root not found', code: 'project_not_found' }),
  failed: () => json(500, { error: 'Internal error' }),
}

/** The failing read for this scene; the other arm answers normally so the panel renders. */
const failing = (init?: RequestInit) =>
  cause === 'timed_out' ? hang(init) : REFUSALS[cause]()

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url

  if (url.startsWith('/api/browse-files')) {
    return arm === 'listing' ? failing(init) : json(200, LISTING)
  }
  if (url.startsWith('/api/file-search')) {
    return arm === 'search' ? failing(init) : json(200, { results: [], root: PROJECT })
  }
  // The panel also asks for the git overlay and the tree; an empty answer keeps
  // those rows out of the frame instead of letting them hit the dev server.
  if (url.startsWith('/api/project-tree')) return json(200, { root: PROJECT, paths: [], repo: false })
  if (url.startsWith('/api/project-git-status')) return json(200, { repo: false, files: [] })
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n(lang)

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <div data-capture-root style={{ width: 420, height: 340 }} className="bg-bg">
        <FolderPanel path={PROJECT} projectDir={PROJECT} onClose={() => {}} onFileOpen={() => {}} />
      </div>
    </QueryClientProvider>
  </MemoryRouter>,
)
