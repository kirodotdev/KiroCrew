/**
 * Isolated capture entry for the Dev Fleet release-channel worktree rows.
 *
 * WHY ISOLATED: reaching /dev-fleet through the full SPA needs a live gateway
 * with real worktrees, a fetched release tag and a registered checkout — none of
 * which exist in a capture run, and a half-stubbed shell renders its prerequisite
 * gate instead of the table, which is worse evidence than none. This mounts the
 * REAL DevFleetPage against the real stylesheet and theme tokens, with `fetch`
 * stubbed at the network seam to serve the same `/fleet` payload shape the
 * backend sends. Every row state under review is decided by that payload, so the
 * code path under review — channelFor, channelNameTakenFor, the placeholder
 * branch, the tip-denominated Behind cell — executes exactly as it does against a
 * real backend; the stub replaces the backend, not the component.
 *
 * Scene + theme come from the query string: ?scene=adopted&theme=dark
 * Scenes are the row states the reviewer named as required evidence:
 *   adopted     — the lane is materialized: version badge + tip-denominated Behind
 *   at-tip      — the same row at the channel tip: ok-variant badge, dashed Behind
 *   placeholder — the lane exists but has no worktree: muted row offering Create
 *   unpublished — nothing resolvable here yet, stated as information: Create LIVE
 *   blocked     — a genuine resolver failure, in BOTH variants it can occur in:
 *                 on the placeholder (Create disabled) and on an adopted row
 *   taken       — a BRANCH checkout occupies the reserved basename: ordinary row,
 *                 no badge, behind-main count, explanation on the row that exists
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
const scene = params.get('scene') || 'adopted'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const NOW = Date.now() / 1000

const MAIN = {
  name: 'main', is_main: true, running: false, has_dist: true, behind: 0,
  branch: 'main', last_updated_at: NOW - 1800,
}

// An ordinary feature worktree, present in every scene so the release row is
// always shown next to the row class it must be distinguishable from — a Behind
// cell counted from main sitting beside one counted from the channel tip.
const FEATURE = {
  name: 'kirocrew-wt-update-freshness', is_main: false, running: false,
  has_dist: true, behind: 12, branch: 'fix/update-freshness',
  last_updated_at: NOW - 3600,
  pr: { number: 9367, state: 'OPEN', url: 'https://github.com/kirodotdev/KiroCrew/pull/9367', isDraft: false },
}

// The reserved basename as a DETACHED checkout — behind-main is large by
// construction on a release worktree, which is exactly the number the row must
// not show.
const STABLE_DETACHED = {
  name: 'release-channel-stable', is_main: false, running: false,
  has_dist: true, behind: 412, last_updated_at: NOW - 86400 * 2,
}

// The same basename as an ordinary BRANCH checkout: it keeps a branch, and its
// Behind is an ordinary behind-main figure.
const STABLE_ON_BRANCH = {
  ...STABLE_DETACHED, behind: 5, branch: 'release-channel-stable',
}

// A genuine git failure, quoted the way the backend passes it through rather
// than paraphrased: this is the class the page routes to the shared ErrorNotice
// instead of a cell or a Badge title, and the only class that blocks Create. Kept
// verbatim so the frame shows how a real one-clause git message sits in the row's
// width, which is what the short cell label plus full notice exist to handle.
const RESOLVER_ERROR =
  "git ls-remote --tags: fatal: unable to access 'https://github.com/kirodotdev/KiroCrew/': Could not resolve host: github.com"

const CHANNEL = {
  lane: 'stable',
  name: 'release-channel-stable',
  worktree: 'release-channel-stable',
  ref: 'refs/tags/v0.5.0',
  version: '0.5.0',
  tip_version: '0.5.3',
  error: null,
  at_tip: false,
  behind: 3,
  name_taken_by_branch: false,
}

const SCENES: Record<string, Record<string, unknown>> = {
  // Materialized lane. at_tip=false so the frame carries BOTH the version badge
  // and the tip-denominated Behind cell, which is the pair the Behind column
  // header now has to describe truthfully.
  adopted: {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_DETACHED, FEATURE],
    release_channel: CHANNEL,
  },
  // The steady state a lane sits in for most of its life: the tree holds the
  // channel tip. The badge flips to its ok variant and the Behind cell reads as a
  // dash rather than a count, so "nothing to do here" is legible without hovering
  // — the frame the behind-tip warn variant cannot stand in for.
  'at-tip': {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_DETACHED, FEATURE],
    release_channel: { ...CHANNEL, version: '0.5.3', ref: 'refs/tags/v0.5.3', at_tip: true, behind: 0 },
  },
  // Lane with no worktree. The placeholder is the ONLY place the feature is
  // discoverable — the design has no header control — so this frame is what
  // settles whether a muted row with a live button reads as an offer.
  placeholder: {
    base_branch: 'main',
    worktrees: [MAIN, FEATURE],
    release_channel: { ...CHANNEL, worktree: null, at_tip: null, behind: null },
  },
  // Nothing resolvable here yet — a shallow clone, or a fork before its first
  // release. Benign and documented, so it reads as information and Create stays
  // LIVE: Create fetches first, which is the action that resolves the state. The
  // version pill falls back to the lane name because no ref resolved.
  unpublished: {
    base_branch: 'main',
    worktrees: [MAIN, FEATURE],
    release_channel: {
      ...CHANNEL, worktree: null, version: null, ref: null, tip_version: null,
      at_tip: null, behind: null, unpublished: true,
    },
  },
  // The failure variant of the SAME placeholder row: git could not be read, so
  // Create is disabled and the full message goes to the row's ErrorNotice while
  // the cell keeps a short label it can ellipsise without cutting a clause.
  'blocked-placeholder': {
    base_branch: 'main',
    worktrees: [MAIN, FEATURE],
    release_channel: {
      ...CHANNEL, worktree: null, version: null, ref: null, tip_version: null,
      at_tip: null, behind: null, error: RESOLVER_ERROR,
    },
  },
  // The second variant, which the placeholder frame cannot show: an ADOPTED row
  // whose resolve() failed. The tree still holds a known release so the badge
  // stays, but the tip is unknown — the row keeps its own facts and the failure
  // routes to the same ErrorNotice rather than a Badge title.
  'blocked-adopted': {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_DETACHED, FEATURE],
    release_channel: { ...CHANNEL, tip_version: null, at_tip: null, behind: null, error: RESOLVER_ERROR },
  },
  // Reserved basename occupied by a branch checkout: NOT adopted. One directory
  // is one row, so the explanation lands on the existing row and no second
  // placeholder row prints the same name twice.
  taken: {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_ON_BRANCH, FEATURE],
    release_channel: { ...CHANNEL, worktree: null, at_tip: null, behind: null, name_taken_by_branch: true },
  },
}

const FLEET = { ...(SCENES[scene] || SCENES.adopted), pods_available: true }

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
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
