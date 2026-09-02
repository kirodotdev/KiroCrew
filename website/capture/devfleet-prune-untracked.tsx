/**
 * Isolated capture entry for Dev Fleet's untracked-scratch cleanup affordances.
 *
 * WHY ISOLATED: both surfaces under review only exist mid-interaction against a
 * live gateway with real worktrees whose git state is precisely staged — a
 * scratch-only tree next to a tracked-dirty one next to an unreadable one. That
 * combination cannot be conjured against a real backend without corrupting it.
 * This mounts the REAL DevFleetPage against the real stylesheet and theme
 * tokens, with `fetch` stubbed at the network seam to serve the same
 * `/fleet`, `/prune-candidates`, and `/worktree?name=` payload shapes the
 * backend sends. The driver then clicks the same buttons a user does, so the
 * prune-preview classifier, the per-row checkbox enable/disable logic, and the
 * remove-confirm sentence composition all execute exactly as in production; the
 * stub replaces the backend, not the component.
 *
 * Scene + theme come from the query string: ?scene=prune&theme=dark
 *   prune  — the prune review dialog: a KEPT section whose three rows separate
 *            a removable scratch-only tree (checkbox ENABLED, filenames inline)
 *            from a tracked-dirty tree and an unreadable tree (both DISABLED),
 *            plus a REMOVE candidate so the dialog reads realistically.
 *   remove — the single-row remove confirm for a tree that has BOTH unmerged
 *            commits AND untracked scratch, proving the unmerged-work warning
 *            and the "Also discards untracked files" line COMPOSE in one frame.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import DevFleetPage from '../src/pages/DevFleetPage'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'prune'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NOW = Date.now() / 1000

// SCENE "remove": a worktree carrying BOTH unmerged commits and untracked
// scratch. `own_commits: 2` + `real_dirty: true` sends the confirm down the
// "has unmerged work" branch (force delete), and `dirty_tracked: false` with
// `dirty_untracked: 3` appends the additive discard line — the two sentences
// the frame must show together. Not shipped, so it never takes the safe branch.
const REMOVE_INFO = {
  branch: 'feat/x',
  own_commits: 2,
  real_dirty: true,
  dirty_tracked: false,
  dirty_untracked: 3,
  dirty_untracked_paths: ['a.py', 'b.md', 'c.mjs'],
  shipped: false,
}

// The fleet payload. For "remove" a single non-main row expands into the detail
// panel whose Remove button opens the confirm; for "prune" the rows are only
// backdrop — the dialog is fed by /prune-candidates below.
const FLEET = {
  base_branch: 'main',
  worktrees: [
    { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main', last_updated_at: NOW - 1800 },
    {
      name: scene === 'remove' ? 'wt-both-hazards' : 'wt-brief-notes',
      is_main: false, running: false, has_dist: false, has_venv: true, behind: 2,
      branch: 'feat/x', last_updated_at: NOW - 3600,
    },
  ],
  pods_available: true,
}

// The prune preview. KEPT carries the three rows the PR distinguishes:
//   scratch-only  — checkbox ENABLED, right label shows the filenames inline
//   tracked-dirty — checkbox DISABLED (unfinished work the override must not eat)
//   unverifiable  — checkbox DISABLED (git status could not be read)
// A REMOVE candidate makes the dialog look like a real cleanup.
const PRUNE_CANDIDATES = {
  ok: true,
  scanned: 4,
  candidates: [
    // `merged` is the real verdict the backend emits for a shipped, clean
    // candidate. Do not invent codes here: `pruneVerdictLabel` falls through to
    // rendering an unknown code VERBATIM, so a made-up one puts a raw string in
    // the frame and makes the UI look like it leaks internal identifiers.
    { name: 'wt-pr-4210-merged', code: 'merged' },
  ],
  kept: [
    {
      name: 'wt-brief-notes', code: 'merged_dirty', dirty: true,
      dirty_tracked: false, dirty_untracked: 3,
      dirty_untracked_paths: ['frontend-brief-p0.md', 'frontend-brief-console.md', 'probe_render.py'],
    },
    { name: 'wt-session-lock', code: 'active', dirty: true, dirty_tracked: true, dirty_untracked: 0 },
    { name: 'wt-unreadable', code: 'dirty_check_failed', dirty: true },
  ],
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/prune-candidates')) {
    return Promise.resolve(new Response(JSON.stringify(PRUNE_CANDIDATES), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/worktree?name=')) {
    return Promise.resolve(new Response(JSON.stringify(REMOVE_INFO), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/fleet')) {
    return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200, headers: { 'Content-Type': 'application/json' } }))
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
