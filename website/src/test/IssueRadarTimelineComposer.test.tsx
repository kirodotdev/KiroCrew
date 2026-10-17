import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import { useEffect } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'

import ErrorNotice from '../components/ErrorNotice'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../components/NavigationLeaveGuard'
import { __resetNavSeamForTests, consumeChatHandoff, installSoftNavigate } from '../utils/errorReport'
import type {
  Issue, IssueDetailResponse, PullDetailResponse, PullRequest, TimelineEvent,
} from '../apps/issue-radar/api'

// Behaviour pins for the comment box at the head of the detail timeline, on the
// issue pane and on the pull-request pane.
//
//  * The box sits ABOVE the newest-first list, so the comment a reader is about to
//    write lands where the next one will appear.
//  * An issue is written through its own route and a pull request through the pull
//    route. GitLab and Azure DevOps number issues and change requests
//    independently, so the pull route given an issue number would comment on a
//    different item.
//  * After a post the pane re-reads the item, so the new comment is the top node
//    without the reader pressing Refresh.
//  * A failed post keeps the draft and says why, and a post that settles after the
//    pane moved to another item must not touch that item's draft.
//  * A repo connected read-only never shows the box.
//  * Typing while a post is in flight survives the post: a success empties the box only
//    when the draft still reads as the sent comment.
//  * Escape leaves the box and keeps the draft, and still reaches `window`, where the
//    pull request list clears its bulk selection. An Escape the input method owns (it is
//    cancelling a candidate) is left alone, in the live window and in the grace window
//    after `compositionend`. Clear is the visible way to discard a draft.
//  * An unsent draft stops a route change that would unmount it, such as the summary card's
//    "Ask the agent" hand-off to chat, and opens the discard dialog. Cancel keeps the reader
//    and the draft where they are, and an empty box asks nothing.

const api = {
  issueDetail: vi.fn(),
  issueAi: vi.fn(),
  addIssueComment: vi.fn(),
  pullDetail: vi.fn(),
  pullAi: vi.fn(),
  addPrComment: vi.fn(),
}
vi.mock('../apps/issue-radar/api', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  issueRadarApi: api,
}))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

// Children that own their own queries or markdown pipeline are stubbed; the box
// under test is not one of them.
vi.mock('../apps/issue-radar/components/RefMarkdown', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../apps/issue-radar/components/InvestigateButton', () => ({
  default: () => <div>investigate</div>,
}))
vi.mock('../apps/issue-radar/components/ReviewButton', () => ({
  default: () => <div>review-button</div>,
}))
vi.mock('../apps/issue-radar/components/PrActionsBar', () => ({
  default: () => <div>pr-actions-bar</div>,
}))
vi.mock('../apps/issue-radar/components/PrRunActions', () => ({
  default: () => <div>pr-run-actions</div>,
}))
vi.mock('../apps/issue-radar/components/AiSummaryCard', () => ({
  default: () => <div>ai-summary-card</div>,
}))

const IssueDetail = (await import('../apps/issue-radar/components/IssueDetail')).default
const PrDetail = (await import('../apps/issue-radar/components/PrDetail')).default
const TimelineComposer = (await import('../apps/issue-radar/components/TimelineComposer')).default

const REF = { owner: 'kirodotdev', repo: 'Kiro' }
const SCOPE = 'github:github.com:kirodotdev/Kiro'
const BOX = { name: 'Add a timeline comment' }

const ISSUE: Issue = {
  number: 11, title: 'Row title', url: 'https://github.com/kirodotdev/Kiro/issues/11',
  labels: [], comments: 0, author: 'alice', author_association: 'MEMBER', state: 'open',
  assignees: [], body: 'Row body', created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-02T00:00:00Z',
}
const PULL: PullRequest = {
  number: 7, title: 'Row title', url: 'https://github.com/kirodotdev/Kiro/pull/7',
  state: 'open', draft: false, labels: [], author: 'alice', author_association: 'MEMBER',
  created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-02T00:00:00Z', merged_at: null,
}

function remark(body: string, at: string): TimelineEvent {
  return { kind: 'comment', actor: 'bob', created_at: at, body }
}

/** The text of every comment card, in document order (the markdown stub). */
function cardTexts(): (string | null)[] {
  return screen.getAllByTestId('md').map((n) => n.textContent)
}

function issueResponse(number: number, timeline: TimelineEvent[]): IssueDetailResponse {
  return {
    owner: REF.owner, repo: REF.repo, number, from_cache: false, timeline,
    detail: {
      number, title: 'Detail title', body: 'The description body', state: 'open', state_reason: null,
      url: `https://github.com/kirodotdev/Kiro/issues/${number}`, author: 'alice',
      author_association: 'MEMBER', created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-02T00:00:00Z',
      closed_at: null, closed_by: null, comments: timeline.length, locked: false, labels: [],
      assignees: [], milestone: null,
      reactions: {
        total: 0, plus1: 0, minus1: 0, laugh: 0, hooray: 0, confused: 0, heart: 0, rocket: 0, eyes: 0,
      },
    },
  }
}

function pullResponse(timeline: TimelineEvent[]): PullDetailResponse {
  return {
    owner: REF.owner, repo: REF.repo, number: 7, from_cache: false, timeline, checks: [],
    detail: {
      number: 7, title: 'Detail title', body: 'The description body', state: 'open', draft: false,
      merged: false, url: 'https://github.com/kirodotdev/Kiro/pull/7', author: 'alice',
      author_association: 'MEMBER', created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-02T00:00:00Z',
      closed_at: null, merged_at: null, merged_by: null, comments: timeline.length, review_comments: 0,
      commits: 1, additions: 1, deletions: 0, changed_files: 1, mergeable: true, mergeable_state: 'clean',
      base: 'main', head: 'feat/thing', head_sha: 'abc1234', labels: [], assignees: [],
      requested_reviewers: [], milestone: null,
    },
  }
}

function renderIssue(issue: Issue = ISSUE) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (i: Issue) => (
    <QueryClientProvider client={qc}><IssueDetail issue={i} /></QueryClientProvider>
  )
  const view = render(tree(issue))
  return { ...view, qc, switchTo: (i: Issue) => view.rerender(tree(i)) }
}

function renderPull() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PrDetail pull={PULL} /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  ctx.value = {
    active: REF,
    colorByName: new Map(),
    memberRoleByLogin: new Map(),
    repoLabels: [],
    countByLabel: new Map(),
    canWrite: true,
    stateFilter: 'open',
    me: 'alice',
    refreshPrefs: { detailPollMs: 30_000, pollInBackground: false },
    listDetail: { isMobile: false, showList: true, showDetail: true, openDetail: vi.fn(), closeDetail: vi.fn() },
    openRef: vi.fn(),
  }
  api.issueAi.mockResolvedValue({
    owner: REF.owner, repo: REF.repo, number: 11, summary: '', suggested_labels: [],
    generated_at: '2026-07-02T01:00:00Z', from_cache: true,
  })
  api.pullAi.mockResolvedValue({
    owner: REF.owner, repo: REF.repo, number: 7, summary: '', generated_at: '2026-07-02T01:00:00Z', from_cache: true,
  })
})

describe('issue pane — comment box at the head of the timeline', () => {
  it('posts through the issue route and shows the comment as the top node', async () => {
    const older = remark('older remark', '2026-07-02T01:00:00Z')
    api.issueDetail
      .mockResolvedValueOnce(issueResponse(11, [older]))
      .mockResolvedValue(issueResponse(11, [older, remark('looks good', '2026-07-02T02:00:00Z')]))
    api.addIssueComment.mockResolvedValue({ number: 11, comment_id: 5, url: null })
    const user = userEvent.setup()
    renderIssue()

    const box = await screen.findByRole('textbox', BOX) as HTMLTextAreaElement
    // Above the list: the existing newest comment follows the box in document order.
    const existing = await screen.findByText('older remark')
    expect(box.compareDocumentPosition(existing) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    // A bare Enter is a newline; only Cmd/Ctrl+Enter sends.
    await user.type(box, 'looks good{Enter}')
    expect(api.addIssueComment).not.toHaveBeenCalled()
    await user.keyboard('{Control>}{Enter}{/Control}')

    await waitFor(() => expect(api.addIssueComment).toHaveBeenCalledWith(REF, 11, 'looks good'))
    // The new comment is the first card after the box, the older one second.
    await waitFor(() => {
      const cards = cardTexts()
      expect(cards).toContain('looks good')
      expect(cards.indexOf('looks good')).toBeLessThan(cards.indexOf('older remark'))
    })
    await waitFor(() => expect(box.value).toBe(''))
  })

  it('keeps the draft and shows the forge message when the post fails', async () => {
    api.issueDetail.mockResolvedValue(issueResponse(11, []))
    api.addIssueComment.mockRejectedValue(new Error('Resource not accessible by integration'))
    const user = userEvent.setup()
    renderIssue()

    const box = await screen.findByRole('textbox', BOX) as HTMLTextAreaElement
    await user.type(box, 'will retry')
    await user.click(screen.getByRole('button', { name: 'Comment' }))

    expect(await screen.findByText(/Resource not accessible by integration/)).toBeTruthy()
    expect(box.value).toBe('will retry')
    expect(api.issueDetail).toHaveBeenCalledTimes(1)
  })

  it('sends nothing for a blank draft', async () => {
    api.issueDetail.mockResolvedValue(issueResponse(11, []))
    const user = userEvent.setup()
    renderIssue()

    const box = await screen.findByRole('textbox', BOX) as HTMLTextAreaElement
    await user.type(box, '   {Control>}{Enter}{/Control}')
    expect(api.addIssueComment).not.toHaveBeenCalled()
  })

  it('sends once while a post is in flight, however many times the shortcut repeats', async () => {
    api.issueDetail.mockResolvedValue(issueResponse(11, []))
    let settle: (v: unknown) => void = () => {}
    api.addIssueComment.mockReturnValue(new Promise((r) => { settle = r }))
    const user = userEvent.setup()
    renderIssue()

    const box = await screen.findByRole('textbox', BOX) as HTMLTextAreaElement
    await user.type(box, 'only once')
    await user.keyboard('{Control>}{Enter}{/Control}')
    await waitFor(() => expect(api.addIssueComment).toHaveBeenCalledTimes(1))
    await user.keyboard('{Control>}{Enter}{/Control}{Control>}{Enter}{/Control}')
    await act(async () => { settle({ number: 11, comment_id: 7, url: null }) })

    await waitFor(() => expect(box.value).toBe(''))
    expect(api.addIssueComment).toHaveBeenCalledTimes(1)
  })

  it('does not let a post that settles late touch the draft of the next issue', async () => {
    api.issueDetail.mockImplementation(async (_ref: unknown, n: number) => issueResponse(n, []))
    let settle: (v: unknown) => void = () => {}
    api.addIssueComment.mockReturnValue(new Promise((r) => { settle = r }))
    const user = userEvent.setup()
    const pane = renderIssue()

    const first = await screen.findByRole('textbox', BOX) as HTMLTextAreaElement
    await user.type(first, 'for eleven')
    await user.keyboard('{Control>}{Enter}{/Control}')
    await waitFor(() => expect(api.addIssueComment).toHaveBeenCalledTimes(1))

    pane.switchTo({ ...ISSUE, number: 12 })
    const second = await screen.findByRole('textbox', BOX) as HTMLTextAreaElement
    expect(second.value).toBe('')
    await user.type(second, 'for twelve')
    await act(async () => { settle({ number: 11, comment_id: 6, url: null }) })

    // The stale post ran to its end: it marked the issue it was sent for as stale.
    await waitFor(() => expect(pane.qc.getQueryState(['issue-radar', 'issue', SCOPE, 11])?.isInvalidated).toBe(true))
    expect(second.value).toBe('for twelve')
  })

  it('is offered on a writable repo and withheld on a read-only one', async () => {
    api.issueDetail.mockResolvedValue(issueResponse(11, []))
    const writable = renderIssue()
    expect(await screen.findByRole('textbox', BOX)).toBeTruthy()
    writable.unmount()

    ctx.value = { ...ctx.value, canWrite: false }
    renderIssue()
    await screen.findByRole('heading', { level: 1 })
    expect(screen.queryByRole('textbox', BOX)).toBeNull()
  })
})

describe('pull-request pane — comment box at the head of the timeline', () => {
  it('posts through the pull route and shows the comment in the timeline', async () => {
    const older = remark('older remark', '2026-07-02T01:00:00Z')
    api.pullDetail
      .mockResolvedValueOnce(pullResponse([older]))
      .mockResolvedValue(pullResponse([older, remark('ship it', '2026-07-02T02:00:00Z')]))
    api.addPrComment.mockResolvedValue({ number: 7, id: 5, url: null, created_at: null })
    const user = userEvent.setup()
    renderPull()

    const box = await screen.findByRole('textbox', BOX) as HTMLTextAreaElement
    const existing = await screen.findByText('older remark')
    expect(box.compareDocumentPosition(existing) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    await user.type(box, 'ship it')
    await user.keyboard('{Control>}{Enter}{/Control}')

    await waitFor(() => expect(api.addPrComment).toHaveBeenCalledWith(REF, 7, 'ship it'))
    expect(api.addIssueComment).not.toHaveBeenCalled()
    await waitFor(() => expect(cardTexts()).toContain('ship it'))
    expect(api.pullDetail).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(box.value).toBe(''))
  })

  it('is offered on a writable repo and withheld on a read-only one', async () => {
    api.pullDetail.mockResolvedValue(pullResponse([]))
    const writable = renderPull()
    expect(await screen.findByRole('textbox', BOX)).toBeTruthy()
    writable.unmount()

    ctx.value = { ...ctx.value, canWrite: false }
    renderPull()
    await screen.findByRole('heading', { level: 1 })
    expect(screen.queryByRole('textbox', BOX)).toBeNull()
  })
})

// The box on its own, with a post the test settles by hand. What a draft survives is a
// property of the component, not of either pane that hosts it.
describe('comment box — what a draft survives', () => {
  function mount(post: (body: string) => Promise<unknown> = vi.fn()) {
    render(<TimelineComposer itemKey="item#1" post={post} />)
    return screen.getByRole('textbox', BOX) as HTMLTextAreaElement
  }

  /** Counts the Escape presses that reach `window`, where the pull request list listens. */
  function escapesSeenOnWindow() {
    const seen = vi.fn()
    window.addEventListener('keydown', seen)
    onTestFinished(() => window.removeEventListener('keydown', seen))
    return () => seen.mock.calls.filter(([e]) => (e as KeyboardEvent).key === 'Escape').length
  }

  it('keeps words typed while the post is in flight, and the box stays editable', async () => {
    let settle: (v?: unknown) => void = () => {}
    const post = vi.fn(() => new Promise((r) => { settle = r }))
    const user = userEvent.setup()
    const box = mount(post)

    await user.type(box, 'first thought')
    await user.keyboard('{Control>}{Enter}{/Control}')
    await waitFor(() => expect(post).toHaveBeenCalledWith('first thought'))
    expect(box.disabled).toBe(false)
    await user.type(box, ' and a second')
    await act(async () => { settle() })

    // The post ended for the item it was sent for, and the draft no longer reads as the
    // sent comment, so it is left as the reader has it.
    expect(box.value).toBe('first thought and a second')
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('still empties the box when only trailing whitespace was typed during the post', async () => {
    let settle: (v?: unknown) => void = () => {}
    const post = vi.fn(() => new Promise((r) => { settle = r }))
    const user = userEvent.setup()
    const box = mount(post)

    await user.type(box, 'just this')
    await user.keyboard('{Control>}{Enter}{/Control}')
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    await user.keyboard('{Enter}')
    await act(async () => { settle() })

    await waitFor(() => expect(box.value).toBe(''))
  })

  it('leaves the box on Escape, keeps the draft, and lets the key reach window', async () => {
    const escapes = escapesSeenOnWindow()
    const user = userEvent.setup()
    const box = mount()

    await user.type(box, 'a paragraph worth keeping')
    expect(document.activeElement).toBe(box)
    await user.keyboard('{Escape}')

    expect(box.value).toBe('a paragraph worth keeping')
    expect(document.activeElement).not.toBe(box)
    expect(escapes()).toBe(1)
  })

  it('leaves an Escape that cancels a live IME candidate to the input method', () => {
    const escapes = escapesSeenOnWindow()
    const box = mount()
    fireEvent.change(box, { target: { value: 'ありがとう' } })
    act(() => box.focus())
    fireEvent.compositionStart(box)

    // A live composition's keydown carries the native `isComposing` flag. The default is
    // KEPT, because the input method needs it to cancel the candidate list; fireEvent
    // returns false when preventDefault was called.
    const defaultKept = fireEvent.keyDown(box, { key: 'Escape', isComposing: true })

    expect(defaultKept).toBe(true)
    expect(box.value).toBe('ありがとう')
    expect(document.activeElement).toBe(box)
    expect(escapes()).toBe(0)
  })

  it('leaves an Escape inside the grace window after compositionend, then recovers', () => {
    // On WebKit the keydown that cancels a candidate can arrive AFTER compositionend with
    // `isComposing` already false; only the tracked latch identifies it. Fake timers hold
    // the 50 ms window open, so a loaded runner cannot let it lapse between the two calls.
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    const escapes = escapesSeenOnWindow()
    const box = mount()
    fireEvent.change(box, { target: { value: '你好' } })
    act(() => box.focus())
    fireEvent.compositionStart(box)
    fireEvent.compositionEnd(box)

    const defaultKept = fireEvent.keyDown(box, { key: 'Escape' })
    expect(defaultKept).toBe(false)
    expect(document.activeElement).toBe(box)
    expect(escapes()).toBe(0)

    // Past the window the key is the reader's again.
    act(() => { vi.advanceTimersByTime(60) })
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(box.value).toBe('你好')
    expect(document.activeElement).not.toBe(box)
    expect(escapes()).toBe(1)
  })

  it('discards the draft and a failure notice only through Clear', async () => {
    const post = vi.fn().mockRejectedValue(new Error('Resource not accessible by integration'))
    const user = userEvent.setup()
    const box = mount(post)
    const clear = screen.getByRole('button', { name: 'Clear' }) as HTMLButtonElement

    // Nothing to discard yet.
    expect(clear.disabled).toBe(true)
    await user.type(box, 'will not be sent')
    expect(clear.disabled).toBe(false)
    await user.keyboard('{Control>}{Enter}{/Control}')
    expect(await screen.findByText(/Resource not accessible by integration/)).toBeTruthy()
    expect(box.value).toBe('will not be sent')

    await user.click(clear)

    expect(box.value).toBe('')
    expect(screen.queryByText(/Resource not accessible by integration/)).toBeNull()
    expect(clear.disabled).toBe(true)
    expect(post).toHaveBeenCalledTimes(1)
  })
})

describe('comment box — leaving the page', () => {
  // The summary card's failure notice hands the error to chat, a route change that
  // unmounts the pane. The harness wires the shell as the dashboard does: the leave
  // registry, a router, and the soft navigator that `sendErrorToChat` goes through.
  function Shell() {
    const navigate = useNavigate()
    const mayLeave = useMayLeaveForNavigation()
    useEffect(() => {
      installSoftNavigate(navigate, mayLeave)
      return () => installSoftNavigate(null)
    }, [navigate, mayLeave])
    return (
      <Routes>
        <Route path="/issues" element={(
          <>
            <ErrorNotice message="Couldn't generate a summary" variant="inline" askAgent />
            <TimelineComposer itemKey="item#1" post={vi.fn()} />
          </>
        )} />
        <Route path="/chat" element={<div>chat page</div>} />
      </Routes>
    )
  }

  function mountShell() {
    onTestFinished(() => { __resetNavSeamForTests(); sessionStorage.clear() })
    render(
      <NavigationLeaveGuardProvider>
        <MemoryRouter initialEntries={['/issues']}>
          <Shell />
        </MemoryRouter>
      </NavigationLeaveGuardProvider>,
    )
    return screen.getByRole('textbox', BOX) as HTMLTextAreaElement
  }

  it('stops the hand-off to chat while a draft is unsent, and Cancel keeps it', async () => {
    const user = userEvent.setup()
    const box = mountShell()

    await user.type(box, 'half a reply')
    await user.click(screen.getByRole('button', { name: /ask the agent/i }))

    expect(screen.queryByText('chat page')).toBeNull()
    expect((screen.getByRole('textbox', BOX) as HTMLTextAreaElement).value).toBe('half a reply')
    expect(consumeChatHandoff()).toBeNull()
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard unsaved changes?')).toBeTruthy()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect((screen.getByRole('textbox', BOX) as HTMLTextAreaElement).value).toBe('half a reply')
    expect(screen.queryByText('chat page')).toBeNull()
  })

  it('lets the hand-off go once the reader discards the draft', async () => {
    const user = userEvent.setup()
    const box = mountShell()

    await user.type(box, 'half a reply')
    await user.click(screen.getByRole('button', { name: /ask the agent/i }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard changes' }))

    await waitFor(() => expect(box.value).toBe(''))
    await user.click(screen.getByRole('button', { name: /ask the agent/i }))

    expect(await screen.findByText('chat page')).toBeTruthy()
    expect(consumeChatHandoff()).toContain("Couldn't generate a summary")
  })

  it('asks nothing when the box holds no text', async () => {
    const user = userEvent.setup()
    const box = mountShell()

    await user.type(box, '   ')
    await user.click(screen.getByRole('button', { name: /ask the agent/i }))

    expect(await screen.findByText('chat page')).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
