/**
 * Isolated capture entry for Dev Fleet's "Create worktree from latest stable" action,
 * an item in the toolbar's More actions menu.
 *
 * WHY ISOLATED: the action's live states — the menu, in-flight, the success toast
 * and a refusal — only exist mid-interaction against a live gateway whose remote
 * actually advertises (or does not advertise) a stable tag, which cannot be staged
 * against a real backend without a real fork + credential. This mounts the REAL
 * DevFleetPage against the real stylesheet and theme tokens with `fetch` stubbed at
 * the network seam, so the menu, the item's own `releasing` state, the `notify()`
 * toast and the ApiError-body unwrap all execute exactly as in production; the stub
 * replaces the backend, not the component. Fixture data only — no real path, user,
 * host or token appears.
 *
 * theme + create come from the query string: ?theme=dark&create=<mode>
 *   (no create) — the resting toolbar: the item enabled in the menu
 *   busy        — the POST never resolves, holding the "Creating stable worktree…" state
 *   ok          — the POST returns {ok:true}, so the success toast fires, and the
 *                 fleet re-read after `invalidateAll()` carries the new row
 *   fail        — the POST returns 409 {ok:false, error}, so the refusal banner shows
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import DevFleetPage from '../src/apps/dev-fleet/DevFleetPage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const create = params.get('create') || ''
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NOW = Date.now() / 1000

// A populated fleet so the toolbar (and its menu) render: a main checkout plus
// one ordinary feature worktree. Fixture names only; no real path is shown at the
// toolbar level under capture, and none is placed here.
const FLEET = {
  base_branch: 'main',
  worktrees: [
    { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', last_updated_at: NOW - 1800 },
    {
      name: 'wt-example-feature', is_main: false, running: false, has_dist: false,
      has_venv: true, behind: 2, branch: 'feat/example', last_updated_at: NOW - 3600,
    },
  ],
  pods_available: true,
}

// The row the create adds, served by every fleet read after a successful POST, so the
// success shot shows the table's re-read beside the toast.
const RELEASE_ROW = {
  name: 'release-channel-stable', is_main: false, running: false, has_dist: false,
  has_venv: false, behind: 0, branch: '', last_updated_at: NOW,
}
let created = false

// The create POST's answer, keyed by the scene. `busy` never resolves, so the
// item stays in its disabled "Creating stable worktree…" state for the shot.
// `ok` returns the result shape the gateway route sends; the success toast names
// it. `fail` returns the 409 refusal the route sends when the remote has no
// stable tag — the client throws ApiError, the handler unwraps its body's
// `error`, and the refusal toast carries that exact server message.
function createResponse(): Promise<Response> {
  if (create === 'busy') return new Promise<Response>(() => {})
  if (create === 'fail') {
    return Promise.resolve(new Response(
      JSON.stringify({ ok: false, code: 'unpublished', error: 'No stable release found on the remote yet. Nothing was created.' }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    ))
  }
  created = true
  return Promise.resolve(new Response(
    JSON.stringify({ ok: true, name: 'release-channel-stable', version: '0.10.0', ref: 'refs/tags/v0.10.0' }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  ))
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/create-release-worktree')) return createResponse()
  if (url.includes('/fleet')) {
    const fleet = created ? { ...FLEET, worktrees: [...FLEET.worktrees, RELEASE_ROW] } : FLEET
    return Promise.resolve(new Response(JSON.stringify(fleet), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/disk')) {
    return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dev-fleet']}>
        <div style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
          <DevFleetPage />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
