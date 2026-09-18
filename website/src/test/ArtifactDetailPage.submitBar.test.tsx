/**
 * Batch comment submit on the standalone artifact page.
 *
 * The comments sidebar's footer now takes the chat side panel's own `SubmitBar`,
 * so the full-page route can hand a whole review to the artifact's companion
 * session in one press instead of only staging a prompt.
 *
 * Scope: the new prop path. What each guard is for —
 * - the count is what the press promises to send, so an agent comment and an
 *   already-sent one must not inflate it (an inflated count is also a re-send of
 *   the whole history);
 * - a refused send must leave the batch PENDING: sent ids are append-only, so
 *   marking an undelivered batch would drop a review with no way to re-offer it;
 * - with no companion session bound there is NO bar: a control that says "send to
 *   this chat" must not appear when there is no chat to send to, and deciding
 *   whether one exists from here is how a second companion chat gets opened;
 * - offline is gated like the file viewer's Submit All, because the send path
 *   refuses silently in that state.
 *
 * ChatPage is stubbed (its own suites cover it) and `api.sendChat` is faked with
 * Response-shaped bodies, so the real receipt classifier runs.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, act } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders, createTestStore } from './helpers'
import { api } from '../api/client'
import { sseSlots, sseConnected, sseDisconnected } from '../store/dashboardSlice'
import type { Artifact, ArtifactComment, ChatSlot } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const ARTIFACT: Artifact = {
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# CR Queue',
}

const mkComment = (id: string, overrides: Partial<ArtifactComment> = {}): ArtifactComment => ({
  id,
  body: `note ${id}`,
  author: 'zoe',
  created_at: '2026-05-21T22:10:00.000000+00:00',
  status: 'open',
  ...overrides,
} as ArtifactComment)

const BOUND = { key: 'chat-bound', title: 'Artifact: CR Queue', messages: 3, running: false, artifact: 'cr-queue' } as ChatSlot

const accepted = () => ({ ok: true, json: () => Promise.resolve({ ok: true }) })
const refused = () => ({ ok: false, json: () => Promise.resolve({ ok: false, error: 'slot agent mismatch' }) })

const SENT_KEY = 'mc-cmt-sent:cr-queue'

function renderPage(
  comments: ArtifactComment[],
  opts: { bound?: boolean; connected?: boolean; sentIds?: string[] } = {},
) {
  vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments })
  // Seeded BEFORE the render: the sent-id set is read on first render.
  if (opts.sentIds) localStorage.setItem(SENT_KEY, JSON.stringify(opts.sentIds))
  const store = createTestStore()
  act(() => {
    store.dispatch(opts.connected === false ? sseDisconnected() : sseConnected())
    store.dispatch(sseSlots(opts.bound === false ? [] : [BOUND]))
  })
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      <Route path="/artifacts" element={<div>library page target</div>} />
    </Routes>,
    { route: '/artifacts/cr-queue', store },
  )
}

/** The bar lives in the comments sidebar, which auto-reveals once the artifact
 *  has comments. */
const bar = () => screen.findByText(/to send to this chat/)
const submit = () => screen.getByRole('button', { name: /^Submit$/ })

describe('ArtifactDetailPage comment submit bar', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    if (!URL.createObjectURL) {
      // @ts-expect-error stub
      URL.createObjectURL = vi.fn().mockReturnValue('blob:http://localhost:6776/test')
      // @ts-expect-error stub
      URL.revokeObjectURL = vi.fn()
    }
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(ARTIFACT)
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'slot-new', title: 'Artifact: CR Queue' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([BOUND])
    vi.mocked(api).sendChat = vi.fn().mockResolvedValue(accepted())
  })

  it('counts only the human comments nobody has submitted yet', async () => {
    renderPage([
      mkComment('h1'),
      mkComment('h2'),
      mkComment('a1', { is_agent: true }),
      mkComment('s1'),
    ], { sentIds: ['s1'] })
    expect(await screen.findByText('2 comments to send to this chat')).toBeInTheDocument()
  })

  it('leaves a resolved thread out of the batch, reply included', async () => {
    // This page has its OWN pending-batch derivation, separate from
    // ArtifactPanel's. Filtering only the panel's would show the page's filtered
    // "N open comments" count beside a bar that still ships the resolved ones.
    renderPage([
      mkComment('r1', { status: 'resolved', thread_id: 'r1' }),
      mkComment('r1a', { parent_id: 'r1', thread_id: 'r1' }),
      mkComment('h1', { thread_id: 'h1' }),
    ])
    expect(await screen.findByText('1 comment to send to this chat')).toBeInTheDocument()
  })

  it('does not send a resolved comment body even when open ones go with it', async () => {
    renderPage([
      mkComment('r1', { status: 'resolved', thread_id: 'r1' }),
      mkComment('h1', { thread_id: 'h1' }),
    ])
    await bar()
    fireEvent.click(submit())
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const [message] = vi.mocked(api).sendChat.mock.calls[0]
    expect(message).toContain('note h1')
    expect(message).not.toContain('note r1')
  })

  it('offers no bar when every thread is resolved', async () => {
    renderPage([mkComment('r1', { status: 'resolved', thread_id: 'r1' })])
    await waitFor(() => expect(screen.getByLabelText('Toggle comments')).toBeInTheDocument())
    expect(screen.queryByText(/to send to this chat/)).not.toBeInTheDocument()
  })

  it('offers no bar when every comment is agent-authored', async () => {
    renderPage([mkComment('a1', { is_agent: true })])
    await waitFor(() => expect(screen.getByLabelText('Toggle comments')).toBeInTheDocument())
    expect(screen.queryByText(/to send to this chat/)).not.toBeInTheDocument()
  })

  it('sends ONE formatted message to the bound companion session', async () => {
    renderPage([mkComment('h1'), mkComment('h2')])
    await bar()
    fireEvent.click(submit())
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const [message, slot] = vi.mocked(api).sendChat.mock.calls[0]
    expect(slot).toBe('chat-bound')
    expect(message).toContain('[Artifact feedback on CR Queue (cr-queue) — 2 comments]')
    expect(message).toContain('note h1')
    expect(message).toContain('note h2')
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })

  it('marks the batch sent, so a second press cannot re-send it', async () => {
    renderPage([mkComment('h1')])
    await bar()
    fireEvent.click(submit())
    await waitFor(() => expect(JSON.parse(localStorage.getItem(SENT_KEY) || '[]')).toEqual(['h1']))
    await waitFor(() => expect(screen.queryByText(/to send to this chat/)).not.toBeInTheDocument())
  })

  it('reveals the companion chat the batch landed in', async () => {
    renderPage([mkComment('h1')])
    await bar()
    fireEvent.click(submit())
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
  })

  it('keeps the comments pending when the send is refused', async () => {
    vi.mocked(api).sendChat = vi.fn().mockResolvedValue(refused())
    renderPage([mkComment('h1')])
    await bar()
    fireEvent.click(submit())
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/slot agent mismatch/)).toBeInTheDocument()
    expect(localStorage.getItem(SENT_KEY)).toBeNull()
    expect(screen.getByText('1 comment to send to this chat')).toBeInTheDocument()
  })

  it('says the comments are still pending when a refusal carries no reason', async () => {
    vi.mocked(api).sendChat = vi.fn().mockResolvedValue({ ok: false, json: () => Promise.reject(new Error('no body')) })
    renderPage([mkComment('h1')])
    await bar()
    fireEvent.click(submit())
    expect(await screen.findByText(/Couldn't send/)).toBeInTheDocument()
    expect(localStorage.getItem(SENT_KEY)).toBeNull()
  })

  it('keeps the comments pending when the transport rejects', async () => {
    vi.mocked(api).sendChat = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    renderPage([mkComment('h1')])
    await bar()
    fireEvent.click(submit())
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByText('1 comment to send to this chat')).toBeInTheDocument())
    expect(localStorage.getItem(SENT_KEY)).toBeNull()
  })

  it('offers no bar when the artifact has no companion session', async () => {
    // "Ask agent to address" is the affordance there, and it states what it does.
    renderPage([mkComment('h1')], { bound: false })
    await waitFor(() => expect(screen.getByRole('button', { name: /Ask agent to address/ })).toBeInTheDocument())
    expect(screen.queryByText(/to send to this chat/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Submit$/ })).not.toBeInTheDocument()
  })

  it('keeps the comments pending when the send times out', async () => {
    // The 10s abort deadline means no receipt arrived, not that the turn ran.
    // Marking the batch sent there would drop a review for good: the sent set is
    // append-only and no UI clears it. A duplicate turn the user chooses is the
    // cheaper mistake.
    const abort = Object.assign(new Error('aborted'), { name: 'AbortError' })
    vi.mocked(api).sendChat = vi.fn().mockImplementation(() => Promise.reject(
      // A DOMException-shaped AbortError, which is what a real abort raises.
      typeof DOMException === 'function' ? new DOMException('aborted', 'AbortError') : abort,
    ))
    renderPage([mkComment('h1')])
    await bar()
    fireEvent.click(submit())
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByText('1 comment to send to this chat')).toBeInTheDocument())
    expect(localStorage.getItem(SENT_KEY)).toBeNull()
  })

  it('marks the batch sent when the server queued it', async () => {
    // A queue entry is custody taken, so the batch must not be re-offered.
    vi.mocked(api).sendChat = vi.fn().mockResolvedValue({
      ok: true, json: () => Promise.resolve({ ok: true, queued: true, queue_id: 'q1' }),
    })
    renderPage([mkComment('h1')])
    await bar()
    fireEvent.click(submit())
    await waitFor(() => expect(JSON.parse(localStorage.getItem(SENT_KEY) || '[]')).toEqual(['h1']))
  })

  it('disables Submit while the gateway is offline', async () => {
    renderPage([mkComment('h1')], { connected: false })
    await bar()
    const btn = screen.getByLabelText('Submit disabled — gateway offline')
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
  })
})
