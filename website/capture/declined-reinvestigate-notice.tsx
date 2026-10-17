/**
 * Evidence for the declined re-investigate notice (#6270).
 *
 * THE DEFECT: when Issue Radar declines a re-investigate because the
 * investigation already concluded and its session was closed, the reason lived in
 * the button's `title` plus an `sr-only` live region. `title` surfaces on HOVER,
 * not on focus, and cannot be surfaced by touch at all — so a sighted keyboard
 * user and a touch user were told nothing, and saw only a label quietly change.
 *
 * The scene mounts the REAL pane (`IssueDetail`) inside the REAL
 * `IssueRadarProvider`, against the real stylesheet, theme tokens and the live
 * i18n catalog, with only `fetch` stubbed — the same shape as
 * `copy-link-feedback.tsx`. Nothing here re-implements the header, the button, the
 * popover, its classes or its strings, so a frame proves what ships.
 *
 * The decline is REAL, not staged: the record carries a `slot_key` and a resolved
 * status, the slot-detail probe 404s the way a closed session does, and
 * `openSession` then takes its own concluded branch. So the frame exercises the
 * exact path the PR changes rather than a hand-set prop.
 *
 *   ?scene=resting   record present, session live      -> "Resume", no notice
 *   ?scene=declined  session closed, work concluded    -> notice + Older Sessions
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import type {
  IssueAiResponse, IssueDetailResponse, Issue,
} from '../src/apps/issue-radar/api'
import IssueDetail from '../src/apps/issue-radar/components/IssueDetail'
import { IssueRadarProvider } from '../src/apps/issue-radar/context'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const declined = params.get('scene') !== 'resting'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const OWNER = 'kirodotdev'
const REPO = 'Kiro'
const NUMBER = 6270
const URL_ = `https://github.com/${OWNER}/${REPO}/issues/${NUMBER}`
const SLOT = 'dashboard_chat-12'

const ROW: Issue = {
  number: NUMBER,
  title: 'Issue Radar: a declined re-investigate explains itself only on hover',
  url: URL_,
  labels: ['enhancement', 'area: dashboard'],
  comments: 9,
  author: 'raymond',
  author_association: 'MEMBER',
  state: 'open',
  assignees: [],
  body: 'The reason a click was declined is on the button\u2019s title, so it is hover-only.',
  created_at: '2026-08-27T11:20:00Z',
  updated_at: '2026-08-28T04:46:00Z',
}

const DETAIL: IssueDetailResponse = {
  owner: OWNER,
  repo: REPO,
  number: NUMBER,
  detail: {
    number: NUMBER,
    title: ROW.title,
    body: ROW.body ?? '',
    state: 'open',
    state_reason: null,
    url: URL_,
    author: 'raymond',
    author_association: 'MEMBER',
    created_at: ROW.created_at ?? '',
    updated_at: ROW.updated_at,
    closed_at: null,
    closed_by: null,
    comments: 9,
    locked: false,
    labels: [
      { name: 'enhancement', color: 'a2eeef', description: 'New feature or request' },
      { name: 'area: dashboard', color: 'C5DEF5', description: 'Dashboard UI and its backend handlers' },
    ],
    assignees: [],
    milestone: null,
    reactions: {
      total: 0, plus1: 0, minus1: 0, laugh: 0, hooray: 0,
      confused: 0, heart: 0, rocket: 0, eyes: 0,
    },
  },
  timeline: [],
  from_cache: true,
}

const AI: IssueAiResponse = {
  owner: OWNER,
  repo: REPO,
  number: NUMBER,
  summary: 'A declined re-investigate has no visible explanation; the reason is on a hover-only title.',
  suggested_labels: [],
  generated_at: '2026-08-28T04:47:00Z',
  from_cache: true,
}

/** The concluded record. `status: 'resolved'` is what the recording tool writes,
 *  and it is the half of the decline test that says the work was FINISHED when
 *  the session was closed. */
const RECORD = {
  owner: OWNER,
  repo: REPO,
  number: NUMBER,
  investigation: {
    slot_key: SLOT,
    status: 'resolved',
    findings: {
      verdict: 'bug',
      summary: 'Reachable only on hover; a keyboard or touch user gets no explanation.',
    },
  },
}

const json = (body: unknown, status = 200) => Promise.resolve(
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
)

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  // The closed session. A 404 here is exactly what a user-closed slot answers,
  // and `isMissingSlotError` reads the status, so this is the real trigger rather
  // than a simulated one. `resting` keeps the slot alive, so the same record
  // resumes instead of declining.
  if (url.includes(`/api/chat/slots/${SLOT}`)) {
    return declined ? json({ error: 'not found' }, 404) : json({ key: SLOT, messages: [] })
  }
  if (url.includes('/investigation')) return json(RECORD)
  if (url.includes('/issue-ai')) return json(AI)
  if (url.includes('/issue?')) return json(DETAIL)
  // `DepsSection` iterates `edges`, so the catch-all's bare `{}` throws into an
  // error boundary and takes the whole pane with it. An empty, well-formed payload
  // renders no section, which is the state this scene wants anyway.
  if (url.includes('/deps')) return json({ schema: 1, edges: [], nodes: {} })
  if (url.includes('/api/')) return json(/repos|members|labels|milestones/.test(url) ? [] : {})
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

await initI18n()

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{ width: 1040, height: 620, display: 'flex', background: 'var(--bg)' }}
  >
    <Provider store={store}>
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <IssueRadarProvider
            repos={[{
              owner: OWNER,
              repo: REPO,
              provider: 'github',
              host: 'github.com',
              enabled: true,
              permissions: {
                admin: false, maintain: false, push: true, triage: true, pull: true,
              },
            }]}
            active={{ owner: OWNER, repo: REPO, provider: 'github', host: 'github.com' }}
            onSwitch={() => {}}
            onAddRepo={() => {}}
          >
            <IssueDetail issue={ROW} />
          </IssueRadarProvider>
        </QueryClientProvider>
      </MemoryRouter>
    </Provider>
  </div>,
)
