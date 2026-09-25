/**
 * Isolated capture entry for the artifact detail page's SYNC-BANNER ABORT state
 * (#7818): the dirty editor buffer is flushed before a sync action, the flush is
 * refused with 409 because the content changed since it was loaded, and the
 * page stops the action instead of pushing the stale buffer over the newer
 * content.
 *
 * WHY ISOLATED: the sync banner renders only when a publish provider is
 * registered, which the public edition's empty registry never is, and the 409
 * needs a second writer. `window.fetch` is replaced with a stub that answers
 * exactly the endpoints the REAL ArtifactDetailPage calls, so the page's own
 * save/flush code runs and lands in its real conflict branch:
 *   - the artifact GET: an editable markdown artifact carrying a `publication`
 *     and `live_dirty: true` (local edits not yet published), token T1;
 *   - publish providers: one capable provider, so the banner mounts;
 *   - upstream-status: `upstream_ahead: false`, so the banner offers
 *     "Snapshot to publish" (the sync action whose pre-flush aborts);
 *   - versions / events / comments / folders: empty;
 *   - the content PATCH: 409 `{ error: "conflict", current_token: T2 }`, the
 *     backend's refusal of a write whose expected_token is no longer current.
 *
 * The capture script (scripts/capture-artifact-sync-conflict.mjs) presses Edit,
 * types into the editor, presses the banner's Snapshot, and asserts the abort
 * state before it shoots: the edit bar's conflict notice, the banner's inline
 * "Save refused" beside its button, the draft still in the editor, and Save
 * relabelled as the overwrite it would now be.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import ArtifactDetailPage from '../src/pages/ArtifactDetailPage'
import type { Artifact } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

// ThemeProvider re-derives the root theme from its own preference store on
// mount, so seed that store; the attribute only covers the pre-mount paint.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'cr-queue'
const TOKEN_LOADED = 'a'.repeat(64)
const TOKEN_CURRENT = 'b'.repeat(64)

const ARTIFACT: Artifact = {
  slug: SLUG,
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# CR Queue\n\nOpen reviews, newest first.\n\n- CR-1204 — token guard on artifact PATCH\n- CR-1199 — sync banner abort path\n',
  content_token: TOKEN_LOADED,
  live_dirty: true,
  publication: {
    artifact_id: 'pub-1',
    view_url: 'https://example.test/artifact/pub-1',
    provider: 'stub',
    visibility: 'PRIVATE',
    shared_with: [],
    auto_sync: false,
    last_synced_kirocrew_version: 1,
    version_map: { '1': 1 },
    published_at: '2026-05-21T22:00:00.000000+00:00',
    published_by: 'me',
    last_error: '',
    notice: '',
  },
}

/* Answer every request the page makes. Anything else gets an empty 200 so
 * unrelated loaders (theme boot, slots, popouts) settle without noise. */
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const ART = `/api/artifacts/${SLUG}`
// The capture script reads this to prove exactly ONE content PATCH went out
// (the refused flush): the stub answers in-page, so nothing crosses the
// network for the script to count there.
const counters = { patches: 0 }
;(window as unknown as { __syncConflictCounters: typeof counters }).__syncConflictCounters = counters
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = new URL(url, location.origin).pathname
  const method = (init?.method || 'GET').toUpperCase()
  if (path === ART && method === 'GET') return json(200, ARTIFACT)
  // The second writer already moved the content past TOKEN_LOADED: every
  // content write from this edit session is refused, whichever button sent it.
  if (path === ART && method === 'PATCH') {
    counters.patches += 1
    return json(409, { error: 'conflict', current_token: TOKEN_CURRENT, version: 2 })
  }
  if (path === '/api/artifacts/publish-providers') {
    return json(200, {
      kind: 'markdown',
      providers: [{ name: 'stub', display_name: 'Stub', capabilities: [], kind_support: 'native', capable: true }],
    })
  }
  if (path === `${ART}/upstream-status`) return json(200, { upstream_ahead: false })
  if (path === `${ART}/versions`) return json(200, { slug: SLUG, versions: [1] })
  if (path === `${ART}/events`) return json(200, { slug: SLUG, events: [] })
  if (path === `${ART}/comments`) return json(200, { slug: SLUG, comments: [] })
  if (path === '/api/artifact-folders') return json(200, { folders: [] })
  return json(200, {})
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[`/artifacts/${SLUG}`]}>
          <div data-capture-root style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--bg)' }}>
            <Routes>
              <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
            </Routes>
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
