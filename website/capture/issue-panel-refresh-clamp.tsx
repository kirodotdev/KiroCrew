/**
 * Evidence for the IssuePanel half of the inline-notice clamp.
 *
 * Mounts the REAL IssuePanel against the real stylesheet, theme tokens and live
 * i18n catalog; only `window.fetch` is stubbed, and only to reach the state this
 * row exists for — an issue that LOADED, whose refresh then failed. Nothing here
 * re-implements the row, the notice or the string, so a frame proves what ships,
 * and every prop exists on the base branch too, which is how `before` is taken.
 *
 *   ?locale=<tag>  catalog to render, default `de` (longest shipped string)
 *   ?panel=<px>    panel width, default 360 (a narrow side panel)
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import IssuePanel from '../src/components/IssuePanel'
import type { IssueSource } from '../src/types'
import type { PullRequestLink } from '../src/utils/pullRequestLinks'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const locale = params.get('locale') || 'de'
const panelWidth = Number(params.get('panel') || 360)

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n(locale)

const issue: IssueSource = {
  provider: 'github',
  url: 'https://github.com/acme/widgets/issues/9',
  number: 9,
  title: 'Crash on empty label list',
  description: '## Steps\nOpen the panel with no labels.',
  state: 'open',
  stateReason: '',
  author: 'octocat',
  createdAt: '2026-07-20T09:00:00Z',
  updatedAt: '2026-07-28T09:00:00Z',
  closedAt: '',
  closedBy: '',
  labels: [{ name: 'bug', color: 'd73a4a', description: 'Something is broken' }],
  assignees: ['hubot'],
  milestone: null,
  commentCount: 0,
  locked: false,
  reactions: null,
  comments: [],
  linkedChanges: [],
  partialSections: [],
}

const links: PullRequestLink[] = [
  { url: issue.url, provider: 'github', number: 9, repo: 'widgets', kind: 'issue' },
]

/**
 * The first issue read succeeds and every later one fails: a stub that always
 * failed would leave no cached issue, and so no row at all.
 */
let issueReads = 0
window.fetch = ((input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/source/issue')) return Promise.resolve(new Response('{}', { status: 200 }))
  issueReads += 1
  if (issueReads === 1) {
    return Promise.resolve(new Response(JSON.stringify(issue), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
  }
  return Promise.reject(new TypeError('Failed to fetch: gateway unreachable'))
}) as typeof window.fetch

// Retries would keep the panel in `isLoading` past the capture window; the
// settled cached-plus-error state is the frame under test.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const root = createRoot(document.getElementById('root')!)
root.render(
  <QueryClientProvider client={qc}>
    <div data-capture-root className="p-4 bg-bg">
      {/* Fixed narrow width: the wrap this change prevents only appears once the
          row is too narrow for the string on one line. */}
      <div
        data-capture-panel
        style={{ width: `${panelWidth}px`, height: '420px' }}
        className="flex flex-col rounded-lg border border-border bg-card overflow-hidden"
      >
        <IssuePanel issues={links} selectedUrl={issue.url} onSelect={() => {}} onAddToChat={() => {}} />
      </div>
    </div>
  </QueryClientProvider>,
)
