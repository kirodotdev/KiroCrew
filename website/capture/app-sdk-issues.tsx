/**
 * Isolated capture entry for the App SDK's `IssueList` / `IssueCard`.
 *
 * WHY ISOLATED: `POST /api/source/issues` is served by a gateway route that
 * lands in a separate PR. `fetch` is stubbed with one page per provider; the
 * components, the app's scoped client and the stylesheet are the real ones.
 *
 * Scenes (?scene=):
 *   condensed  the list in rows, filter bar on, all four providers, Load more.
 *   expanded   the same issues as cards.
 *   refused    an app that declares only /api/source/issue (singular).
 *   empty / loading / error   the list's other states, filter bar on.
 *   (The shoot script clicks "Assigned to me" on the condensed scene.)
 * Theme: &theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { AppApiProvider, IssueList } from '../src/app-sdk/index'
import type { IssueSearchRequest, IssueSearchResponse, IssueSummary } from '../src/types'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'condensed'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const hoursAgo = (h: number) => new Date(Date.now() - h * 3600_000).toISOString()
const person = (login: string, name: string) => ({ login, name, avatarUrl: '' })

const ISSUES: IssueSummary[] = [
  {
    provider: 'github', url: 'https://github.com/acme/widgets/issues/4821', key: '#4821', number: 4821,
    title: 'Crash when the issue list is empty and the filter bar is open', state: 'open', statusLabel: 'Open',
    labels: [
      { name: 'bug', color: 'd73a4a', description: '' },
      { name: 'frontend', color: '0e8a16', description: '' },
      { name: 'p1', color: 'fbca04', description: '' },
    ],
    assignees: [person('octocat', 'Mona Lisa')], author: person('hubot', 'Hubot'), priority: null,
    commentCount: 4, createdAt: hoursAgo(80), updatedAt: hoursAgo(2),
    excerpt: 'Steps to reproduce: open an app with no issues, open the filter bar, then clear the search text. The list throws instead of showing the empty state.',
    project: 'acme/widgets',
    linkedChanges: [{ provider: 'github', url: 'https://github.com/acme/widgets/pull/4830', number: 4830, title: 'Fix empty list', state: 'open' }],
  },
  {
    provider: 'gitlab', url: 'https://gitlab.com/acme/platform/-/issues/312', key: '#312', number: 312,
    title: 'Pipeline cache misses on every merge request', state: 'open', statusLabel: 'opened',
    labels: [{ name: 'ci', color: '428bca', description: '' }],
    assignees: [], author: person('dana', 'Dana Ruiz'), priority: 'Medium',
    commentCount: 0, createdAt: hoursAgo(200), updatedAt: hoursAgo(26),
    excerpt: 'The cache key includes the commit SHA, so no job ever reuses it.', project: 'acme/platform', linkedChanges: [],
  },
  {
    provider: 'jira', url: 'https://acme.atlassian.net/browse/CREW-142', key: 'CREW-142', number: null,
    title: 'Let crews pick up Jira tickets from the board', state: 'open', statusLabel: 'In Progress',
    labels: [{ name: 'feature', color: 'a2eeef', description: '' }, { name: 'crews', color: '5319e7', description: '' }],
    assignees: [person('kyle', 'Kyle')], author: person('sam', 'Sam Lee'), priority: 'High',
    commentCount: 12, createdAt: hoursAgo(400), updatedAt: hoursAgo(5),
    excerpt: 'A crew watching CREW should see new tickets within a minute.', project: 'CREW', linkedChanges: [],
  },
  {
    provider: 'linear', url: 'https://linear.app/acme/issue/ENG-77', key: 'ENG-77', number: null,
    title: 'خطأ عند فتح القائمة في الوضع الداكن', state: 'closed', statusLabel: 'Done',
    labels: [], assignees: [], author: null, priority: null,
    commentCount: 1, createdAt: hoursAgo(900), updatedAt: hoursAgo(72),
    excerpt: 'نص عربي يصف المشكلة.', project: 'ENG', linkedChanges: [],
  },
]

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href)
  if (url.pathname !== '/api/source/issues') return realFetch(input, init)
  const body = JSON.parse(String(init?.body ?? '{}')) as IssueSearchRequest
  if (scene === 'loading') return new Promise<Response>(() => {})
  if (scene === 'error') {
    return new Response(JSON.stringify({ error: 'GitHub rate limit reached. Try again in a few minutes.' }), { status: 502, headers: { 'Content-Type': 'application/json' } })
  }
  if (scene === 'empty') {
    return new Response(JSON.stringify({ provider: body.provider, scope: body.scope, issues: [], nextCursor: null, total: 0 }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  }
  const first = !body.cursor
  const page: IssueSearchResponse = {
    provider: body.provider, scope: body.scope,
    issues: first ? ISSUES : [], nextCursor: first ? 'page-2' : null, total: null,
  }
  return new Response(JSON.stringify(page), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

const QUERY: IssueSearchRequest = { provider: 'github', scope: 'acme/widgets' }
const declared = scene === 'refused' ? ['/api/source/issue'] : ['/api/source/issues']

createRoot(document.getElementById('root')!).render(
  <div className="min-h-screen bg-bg p-4 text-text">
    <div data-testid="frame" className="mx-auto max-w-[720px]">
      <AppApiProvider appName="issues-demo" allowedApiPaths={declared} navigateFn={() => {}}>
        <IssueList query={QUERY} view={scene === 'expanded' ? 'expanded' : 'condensed'} showFilters={scene !== 'expanded'} />
      </AppApiProvider>
    </div>
  </div>,
)
