// Review threads: grouping, replying, resolving.
//
// The provider hands back a FLAT comment list with a threadId on inline comments,
// so grouping is where the real logic lives — and the reason the tab is usable at
// all, since a reply separated from the line it answers says nothing.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import CommentThreads, {
  groupThreads,
} from '../components/CommentThreads'
import { api } from '../api/client'
import type { PullRequestComment, PullRequestSource } from '../types'

vi.mock('../api/client', () => ({
  api: {
    replyToPullRequestThread: vi.fn(),
    commentOnPullRequest: vi.fn(),
    resolvePullRequestThread: vi.fn(),
    unresolvePullRequestThread: vi.fn(),
  },
}))

const mockApi = api as unknown as Record<string, ReturnType<typeof vi.fn>>
const URL_ = 'https://github.com/acme/widgets/pull/7'

function comment(over: Partial<PullRequestComment> = {}): PullRequestComment {
  return {
    id: 'c1', kind: 'inline', author: 'bob', body: 'Cap this',
    state: '', createdAt: new Date().toISOString(), url: '',
    path: 'src/jar.py', line: 42, threadId: 'T1', resolvable: true,
    resolved: false,
    ...over,
  }
}

function source(comments: PullRequestComment[], over: Partial<PullRequestSource> = {}) {
  return {
    provider: 'github', url: URL_, number: 7, title: 't', description: '',
    state: 'open', draft: false, mergedAt: '', updatedAt: '', headBranch: 'x',
    baseBranch: 'main', headSha: 'abc', author: 'ann', additions: 1,
    deletions: 0, changedFiles: 1, commits: [], checks: [], comments,
    files: [], partialSections: [],
    ...over,
  } as unknown as PullRequestSource
}

function mount(src: PullRequestSource) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}><CommentThreads src={src} /></QueryClientProvider>,
  )
}

describe('groupThreads', () => {
  it('collapses comments sharing a threadId into one thread', () => {
    const threads = groupThreads([
      comment({ id: 'c1', body: 'Cap this' }),
      comment({ id: 'c2', body: 'Done', author: 'ann' }),
    ])
    expect(threads).toHaveLength(1)
    expect(threads[0].root.id).toBe('c1')
    expect(threads[0].replies.map((r) => r.id)).toEqual(['c2'])
  })

  it('keeps standalone comments and review summaries separate', () => {
    // GitHub does not thread these either, so neither do we.
    const threads = groupThreads([
      comment({ id: 'c1', kind: 'comment', threadId: '', path: '' }),
      comment({ id: 'c2', kind: 'review', threadId: '', path: '', state: 'APPROVED' }),
    ])
    expect(threads).toHaveLength(2)
    expect(threads.every((t) => t.replies.length === 0)).toBe(true)
  })

  it('treats a thread as resolved when any of its comments says so', () => {
    // The flat payload repeats the flag per comment; resolution is a property of
    // the thread, not of one message in it.
    const threads = groupThreads([
      comment({ id: 'c1', resolved: false }),
      comment({ id: 'c2', resolved: true }),
    ])
    expect(threads[0].resolved).toBe(true)
  })

  it('drops a review that carries no message', () => {
    // Scanners submit a bodyless review alongside their inline findings; it
    // rendered as an "(empty)" card and pushed real threads down the page.
    const threads = groupThreads([
      comment({ id: 'r1', kind: 'review', body: '', threadId: '', path: '' }),
      comment({ id: 'c1', body: 'Cap this' }),
    ])
    expect(threads).toHaveLength(1)
    expect(threads[0].root.id).toBe('c1')
  })

  it('keeps a review that actually says something', () => {
    const threads = groupThreads([
      comment({ id: 'r1', kind: 'review', body: 'Looks good', threadId: '', path: '' }),
    ])
    expect(threads).toHaveLength(1)
  })

  it('preserves provider order', () => {
    const threads = groupThreads([
      comment({ id: 'a', threadId: 'T1' }),
      comment({ id: 'b', threadId: 'T2' }),
      comment({ id: 'c', threadId: 'T1' }),
    ])
    expect(threads.map((t) => t.threadId)).toEqual(['T1', 'T2'])
  })

  it('carries the file anchor from the thread root', () => {
    const threads = groupThreads([comment({ path: 'src/a.py', line: 9 })])
    expect(threads[0].path).toBe('src/a.py')
    expect(threads[0].line).toBe(9)
  })
})

describe('reading threads', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows a reply nested under the comment it answers', () => {
    mount(source([
      comment({ id: 'c1', body: 'Cap this' }),
      comment({ id: 'c2', body: 'Fixed in abc123', author: 'ann' }),
    ]))
    expect(screen.getByText(/Cap this/)).toBeTruthy()
    expect(screen.getByText(/Fixed in abc123/)).toBeTruthy()
    expect(screen.getByText('2 messages')).toBeTruthy()
  })

  it('anchors the thread to its file and line', () => {
    mount(source([comment({ path: 'src/jar.py', line: 42 })]))
    expect(screen.getByText('src/jar.py:42')).toBeTruthy()
  })

  it('hides resolved threads behind a count instead of dropping them', () => {
    mount(source([
      comment({ id: 'c1', threadId: 'T1', body: 'Open thing' }),
      comment({ id: 'c2', threadId: 'T2', body: 'Settled thing', resolved: true }),
    ]))
    expect(screen.getByText(/1 open · 1 resolved/)).toBeTruthy()
    expect(screen.queryByText(/Settled thing/)).toBeNull()
  })

  it('reveals resolved threads on request', async () => {
    mount(source([
      comment({ id: 'c1', threadId: 'T1' }),
      comment({ id: 'c2', threadId: 'T2', body: 'Settled thing', resolved: true }),
    ]))
    await userEvent.click(screen.getByRole('button', { name: /Show resolved/ }))
    expect(await screen.findByText(/Settled thing/)).toBeTruthy()
  })

  it('says so when there are no comments', () => {
    mount(source([]))
    expect(screen.getByText(/No comments yet/)).toBeTruthy()
  })
})

describe('writing', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockApi.replyToPullRequestThread.mockResolvedValue({ posted: true })
    mockApi.commentOnPullRequest.mockResolvedValue({ posted: true })
    mockApi.resolvePullRequestThread.mockResolvedValue({ resolved: true })
    mockApi.unresolvePullRequestThread.mockResolvedValue({ resolved: false })
  })

  it('replies into the thread it was opened from', async () => {
    mount(source([
      comment({ id: 'c1', threadId: 'T1' }),
      comment({ id: 'c2', threadId: 'T2', body: 'Other thread' }),
    ]))
    const threads = screen.getAllByRole('listitem')
    const second = threads[1]
    await userEvent.click(within(second).getByRole('button', { name: /^Reply$/ }))
    await userEvent.type(within(second).getByRole('textbox', { name: /Reply/ }), 'Agreed')
    await userEvent.click(within(second).getByRole('button', { name: /^Reply$/ }))
    await waitFor(() => expect(mockApi.replyToPullRequestThread)
      .toHaveBeenCalledWith(URL_, 'T2', 'Agreed'))
  })

  it('refuses to send an empty reply', async () => {
    mount(source([comment()]))
    await userEvent.click(screen.getByRole('button', { name: /^Reply$/ }))
    await userEvent.type(screen.getByRole('textbox', { name: /Reply/ }), '   ')
    // An accidental empty comment is visible to everyone and cannot be removed
    // from here, so the button stays disabled rather than posting whitespace.
    expect(screen.getByRole('button', { name: /^Reply$/ })).toBeDisabled()
    expect(mockApi.replyToPullRequestThread).not.toHaveBeenCalled()
  })

  it('posts a top-level comment when nobody is being replied to', async () => {
    mount(source([]))
    await userEvent.click(screen.getByRole('button', { name: /Comment on this pull request/ }))
    await userEvent.type(
      screen.getByRole('textbox', { name: /Comment on this pull request/ }),
      'Looks good overall')
    await userEvent.click(screen.getByRole('button', { name: /Comment on this pull request/ }))
    await waitFor(() => expect(mockApi.commentOnPullRequest)
      .toHaveBeenCalledWith(URL_, 'Looks good overall'))
  })

  it('resolves an open thread', async () => {
    mount(source([comment({ threadId: 'T1' })]))
    await userEvent.click(screen.getByRole('button', { name: /Resolve/ }))
    await waitFor(() => expect(mockApi.resolvePullRequestThread)
      .toHaveBeenCalledWith(URL_, 'T1'))
  })

  it('reopens a resolved thread', async () => {
    mount(source([comment({ threadId: 'T1', resolved: true })]))
    await userEvent.click(screen.getByRole('button', { name: /Show resolved/ }))
    await userEvent.click(await screen.findByRole('button', { name: /Reopen/ }))
    await waitFor(() => expect(mockApi.unresolvePullRequestThread)
      .toHaveBeenCalledWith(URL_, 'T1'))
  })

  it('offers no resolve control on a comment that is not a thread', () => {
    // Standalone comments and review summaries have nothing to resolve.
    mount(source([comment({ kind: 'comment', threadId: '', path: '' })]))
    expect(screen.queryByRole('button', { name: /Resolve/ })).toBeNull()
  })

  it('surfaces a failed reply instead of looking sent', async () => {
    mockApi.replyToPullRequestThread.mockRejectedValue(new Error('gh api said no'))
    mount(source([comment({ threadId: 'T1' })]))
    await userEvent.click(screen.getByRole('button', { name: /^Reply$/ }))
    await userEvent.type(screen.getByRole('textbox', { name: /Reply/ }), 'Agreed')
    await userEvent.click(screen.getByRole('button', { name: /^Reply$/ }))
    expect(await screen.findByText(/gh api said no/)).toBeTruthy()
  })

  it('keeps the draft when the post fails, so nothing typed is lost', async () => {
    mockApi.replyToPullRequestThread.mockRejectedValue(new Error('gh api said no'))
    mount(source([comment({ threadId: 'T1' })]))
    await userEvent.click(screen.getByRole('button', { name: /^Reply$/ }))
    const box = screen.getByRole('textbox', { name: /Reply/ })
    await userEvent.type(box, 'Agreed')
    await userEvent.click(screen.getByRole('button', { name: /^Reply$/ }))
    await screen.findByText(/gh api said no/)
    // Composer still open with the text in it, ready to retry.
    expect(screen.getByRole('textbox', { name: /Reply/ })).toHaveValue('Agreed')
  })

  it('clears the draft once the post lands', async () => {
    mount(source([comment({ threadId: 'T1' })]))
    await userEvent.click(screen.getByRole('button', { name: /^Reply$/ }))
    await userEvent.type(screen.getByRole('textbox', { name: /Reply/ }), 'Agreed')
    await userEvent.click(screen.getByRole('button', { name: /^Reply$/ }))
    await waitFor(() => expect(screen.queryByRole('textbox', { name: /Reply/ })).toBeNull())
  })

  it('says replying is GitHub-only on a GitLab merge request', () => {
    mount(source([comment({ threadId: '' })], { provider: 'gitlab' }))
    expect(screen.getByText(/GitHub-only for now/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Reply$/ })).toBeNull()
  })
})
