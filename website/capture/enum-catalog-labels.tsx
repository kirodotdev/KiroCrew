/**
 * Isolated capture entry for the four wire-enum → catalog relabellings (#8487).
 *
 * The bug: four surfaces printed a raw wire enum value as visible text, cased by
 * a CSS `capitalize`. A screenshot is the only evidence that can show the fix,
 * because the change is a rendered STRING — a diff cannot show that "Conflicting"
 * now reaches the pane, or that an unmapped Jira state still reads "In Review"
 * (capitalized) beside the catalog-cased "Open". Each surface is the REAL
 * component, imported unmodified; only its inputs are fixtures.
 *
 * The three fetching components (PrDetail, PullRequestPanel, IssuePanel) read
 * their data through `window.fetch`, so a tiny in-page stub answers exactly the
 * endpoints they hit — same technique as the serve-dist harnesses, inlined
 * because this page mounts the components directly rather than the whole SPA.
 * StatusBadge takes a prop and fetches nothing.
 *
 * `?theme=` chooses dark (default) / light; `?lang=` chooses the locale, so the
 * capture script can shoot en and zh-CN from one page. i18n MUST be initialised
 * before mount or every label renders empty and the frame documents nothing.
 *
 * Usage: served by vite at /capture/enum-catalog-labels.html
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../src/store'
import ErrorBoundary from '../src/components/ErrorBoundary'
import { initI18n, i18next } from '../src/i18n/all'
import PullRequestPanel from '../src/components/PullRequestPanel'
import IssuePanel from '../src/components/IssuePanel'
import PrDetail from '../src/apps/issue-radar/components/PrDetail'
import { StatusBadge } from '../src/pages/settings/InstancesPanel'
import { IssueRadarProvider } from '../src/apps/issue-radar/context'
import type { PullRequest } from '../src/apps/issue-radar/api'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const lang = params.get('lang') || 'en'
document.documentElement.setAttribute('data-theme', theme)

// ---- fixtures ----------------------------------------------------------------

const PR_URL = 'https://github.com/acme/widgets/pull/42'
const ISSUE_URL = 'https://github.com/acme/widgets/issues/7'

/** issue-radar detail pane: an OPEN PR whose mergeable_state is `has_hooks`,
 *  which maps to the catalog label "Clean (hooks)" — a wording that could only
 *  come from the catalog, never from capitalizing the raw wire value. */
const PR_DETAIL = {
  owner: 'acme', repo: 'widgets', number: 42, from_cache: false,
  timeline: [], checks: [],
  detail: {
    number: 42, title: 'Guard the empty label list',
    body: 'Body', state: 'open', draft: false, merged: false,
    url: PR_URL, author: 'alice', author_association: 'MEMBER',
    created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-02T00:00:00Z',
    closed_at: null, merged_at: null, merged_by: null,
    comments: 1, review_comments: 0, commits: 2,
    additions: 12, deletions: 4, changed_files: 2,
    mergeable: true, mergeable_state: 'has_hooks',
    base: 'main', head: 'feat/guard', head_sha: 'abc1234',
    labels: [], assignees: [], requested_reviewers: [], milestone: null,
  },
}

const PR_LIST_ROW: PullRequest = {
  number: 42, title: 'Guard the empty label list', url: PR_URL,
  state: 'open', draft: false, labels: [], author: 'alice',
  author_association: 'MEMBER', updated_at: '2026-09-02T00:00:00Z',
  created_at: '2026-09-01T00:00:00Z', merged_at: null,
  additions: 12, deletions: 4, changed_files: 2,
  base: 'main', head: 'feat/guard', head_sha: 'abc1234',
  mergeable: true, mergeable_state: 'has_hooks',
}

/** chat PR side-panel: one changed file per GitHub file-status value so the
 *  relabelled statuses ("Added", "Modified", "Removed", "Renamed") are all in
 *  the frame. */
const PR_SOURCE = {
  provider: 'github', url: PR_URL, number: 42, title: 'Guard the empty label list',
  description: 'Body', state: 'open', draft: false, mergedAt: '', updatedAt: '2026-09-02T00:00:00Z',
  headBranch: 'feat/guard', baseBranch: 'main', headSha: 'abc1234', author: 'alice',
  additions: 20, deletions: 6, changedFiles: 4,
  mergeable: 'mergeable', mergeStateStatus: 'clean', autoMerge: false,
  commits: [], checks: [], comments: [],
  files: [
    { path: 'src/added.ts', status: 'added', additions: 10, deletions: 0, patch: '' },
    { path: 'src/panel.tsx', status: 'modified', additions: 6, deletions: 4, patch: '' },
    { path: 'src/gone.ts', status: 'removed', additions: 0, deletions: 2, patch: '' },
    { path: 'src/moved.ts', status: 'renamed', additions: 4, deletions: 0, patch: '' },
  ],
}

/** chat issue side-panel: two linked changes — a MAPPED lifecycle state
 *  ("Merged", catalog-cased, no `capitalize`) and an UNMAPPED Jira workflow
 *  state ("In Review", which keeps `capitalize` so it is not "in review"). */
const ISSUE_SOURCE = {
  provider: 'github', url: ISSUE_URL, number: 7, title: 'Crash on empty label list',
  description: 'Repro inside.', state: 'open', stateReason: '', author: 'octocat',
  createdAt: '2026-09-01T00:00:00Z', updatedAt: '2026-09-02T00:00:00Z', closedAt: '', closedBy: '',
  labels: [], assignees: [], milestone: null, reactions: null, comments: [],
  linkedChanges: [
    { provider: 'github', url: 'https://github.com/acme/widgets/pull/13', number: 13, title: 'Ship the guard', state: 'MERGED' },
    { provider: 'jira', url: 'https://jira.example/browse/PROJ-1', number: 1, title: 'Track the crash', state: 'In Review', issueKey: 'PROJ-1' },
  ],
}

const TUNNEL_STATUS = { instance_id: 'box', state: 'connected' as const, local_port: 7801, connected_at: Date.now() / 1000 }

// ---- fetch stub --------------------------------------------------------------
// Answer only the endpoints the four components hit; anything else 404s loudly.
const realFetch = window.fetch.bind(window)
const jsonResponse = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const path = url.startsWith('http') ? new URL(url).pathname + new URL(url).search : url
  if (path.includes('/api/apps/issue-radar/pull-ai')) return Promise.resolve(jsonResponse({ summary: '', suggested_labels: [] }))
  if (path.includes('/api/apps/issue-radar/deps')) return Promise.resolve(jsonResponse({ version: 1, nodes: {}, edges: [] }))
  if (path.includes('/api/apps/issue-radar/pull')) return Promise.resolve(jsonResponse(PR_DETAIL))
  if (path.includes('/api/source/pull-request/status')) return Promise.resolve(jsonResponse({ statuses: {} }))
  if (path.includes('/api/source/pull-request/checks')) return Promise.resolve(jsonResponse({ checks: [] }))
  if (path.includes('/api/source/pull-request')) return Promise.resolve(jsonResponse(PR_SOURCE))
  if (path.includes('/api/source/issue')) return Promise.resolve(jsonResponse(ISSUE_SOURCE))
  // Let asset/module requests through; stub the rest as empty.
  if (path.includes('/api/')) return Promise.resolve(jsonResponse({}))
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })

const ACTIVE = { owner: 'acme', repo: 'widgets', provider: 'github' as const, host: 'github.com' }
const CONNECTED = [{ owner: 'acme', repo: 'widgets', provider: 'github' as const, host: 'github.com', enabled: true }]

const Section = ({ label, width = 420, children }: { label: string; width?: number; children: React.ReactNode }) => (
  <div data-capture-section style={{ marginBottom: 28 }}>
    <div style={{ fontSize: 10, letterSpacing: 1, textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 6 }}>{label}</div>
    <div style={{ width, border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden', background: 'var(--bg)' }}>
      {/* Isolate each surface: a crash in one must not blank the frame for the
          other three. */}
      <ErrorBoundary>{children}</ErrorBoundary>
    </div>
  </div>
)

initI18n(lang)
// initI18n resolves the stored/boot language; force the requested one.
void i18next.changeLanguage(lang)

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <MemoryRouter initialEntries={['/']}>
      <QueryClientProvider client={qc}>
      <div
        data-capture-root
        style={{ padding: 20, width: 900, background: 'var(--bg)', color: 'var(--text)', font: '13px system-ui, -apple-system, sans-serif' }}
      >
        <Section label="PrDetail — mergeable_state (issue-radar)">
          <IssueRadarProvider repos={CONNECTED as never} active={ACTIVE as never} onSwitch={() => {}} onAddRepo={() => {}}>
            <PrDetail pull={PR_LIST_ROW} />
          </IssueRadarProvider>
        </Section>

        <Section label="PullRequestPanel — file.status" width={480}>
          <PullRequestPanel sources={[{ url: PR_URL, provider: 'github', number: 42, repo: 'widgets', kind: 'change' }] as never} selectedUrl={PR_URL} onSelect={() => {}} />
        </Section>

        <Section label="IssuePanel — linked-change state (mapped + unmapped raw)" width={480}>
          <IssuePanel issues={[{ url: ISSUE_URL, provider: 'github', number: 7, repo: 'widgets', kind: 'issue' }] as never} selectedUrl={ISSUE_URL} onSelect={() => {}} onAddToChat={() => {}} />
        </Section>

        <Section label="StatusBadge — tunnel status.state (settings)" width={280}>
          <div style={{ padding: 12 }}><StatusBadge status={TUNNEL_STATUS as never} /></div>
        </Section>
      </div>
      </QueryClientProvider>
    </MemoryRouter>
  </Provider>,
)
