/**
 * The App SDK's `PullRequestPanel` reaches only what the hosting app declares.
 *
 * An external app's panel is backed by its scoped transport: an undeclared read
 * is a refusal with no request, and a declared read admits every endpoint the
 * panel calls, since each is a child of the read's path. A builtin page keeps
 * the dashboard's client, and a surface with no app identity fails closed.
 *
 * The host panel sits behind the SDK wrapper's lazy `import()`, so the first
 * query for anything it draws waits on that chunk (LAZY_PANEL).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { PullRequestSource } from '../types'
import type { PullRequestLink } from '../utils/pullRequestLinks'

const mockApi = vi.hoisted(() => ({
  pullRequestSource: vi.fn(),
  pullRequestChecks: vi.fn(),
  pullRequestStatuses: vi.fn(),
  markPullRequestReady: vi.fn(),
  enablePullRequestAutoMerge: vi.fn(),
  replyToPullRequestThread: vi.fn(),
  commentOnPullRequest: vi.fn(),
  resolvePullRequestThread: vi.fn(),
  unresolvePullRequestThread: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

import { AppApiProvider, PullRequestPanel, type AppApi } from '../app-sdk/index'
import { apiPathAllowed } from '../app-sdk/scopedApi'
import { AppIdentityProvider } from '../app-sdk/identity'
import { PULL_REQUEST_PANEL_PATHS, scopedPullRequestClient } from '../app-sdk/PullRequestPanel'

const URL_ = 'https://github.com/acme/widgets/pull/12'

// The wait for the host panel's lazy chunk to load and commit (docs/testing.md).
const LAZY_PANEL = { timeout: 5000 }

const draft: PullRequestSource = {
  provider: 'github',
  url: URL_,
  number: 12,
  title: 'Add source tabs',
  description: 'Source details.',
  state: 'OPEN',
  draft: true,
  mergedAt: '',
  updatedAt: '2026-07-13T12:00:00Z',
  headBranch: 'feature/source-tabs',
  baseBranch: 'main',
  headSha: 'abcdef123456',
  author: 'octocat',
  additions: 1,
  deletions: 1,
  changedFiles: 1,
  files: [{ path: 'src/panel.tsx', status: 'modified', additions: 1, deletions: 1, patch: '@@ -1 +1 @@\n-old\n+new' }],
  commits: [],
  checks: [],
  comments: [
    { id: '1', kind: 'inline', author: 'reviewer', body: 'Please cover this case.', state: '', createdAt: '2026-07-13T12:00:00Z', url: '', path: 'src/panel.tsx', line: 9, threadId: 'PRRT_thread1', resolvable: true, resolved: false },
  ],
}

const sources: PullRequestLink[] = [{ url: URL_, provider: 'github', number: 12, repo: 'widgets', kind: 'change' }]

let fetchMock: ReturnType<typeof vi.fn>
let appSeq = 0

function pathOf(call: unknown[]): string {
  return new URL(String(call[0]), 'http://localhost').pathname
}

beforeEach(() => {
  for (const fn of Object.values(mockApi)) fn.mockReset()
  mockApi.pullRequestSource.mockResolvedValue(draft)
  mockApi.pullRequestStatuses.mockResolvedValue({ statuses: {} })
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname
    const body = path === PULL_REQUEST_PANEL_PATHS.source ? draft
      : path === PULL_REQUEST_PANEL_PATHS.statuses ? { statuses: {} }
        : {}
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function externalApp(allowedApiPaths: string[], children: ReactNode) {
  // One app id per mount: an external app's query client is held per id.
  appSeq += 1
  return (
    <AppApiProvider appName={`pr-panel-app-${appSeq}`} allowedApiPaths={allowedApiPaths} navigateFn={() => {}}>
      {children}
    </AppApiProvider>
  )
}

const panel = <PullRequestPanel sources={sources} selectedUrl={URL_} onSelect={() => {}} />

async function openReviews() {
  fireEvent.click(await screen.findByRole('tab', { name: /Reviews/i }, LAZY_PANEL))
  await screen.findByText('Please cover this case.')
}

describe('scopedPullRequestClient', () => {
  const fakeApi = () => ({ post: vi.fn(async () => ({})) }) as unknown as AppApi & { post: ReturnType<typeof vi.fn> }

  it('is null without the source read, whatever else is declared', () => {
    const api = fakeApi()
    expect(scopedPullRequestClient(api, [])).toBeNull()
    expect(scopedPullRequestClient(api, ['/api/apps/my-app/*'])).toBeNull()
    expect(scopedPullRequestClient(api, [PULL_REQUEST_PANEL_PATHS.markReady, PULL_REQUEST_PANEL_PATHS.comment])).toBeNull()
    expect(scopedPullRequestClient(api, ['/api/source/issue'])).toBeNull()
  })

  it('is a client under each declaration that covers the read, and sends nothing yet', () => {
    const api = fakeApi()
    for (const declared of [[PULL_REQUEST_PANEL_PATHS.source], ['/api/source/pull-request/*'], ['/api/source/*']]) {
      expect(scopedPullRequestClient(api, declared), declared.join()).not.toBeNull()
    }
    expect(api.post).not.toHaveBeenCalled()
  })

  it('covers every endpoint it calls with the read declaration alone', () => {
    // A bare prefix covers its children at a slash boundary (the backend's rule
    // too), so the read's own path declares the whole family.
    for (const path of Object.values(PULL_REQUEST_PANEL_PATHS)) {
      expect(apiPathAllowed([PULL_REQUEST_PANEL_PATHS.source], path), path).toBe(true)
    }
  })

  it('posts the bodies the dashboard client posts', async () => {
    const api = fakeApi()
    const c = scopedPullRequestClient(api, ['/api/source/pull-request/*'])!
    await c.source(URL_)
    await c.source(URL_, true)
    await c.checks(URL_)
    await c.statuses([URL_])
    await c.markReady(URL_)
    await c.enableAutoMerge(URL_, true)
    await c.replyToThread(URL_, 'T1', 'hi')
    await c.comment(URL_, 'yo')
    await c.resolveThread(URL_, 'T1')
    await c.unresolveThread(URL_, 'T1')
    expect(api.post.mock.calls).toEqual([
      ['/api/source/pull-request', { url: URL_, refresh: false }],
      ['/api/source/pull-request', { url: URL_, refresh: true }],
      ['/api/source/pull-request/checks', { url: URL_ }],
      ['/api/source/pull-request/status', { urls: [URL_] }],
      ['/api/source/pull-request/ready', { url: URL_ }],
      ['/api/source/pull-request/auto-merge', { url: URL_, confirmImmediateMerge: true }],
      ['/api/source/pull-request/reply', { url: URL_, threadId: 'T1', body: 'hi' }],
      ['/api/source/pull-request/comment', { url: URL_, body: 'yo' }],
      ['/api/source/pull-request/resolve', { url: URL_, threadId: 'T1' }],
      ['/api/source/pull-request/unresolve', { url: URL_, threadId: 'T1' }],
    ])
  })
})

describe('the App SDK PullRequestPanel', () => {
  it('refuses an external app that declares no source read, and sends nothing', async () => {
    // Declaring a write or a sibling read is not the panel's read.
    render(externalApp(['/api/apps/pr-panel-app', PULL_REQUEST_PANEL_PATHS.comment, '/api/source/issue'], panel))
    expect(await screen.findByTestId('app-pull-request-panel-not-declared')).toHaveTextContent(PULL_REQUEST_PANEL_PATHS.source)
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.pullRequestSource).not.toHaveBeenCalled()
  })

  it('fails closed with no app identity and no SDK provider', async () => {
    const client = new QueryClient()
    render(<QueryClientProvider client={client}>{panel}</QueryClientProvider>)
    expect(await screen.findByTestId('app-pull-request-panel-not-declared')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(mockApi.pullRequestSource).not.toHaveBeenCalled()
  })

  it('draws every write once the read is declared, through the app transport', async () => {
    render(externalApp([PULL_REQUEST_PANEL_PATHS.source], panel))
    expect(await screen.findByRole('button', { name: /Ready for review/i }, LAZY_PANEL)).toBeInTheDocument()
    await openReviews()
    expect(screen.getByRole('button', { name: /^Reply/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Resolve/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Comment on this pull request/i })).toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.map(pathOf)).toContain(PULL_REQUEST_PANEL_PATHS.statuses))
    fireEvent.click(screen.getByRole('button', { name: /Ready for review/i }))
    await waitFor(() => expect(fetchMock.mock.calls.map(pathOf)).toContain(PULL_REQUEST_PANEL_PATHS.markReady))
    expect(Object.values(mockApi).every(fn => fn.mock.calls.length === 0)).toBe(true)
  })

  it('keeps the dashboard client for a builtin page, as the chat window has it', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <AppIdentityProvider appId="code-review-sage" origin="builtin">{panel}</AppIdentityProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('button', { name: /Ready for review/i }, LAZY_PANEL)).toBeInTheDocument()
    expect(mockApi.pullRequestSource).toHaveBeenCalledWith(URL_, false)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
