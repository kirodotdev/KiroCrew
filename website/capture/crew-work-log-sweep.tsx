/**
 * Evidence for the crew work log's crew-level line.
 *
 * WHAT CHANGED: a crew that swept its queue and took nothing can now record the
 * cycle. That line belongs to no issue, so the backend omits `number` from it
 * entirely. The Issue column is monospaced and right-aligned against real issue
 * numbers, so rendering the absent value straight would print a bare `#` and
 * read as a number that failed to load; it renders an em dash instead, and the
 * OUTCOME badge carries the new `sweep` kind.
 *
 * WHY A HARNESS: the work log lives inside `CrewPageView`, which needs the whole
 * Issue Radar context and a live gateway to reach. The page's only input is
 * `GET /api/apps/issue-radar/crew`, so stubbing that one response renders the
 * REAL view -- real classes, real Tailwind output, real theme tokens, real
 * catalog -- with nothing re-implemented here. The frame therefore shows what
 * ships, including the numbered rows beside the crew-level one, which is the
 * comparison the change is about.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import type { CrewDetailResponse } from '../src/apps/issue-radar/api'
import CrewPageView from '../src/apps/issue-radar/views/CrewPageView'
import { IssueRadarProvider } from '../src/apps/issue-radar/context'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const OWNER = 'kirodotdev'
const REPO = 'Kiro'
const CREW = 'c_1a2b3c4d'

/** An ISO stamp `hours` before now, so every row lands inside the 24h window and
 *  the Earlier divider stays out of the frame. */
const ago = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString()

const PAYLOAD: CrewDetailResponse = {
  crew: {
    schema: 1,
    id: CREW,
    name: 'Andromeda',
    avatar_seed: CREW,
    avatar_variant: 2,
    agent: 'kirocrew',
    model: 'claude-opus-5',
    extra_prompt: '',
    labels: ['area: apps'],
    auto_resolve_conflicts: false,
    auto_merge: false,
    unattended: true,
    max_open: 3,
    worktree_root: '',
    slot_key: `crew-${CREW}`,
    enabled: true,
    paused_reason: '',
    created_at: ago(72),
    retired_at: null,
  },
  items: [
    {
      schema: 1, crew_id: CREW, owner: OWNER, repo: REPO, number: 2251,
      phase: 'awaiting-ci', outcome: null, decision: '', why: '',
      next: 'Wait out CI round 3, then re-read the two inherited reds before rebasing.',
      tried: [], worktree: '', branch: 'fix/safe-chmod-windows', base_sha: '',
      pr_number: 2288, ci_state: { passed: 41, total: 47, round: 3 },
      claim_comment_id: null, labels_applied: [],
      claimed_at: ago(30), last_progress_at: ago(1), finished_at: null,
    },
  ],
  events: [
    // The crew-level line: no `number` key at all, which is what the Issue cell
    // has to render honestly. It is also the NEWEST line, so its stretch is still
    // open and its WHEN column reads as ongoing.
    {
      id: 'ev-sweep', ts: ago(0.4), crew_id: CREW, kind: 'sweep',
      text: 'checked 42 open issues, took none -- every candidate is claimed or skipped',
    },
    // Numbered siblings, unchanged, so the frame shows the contrast rather than
    // one row in isolation.
    { id: 'ev-ci', ts: ago(1), crew_id: CREW, number: 2251, kind: 'ci', text: 'CI round 3 -- 41/47 green, 2 inherited from main' },
    { id: 'ev-impl', ts: ago(3), crew_id: CREW, number: 2251, kind: 'implement', text: 'added the Windows branch to _safe_chmod' },
    { id: 'ev-claim', ts: ago(6), crew_id: CREW, number: 2251, kind: 'claim', text: 'took it -- regression test already fails on main' },
    // A SECOND crew-level line, older than that work: this stretch has ENDED, so
    // the same kind must render as a plain past instant. Both cases in one frame,
    // because the qualifier is keyed on recency and a frame showing only the open
    // stretch could not tell the two apart.
    {
      id: 'ev-sweep-closed', ts: ago(9), crew_id: CREW, kind: 'sweep',
      text: 'checked 39 open issues, took none',
    },
  ],
  counts: { open: 1 },
}

// Only the crew endpoint is stubbed; anything else falls through so a missed
// dependency surfaces as a real network error instead of an empty render.
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/apps/issue-radar/crew?')) {
    return Promise.resolve(new Response(JSON.stringify(PAYLOAD), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const active = { owner: OWNER, repo: REPO, provider: 'github' as const, host: 'github.com' }

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Provider store={store}>
      <MemoryRouter>
        <IssueRadarProvider
          repos={[{ ...active, permissions: null }] as never}
          active={active as never}
          onSwitch={() => {}}
          onAddRepo={() => {}}
        >
          <div data-testid="capture-frame" className="inline-block bg-bg p-6 text-text">
            <CrewPageView crewId={CREW} />
          </div>
        </IssueRadarProvider>
      </MemoryRouter>
    </Provider>
  </QueryClientProvider>,
)
