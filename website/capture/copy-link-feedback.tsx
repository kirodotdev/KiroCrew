/**
 * Evidence for the Issue Radar copy-link confirmation.
 *
 * THE BUG: the overflow menu's copy row called `navigator.clipboard.writeText`
 * directly and swallowed the rejection. The async Clipboard API exists only in a
 * SECURE CONTEXT, so on a plain-http origin that call throws before anything
 * reaches the clipboard and the row changed nothing at all — no tick, no
 * warning, and no URL to paste.
 *
 * The scene mounts the REAL pane (`IssueDetail`) inside the REAL
 * `IssueRadarProvider`, against the real stylesheet, theme tokens and the live
 * i18n catalog, with only `fetch` stubbed. Nothing here re-implements the menu
 * row, its icons, its classes or its strings, so a frame proves what ships.
 *
 * The clipboard is installed per scene, since that is the axis under test:
 *
 *   ?scene=idle      menu open, row not pressed
 *   ?scene=copied    async API resolves           -> "Link copied"
 *   ?scene=fallback  API rejects, execCommand ok  -> "Link copied" (plain http)
 *   ?scene=failed    both paths dead             -> "Copy failed"
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
const raw = params.get('scene')
const scene = raw === 'copied' || raw === 'fallback' || raw === 'failed' ? raw : 'idle'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The clipboard the pressed row will meet. `copied` is a secure-context origin;
// `fallback` is the plain-http one the fix exists for (no async API, so the
// helper's textarea path is what carries the URL); `failed` has nothing left.
const clipboard = {
  copied: () => Promise.resolve(),
  fallback: () => Promise.reject(new Error('not a secure context')),
  failed: () => Promise.reject(new Error('not a secure context')),
  idle: () => Promise.resolve(),
}[scene]
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: clipboard } })
;(document as unknown as { execCommand: unknown }).execCommand = scene === 'failed'
  ? () => { throw new Error('copy unavailable') }
  : () => true

const OWNER = 'kirodotdev'
const REPO = 'Kiro'
const URL_ = `https://github.com/${OWNER}/${REPO}/issues/4438`

const ROW: Issue = {
  number: 4438,
  title: 'Issue Radar: copy link to this issue reports nothing at all',
  url: URL_,
  labels: ['bug', 'area: apps'],
  comments: 2,
  author: 'raymond',
  author_association: 'MEMBER',
  state: 'open',
  assignees: [],
  body: 'Clicking "Copy link to this issue" changes nothing on screen.',
  created_at: '2026-08-19T05:00:00Z',
  updated_at: '2026-08-19T06:00:00Z',
}

const DETAIL: IssueDetailResponse = {
  owner: OWNER,
  repo: REPO,
  number: 4438,
  detail: {
    number: 4438,
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
    comments: 2,
    locked: false,
    labels: [
      { name: 'bug', color: 'd73a4a', description: '' },
      { name: 'area: apps', color: '0e8a16', description: '' },
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
  number: 4438,
  summary: 'The copy affordance reports neither success nor failure.',
  suggested_labels: [],
  generated_at: '2026-08-19T06:01:00Z',
  from_cache: true,
}

const json = (body: unknown) => Promise.resolve(
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }),
)

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/issue-ai')) return json(AI)
  if (url.includes('/issue?')) return json(DETAIL)
  if (url.includes('/api/')) return json(/repos|members|labels|milestones/.test(url) ? [] : {})
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

await initI18n()

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{ width: 1040, height: 560, display: 'flex', background: 'var(--bg)' }}
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
