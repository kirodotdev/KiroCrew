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
 * branch, the n/a Behind cell — executes exactly as it does against a real
 * backend; the stub replaces the backend, not the component.
 *
 * Scene + theme come from the query string: ?scene=adopted&theme=dark
 * Scenes are the row states the reviewer named as required evidence:
 *   adopted     — the lane is materialized: warn-variant version badge whose
 *                 TEXT names the newer release (`0.5.0 · latest 0.5.3`); Behind
 *                 is the n/a marker
 *   at-tip      — the same row at the channel tip: ok-variant badge; Behind is
 *                 the same n/a marker (never a dash, which means up to date
 *                 with main on every other row)
 *   no-release  — the lane tree is detached at NO release tag: "not on a
 *                 release" badge whose tooltip names the tip; Behind is the
 *                 n/a marker
 *   placeholder — the lane exists but has no worktree: muted row offering Create,
 *                 its status line naming the version Create checks out
 *   unpublished — nothing resolvable here yet, stated as information: Create LIVE
 *   blocked     — a genuine resolver failure, in BOTH variants it can occur in:
 *                 on the placeholder (Create disabled) and on an adopted row
 *   taken       — a BRANCH checkout occupies the reserved basename: ordinary row,
 *                 no version badge and no warn pill; in the name cell a muted
 *                 status SENTENCE (the placeholder row's own status slot) says the
 *                 checkout is on a branch, that this is why the stable release
 *                 cannot use the name, and that renaming or removing frees it;
 *                 behind-main count, explanation on the row that exists
 *   unreadable  — the reserved directory exists but its HEAD could not be read:
 *                 neither adopted nor on a branch, so it is an ordinary row with
 *                 NO version badge, NO Create and NO placeholder row (one directory,
 *                 one row) — the backend's error reaches the row's ErrorNotice
 *   menu-open   — the runner's scene, not a payload: it loads `adopted` and opens
 *                 the release row's "..." menu to show Rebase onto main is absent
 *
 * Create's OUTCOMES layer on the placeholder scene: ?scene=placeholder&create=<mode>
 * The click is the one thing the payload cannot decide — it is a POST and a
 * re-render — so the stub answers `/release-channel/create` per mode and the
 * runner performs the click before shooting. The handler under review is the
 * page's own: rcBusy, notify(), setReleaseChannelCreateError(), invalidateFleet().
 *   create=busy — the POST never settles: the button is disabled and reads
 *                 "Creating…" for as long as the frame is open
 *   create=ok   — the backend adopts the lane: the success toast names the release
 *                 and the next control ("next: Provision"); the /fleet refetch that
 *                 follows serves a freshly-materialized AT-TIP row with has_dist
 *                 false, so under the toast the placeholder has become the real row
 *                 AND the Provision button the toast points at is on it. The
 *                 create=* scenes resolve at the tip (0.5.3): a placeholder behind
 *                 the tip would create a row already wearing `latest`, and the
 *                 adopted payload's has_dist:true had left "next: Provision"
 *                 pointing at no button
 *   create=fail — the backend passes git's refusal through: the row keeps its
 *                 placeholder shape, Create is live again, and the failure lands
 *                 in a row-scoped ErrorNotice with Dismiss and the agent hand-off
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
// cell counted from main (`↓12`) sitting above one that says the column does
// not apply.
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
  name_taken_by_branch: false,
}

const SCENES: Record<string, Record<string, unknown>> = {
  // Materialized lane. at_tip=false so the badge takes its warn variant and its
  // TEXT names the newer release (`0.5.0 · latest 0.5.3`) — currency is legible
  // without colour or hover. The payload carries no lane distance; the Behind cell is n/a.
  adopted: {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_DETACHED, FEATURE],
    release_channel: CHANNEL,
  },
  // The steady state a lane sits in for most of its life: the tree holds the
  // channel tip. The badge flips to its ok variant, and that variant is what says
  // "nothing to do here" — the Behind cell is the same n/a marker as in every
  // other channel state, so a dash there can never be mistaken for "up to date
  // with main". The frame the behind-tip warn variant cannot stand in for.
  'at-tip': {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_DETACHED, FEATURE],
    release_channel: { ...CHANNEL, version: '0.5.3', ref: 'refs/tags/v0.5.3', at_tip: true },
  },
  // Adoption is by SHAPE (detached), not by being at a release, so an operator who
  // checked out an arbitrary commit in the lane holds NO release. The badge must
  // neither borrow the tip's version nor fall back to the lane word — `stable` on
  // a row already named `release-channel-stable` reads as a version — so it reads
  // "not on a release" — the TREE's condition, so it is not read as the same
  // worry as a failed lookup — with the tip named only in its tooltip.
  // `version: null` is the whole scene; the tip is still resolved and Behind
  // still renders the n/a marker.
  'no-release': {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_DETACHED, FEATURE],
    release_channel: { ...CHANNEL, version: null, ref: 'refs/tags/v0.5.3', at_tip: false },
  },
  // Lane with no worktree. The placeholder is the ONLY place the feature is
  // discoverable — the design has no header control — so this frame is what
  // settles whether a muted row with a live button reads as an offer.
  placeholder: {
    base_branch: 'main',
    worktrees: [MAIN, FEATURE],
    release_channel: { ...CHANNEL, worktree: null, at_tip: null },
  },
  // Nothing resolvable here yet — a shallow clone, or a fork before its first
  // release. Benign and documented, so it reads as information and Create stays
  // LIVE: Create fetches first, which is the action that resolves the state. No
  // version pill renders because no ref resolved — the status line says it, and
  // never the lane name.
  unpublished: {
    base_branch: 'main',
    worktrees: [MAIN, FEATURE],
    release_channel: {
      ...CHANNEL, worktree: null, version: null, ref: null, tip_version: null,
      at_tip: null, unpublished: true,
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
      at_tip: null, error: RESOLVER_ERROR,
    },
  },
  // The second variant, which the placeholder frame cannot show: an ADOPTED row
  // whose resolve() failed. The tree still holds a known release so the badge
  // stays, but the tip is unknown — the row keeps its own facts and the failure
  // routes to the same ErrorNotice rather than a Badge title.
  'blocked-adopted': {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_DETACHED, FEATURE],
    release_channel: { ...CHANNEL, tip_version: null, at_tip: null, error: RESOLVER_ERROR },
  },
  // Reserved basename occupied by a branch checkout: NOT adopted. One directory
  // is one row, so the explanation lands on the existing row and no second
  // placeholder row prints the same name twice.
  taken: {
    base_branch: 'main',
    worktrees: [MAIN, STABLE_ON_BRANCH, FEATURE],
    release_channel: { ...CHANNEL, worktree: null, at_tip: null, name_taken_by_branch: true },
  },
  // The THIRD classification: the reserved directory is in the fleet (no branch,
  // so not `taken`) but `git rev-parse HEAD` failed inside it, so the backend
  // could classify it as neither adopted nor branch-occupied — `worktree: null`,
  // `at_tip: null`, `name_taken_by_branch: false`, and the git failure in `error`.
  // The page keys the placeholder off the NAME being present in the unfiltered
  // fleet, so this renders as one ordinary row with the error under it, never a
  // second Create row for a directory that exists. The error text is the shape
  // git actually emits for a worktree whose gitdir pointer is dangling.
  unreadable: {
    base_branch: 'main',
    worktrees: [MAIN, { ...STABLE_DETACHED, behind: 0, last_updated_at: NOW - 3600 }, FEATURE],
    release_channel: {
      ...CHANNEL, worktree: null, at_tip: null, name_taken_by_branch: false,
      error: 'git rev-parse HEAD: fatal: not a git repository: ../KiroCrew/.git/worktrees/release-channel-stable',
    },
  },
}

// What the create=* scenes start from and land on. The placeholder resolves AT the
// tip (the newest release is what Create checks out, so a fresh worktree is never
// behind), and success flips to the row that a real POST produces: the same
// directory, now detached at that tag, at_tip, and NOT yet provisioned — has_dist
// false is what puts the Provision button on the row, and that button is the
// control the toast names as "next". Reusing the ADOPTED payload here had put a
// behind-the-tip badge and has_dist:true under a toast pointing at a Provision
// button that did not exist.
const CREATE_PLACEHOLDER = {
  base_branch: 'main',
  worktrees: [MAIN, FEATURE],
  release_channel: { ...CHANNEL, worktree: null, at_tip: null, version: '0.5.3', ref: 'refs/tags/v0.5.3' },
}
const CREATE_MATERIALIZED = {
  base_branch: 'main',
  worktrees: [MAIN, { ...STABLE_DETACHED, has_dist: false }, FEATURE],
  release_channel: { ...CHANNEL, version: '0.5.3', ref: 'refs/tags/v0.5.3', at_tip: true },
}

// Mutable on purpose: a successful Create is followed by a /fleet refetch, and the
// MATERIALIZED payload is what that refetch must serve for the placeholder to turn
// into the row the toast is describing. A create=* run starts from the at-tip
// placeholder whatever `scene` says, so the three outcome frames share one origin.
const create = params.get('create')
let FLEET = { ...(create ? CREATE_PLACEHOLDER : SCENES[scene] || SCENES.adopted), pods_available: true }

// What the backend's `/release-channel/create` answers, by mode. The fail text is a
// real git refusal quoted verbatim — the class the handler prefixes with its own
// "Could not create…" clause — so the frame shows how a one-clause git message sits
// in the row-scoped notice, which is what the notice exists to hold.
const CREATE_OK = { ok: true, lane: 'stable', version: '0.5.3', ref: 'refs/tags/v0.5.3' }
const CREATE_FAIL = {
  ok: false,
  error: "git worktree add: fatal: 'release-channel-stable' is a missing but already registered worktree; use 'add -f' to override, or 'prune' or 'remove' to clear",
}

const json = (body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  // Before the `/api/` catch-all: the page's own base is `/apps/dev-fleet/api`, so
  // the catch-all would otherwise answer this POST with `{}` — neither ok nor an
  // error — and the harness would be photographing a state the backend never sends.
  if (url.includes('/release-channel/create')) {
    if (create === 'busy') return new Promise<Response>(() => {})
    if (create === 'ok') {
      FLEET = { ...CREATE_MATERIALIZED, pods_available: true }
      return json(CREATE_OK)
    }
    if (create === 'fail') return json(CREATE_FAIL)
  }
  if (url.includes('/fleet')) {
    return json(FLEET)
  }
  if (url.includes('/disk')) {
    return json({ total_mb: 51200 })
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
