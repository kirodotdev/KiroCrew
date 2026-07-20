import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PullRequestSource } from '../types'
import { MAX_PULL_REQUEST_SOURCES } from '../utils/pullRequestLinks'

const mockApi = vi.hoisted(() => ({
  pullRequestChecks: vi.fn(),
  pullRequestSource: vi.fn(),
  resolvePullRequestThread: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

import PullRequestPanel, {
  CHECK_POLL_MAX_FAILURES,
  pullRequestCheckPollDelay,
} from '../components/PullRequestPanel'

const github: PullRequestSource = {
  provider: 'github',
  url: 'https://github.com/acme/widgets/pull/12',
  number: 12,
  title: 'Add source tabs',
  description: '## Summary\nSource details.',
  state: 'OPEN',
  draft: false,
  mergedAt: '',
  updatedAt: '2026-07-13T12:00:00Z',
  headBranch: 'feature/source-tabs',
  baseBranch: 'main',
  headSha: 'abcdef123456',
  author: 'octocat',
  additions: 20,
  deletions: 4,
  changedFiles: 1,
  files: [{ path: 'src/panel.tsx', status: 'modified', additions: 20, deletions: 4, patch: '@@ -1 +1 @@\n-old\n+new' }],
  commits: [{ sha: 'abcdef123456', title: 'Add source tabs', body: '', author: 'octocat', date: '2026-07-13T11:00:00Z', url: 'https://github.com/acme/widgets/commit/abcdef123456' }],
  checks: [{ name: 'test', workflow: 'CI', status: 'COMPLETED', conclusion: 'SUCCESS', bucket: 'passed', url: 'https://github.com/acme/widgets/actions/1', startedAt: '', completedAt: '' }],
  comments: [
    { id: '1', kind: 'inline', author: 'reviewer', body: 'Please cover this case.', state: '', createdAt: '2026-07-13T12:00:00Z', url: 'https://github.com/acme/widgets/pull/12#discussion_r1', path: 'src/panel.tsx', line: 9, threadId: 'PRRT_thread1', resolvable: true, resolved: false },
    { id: '2', kind: 'comment', author: 'commenter', body: 'General note.', state: '', createdAt: '2026-07-13T12:05:00Z', url: '', path: '', line: null, threadId: '', resolvable: false, resolved: false },
    { id: '3', kind: 'inline', author: 'reviewer', body: 'Already handled.', state: '', createdAt: '2026-07-13T12:10:00Z', url: '', path: 'src/panel.tsx', line: 20, threadId: 'PRRT_thread2', resolvable: true, resolved: true },
  ],
}

const gitlab: PullRequestSource = {
  ...github,
  provider: 'gitlab',
  url: 'https://gitlab.com/acme/service/-/merge_requests/7',
  number: 7,
  title: 'GitLab source',
  state: 'merged',
  mergedAt: '2026-07-13T10:00:00Z',
}

function renderPanel(onAddToChat = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    client,
    onAddToChat,
    ...render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={[
            { url: github.url, provider: 'github', number: 12, repo: 'widgets' },
            { url: gitlab.url, provider: 'gitlab', number: 7, repo: 'service' },
          ]}
          selectedUrl={github.url}
          onSelect={() => {}}
          onAddToChat={onAddToChat}
        />
      </QueryClientProvider>,
    ),
  }
}

beforeEach(() => {
  mockApi.pullRequestChecks.mockReset()
  mockApi.pullRequestChecks.mockResolvedValue({ checks: github.checks })
  mockApi.pullRequestSource.mockReset()
  mockApi.pullRequestSource.mockImplementation((url: string) => Promise.resolve(new URL(url).hostname === 'gitlab.com' ? gitlab : github))
  mockApi.resolvePullRequestThread.mockReset()
  mockApi.resolvePullRequestThread.mockResolvedValue({ resolved: true })
})

describe('PullRequestPanel', () => {
  it('renders provider identity, PR tabs, changed files, and check status', async () => {
    renderPanel()
    expect(await screen.findByText('Add source tabs')).toBeInTheDocument()
    expect(screen.getByText('Github', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('src/panel.tsx')).toBeInTheDocument()
    expect(screen.getByText('1 File Changed')).toBeInTheDocument()
    // Diffs stay unmounted until explicitly expanded, then parse after the
    // drawer animation deferral.
    expect(screen.queryByText('new')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /src\/panel\.tsx/i }))
    expect(await screen.findByText('new')).toBeInTheDocument()
    expect(screen.getByText('old')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /All checks passed/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /PR #12/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /MR !7/i })).toBeInTheDocument()
  })

  it('caps rendered source tabs at the per-slot limit', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const sources = Array.from({ length: MAX_PULL_REQUEST_SOURCES + 5 }, (_, index) => ({
      url: `https://github.com/acme/widgets/pull/${index + 1}`,
      provider: 'github' as const,
      number: index + 1,
      repo: 'widgets',
    }))

    render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={sources}
          selectedUrl={sources[0].url}
          onSelect={() => {}}
          onAddToChat={() => {}}
        />
      </QueryClientProvider>,
    )

    expect(screen.getAllByRole('tab', { name: /PR #/i })).toHaveLength(
      MAX_PULL_REQUEST_SOURCES,
    )
    expect(screen.queryByRole('tab', { name: `PR #${MAX_PULL_REQUEST_SOURCES + 1}` })).not.toBeInTheDocument()
  })

  it('separates network refresh from actual running checks', async () => {
    let resolveRefresh: (value: PullRequestSource) => void = () => {}
    const pendingCheck = {
      ...github.checks[0],
      status: 'IN_PROGRESS',
      conclusion: '',
      bucket: 'pending' as const,
    }
    mockApi.pullRequestChecks.mockResolvedValue({ checks: [pendingCheck] })
    renderPanel()
    await screen.findByText('Add source tabs')
    mockApi.pullRequestSource.mockImplementationOnce(
      () => new Promise<PullRequestSource>(resolve => { resolveRefresh = resolve }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Refresh pull request' }))

    expect(await screen.findByRole('button', { name: 'Refreshing pull request' })).toBeDisabled()
    expect(screen.getByRole('tab', { name: /Checks 1\/1/i })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /All checks passed/i })).not.toBeInTheDocument()
    expect(mockApi.pullRequestSource).toHaveBeenLastCalledWith(github.url, true)

    resolveRefresh({ ...github, checks: [pendingCheck] })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Refresh pull request' })).toBeEnabled(),
    )
    expect(screen.getByRole('tab', { name: /Checks running 0\/1/i })).toBeInTheDocument()
  })
  it('uses bounded backoff only while checks remain pending', () => {
    const pending = [{ ...github.checks[0], bucket: 'pending' as const }]

    expect(pullRequestCheckPollDelay(pending, 0)).toBe(10_000)
    expect(pullRequestCheckPollDelay(pending, 1)).toBe(20_000)
    expect(pullRequestCheckPollDelay(pending, 2)).toBe(40_000)
    expect(pullRequestCheckPollDelay(pending, CHECK_POLL_MAX_FAILURES)).toBe(false)
    expect(pullRequestCheckPollDelay(github.checks, 0)).toBe(false)
  })

  it('polls only checks until pending CI completes, then stops', async () => {
    vi.useFakeTimers()
    const pendingCheck = {
      ...github.checks[0],
      status: 'IN_PROGRESS',
      conclusion: '',
      bucket: 'pending' as const,
    }
    mockApi.pullRequestSource.mockResolvedValue({ ...github, checks: [pendingCheck] })
    mockApi.pullRequestChecks
      .mockResolvedValueOnce({ checks: [pendingCheck] })
      .mockResolvedValueOnce({ checks: github.checks })
    const { client, unmount } = renderPanel()

    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(screen.getByRole('tab', { name: /Checks running 0\/1/i })).toBeInTheDocument()
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000)
        await Promise.resolve()
      })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(2)
      await act(async () => {
        await Promise.resolve()
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(
        client.getQueryData<PullRequestSource>(['pull-request-source', github.url])
          ?.checks[0].bucket,
      ).toBe('passed')
      expect(screen.getByRole('tab', { name: /All checks passed 1\/1/i })).toBeInTheDocument()
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)

      await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(2)
    } finally {
      unmount()
      vi.useRealTimers()
    }
  })

  it('stops polling and removes the spinner after three check-provider failures', async () => {
    vi.useFakeTimers()
    const pendingCheck = {
      ...github.checks[0],
      status: 'IN_PROGRESS',
      conclusion: '',
      bucket: 'pending' as const,
    }
    mockApi.pullRequestSource.mockResolvedValue({ ...github, checks: [pendingCheck] })
    mockApi.pullRequestChecks.mockRejectedValue(new Error('provider unavailable'))
    const { unmount } = renderPanel()

    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000)
        await Promise.resolve()
      })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(2)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(40_000)
        await Promise.resolve()
      })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(CHECK_POLL_MAX_FAILURES)
      const unavailableTab = screen.getByRole('tab', { name: /Checks unavailable 0\/1/i })
      expect(unavailableTab.querySelector('.animate-spin')).toBeNull()
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)

      await act(async () => { await vi.advanceTimersByTimeAsync(120_000) })
      expect(mockApi.pullRequestChecks).toHaveBeenCalledTimes(CHECK_POLL_MAX_FAILURES)
      expect(mockApi.pullRequestSource).toHaveBeenCalledTimes(1)
    } finally {
      unmount()
      vi.useRealTimers()
    }
  })

  it('warns when provider page limits may hide results', async () => {
    mockApi.pullRequestSource.mockResolvedValue({
      ...github,
      partialSections: ['files', 'inline review comments'],
    })
    renderPanel()
    const warning = await screen.findByRole('status')
    expect(warning).toHaveTextContent('Provider results may be partial for files, inline review comments')
    expect(warning).toHaveTextContent('Open the pull request for the complete set')
  })

  it('shows commits, checks, and review comments in their tabs', async () => {
    const onAddToChat = vi.fn()
    renderPanel(onAddToChat)
    await screen.findByText('Add source tabs')

    fireEvent.click(screen.getByRole('tab', { name: /Commits 1/i }))
    expect(screen.getAllByText('Add source tabs')).toHaveLength(2)

    fireEvent.click(screen.getByRole('tab', { name: /All checks passed/i }))
    expect(screen.getByText('test')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open test check details' })).toHaveAttribute(
      'href',
      'https://github.com/acme/widgets/actions/1',
    )

    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))
    expect(screen.getByText('Please cover this case.')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: 'Add to chat' })[0])
    expect(onAddToChat).toHaveBeenCalledWith(expect.stringContaining('PR comment from reviewer'))
  })

  it('hands a failed CI check off to chat', async () => {
    const failing = { ...github, checks: [...github.checks, { name: 'lint', workflow: 'CI', status: 'COMPLETED', conclusion: 'FAILURE', bucket: 'failed' as const, url: 'https://github.com/acme/widgets/actions/2', startedAt: '', completedAt: '' }] }
    mockApi.pullRequestSource.mockImplementation((url: string) => Promise.resolve(new URL(url).hostname === 'gitlab.com' ? gitlab : failing))
    const onAddToChat = vi.fn()
    renderPanel(onAddToChat)
    await screen.findByText('Add source tabs')
    fireEvent.click(screen.getByRole('tab', { name: /Checks/i }))
    // Only the failed check exposes Add to chat.
    const addButtons = screen.getAllByRole('button', { name: 'Add to chat' })
    expect(addButtons).toHaveLength(1)
    fireEvent.click(addButtons[0])
    expect(onAddToChat).toHaveBeenCalledWith(expect.stringContaining('Failing CI check'))
    expect(onAddToChat).toHaveBeenCalledWith(expect.stringContaining('lint'))
  })

  it('does not render provider-controlled non-HTTP(S) URLs as links', async () => {
    const unsafe: PullRequestSource = {
      ...github,
      url: 'javascript:alert(1)',
      commits: [{ ...github.commits[0], url: 'data:text/html,unsafe' }],
      checks: [{ ...github.checks[0], url: 'javascript:alert(2)' }],
      comments: [{ ...github.comments[0], url: 'file:///tmp/unsafe' }],
    }
    mockApi.pullRequestSource.mockResolvedValue(unsafe)
    const { container } = renderPanel()
    await screen.findByText('Add source tabs')
    expect(screen.queryByRole('link', { name: 'Open pull request' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Commits 1/i }))
    expect(screen.getAllByText('Add source tabs')).toHaveLength(2)
    expect(screen.getAllByText('Add source tabs')[1].closest('a')).toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: /All checks passed/i }))
    expect(screen.queryByRole('link', { name: 'Open check details' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /Reviews 1/i }))
    expect(container.querySelector('a[href^="javascript:"], a[href^="data:"], a[href^="file:"]')).toBeNull()
  })

  it('collapses and expands a comment from the header chevron', async () => {
    renderPanel()
    await screen.findByText('Add source tabs')
    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))
    expect(screen.getByText('Please cover this case.')).toBeInTheDocument()

    fireEvent.click(screen.getAllByRole('button', { name: 'Collapse comment' })[0])
    expect(screen.queryByText('Please cover this case.')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Expand comment' }))
    expect(screen.getByText('Please cover this case.')).toBeInTheDocument()
  })

  it('shows Resolve only on resolvable unresolved comments and posts the resolution', async () => {
    renderPanel()
    await screen.findByText('Add source tabs')
    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))

    // One resolvable+unresolved comment gets the button, the resolved one gets the indicator,
    // the top-level comment gets neither.
    expect(screen.getAllByRole('button', { name: /Resolve/i })).toHaveLength(1)
    expect(screen.getByText('Resolved')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Resolve/i }))
    await waitFor(() =>
      expect(mockApi.resolvePullRequestThread).toHaveBeenCalledWith(github.url, 'PRRT_thread1'),
    )
  })

  it('shows an inline error when resolving fails', async () => {
    mockApi.resolvePullRequestThread.mockRejectedValue(new Error('boom'))
    renderPanel()
    await screen.findByText('Add source tabs')
    fireEvent.click(screen.getByRole('tab', { name: /Reviews 3/i }))

    fireEvent.click(screen.getByRole('button', { name: /Resolve/i }))
    expect(await screen.findByText('Could not resolve')).toBeInTheDocument()
  })

  it('fetches the selected GitLab merge request', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <PullRequestPanel
          sources={[{ url: gitlab.url, provider: 'gitlab', number: 7, repo: 'service' }]}
          selectedUrl={gitlab.url}
          onSelect={() => {}}
          onAddToChat={() => {}}
        />
      </QueryClientProvider>,
    )
    expect(await screen.findByText('GitLab source')).toBeInTheDocument()
    await waitFor(() => expect(mockApi.pullRequestSource).toHaveBeenCalledWith(gitlab.url, false))
    expect(screen.getByText('Merged')).toBeInTheDocument()
  })
})
