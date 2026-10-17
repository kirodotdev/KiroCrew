import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '../../api/apiError'
import { threadDrafts, threadLiveStore, threadPendingReplies } from '../../state/threadLiveStore'

const mockDetail = vi.fn()
const mockReply = vi.fn()

vi.mock('../../api/threads', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/threads')>()
  return {
    ...actual,
    threadsApi: {
      summary: vi.fn(),
      detail: (...args: unknown[]) => mockDetail(...args),
      reply: (...args: unknown[]) => mockReply(...args),
    },
  }
})

// The markdown renderer pulls in the whole highlight/mermaid stack; the thread
// panel's contract is the rows and the composer, not markdown rendering.
vi.mock('../../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))

import ThreadPanel from './ThreadPanel'

const PARENT = { mid: 'm-1', role: 'assistant', content: 'Overnight triage: 9 new issues.', ts: '2026-09-22T07:02:00Z' }
const REPLIES = [
  { id: 'r1', role: 'user', content: 'What about the other 8?', ts: '2026-09-22T07:41:00Z' },
  { id: 'r2', role: 'assistant', content: '5 are covered by open PRs.', ts: '2026-09-22T07:41:30Z' },
  { id: 'r3', role: 'assistant', content: '3 are queued for Fixer.', ts: '2026-09-22T07:41:40Z' },
]

let qc: QueryClient
const renderPanel = (onClose = vi.fn()) =>
  render(
    <QueryClientProvider client={qc}>
      <ThreadPanel slot="member-radar" mid="m-1" crewmateName="Radar" onClose={onClose} />
    </QueryClientProvider>,
  )

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  mockDetail.mockResolvedValue({ parent: PARENT, replies: REPLIES, in_flight: false })
  mockReply.mockResolvedValue({ reply: { id: 'r4', role: 'user', content: 'ok', ts: '2026-09-22T07:44:00Z' }, run_id: 'run-1' })
  threadLiveStore.clearError('member-radar', 'm-1')
  threadDrafts.set('member-radar', 'm-1', '')
  threadPendingReplies.set('member-radar', 'm-1', null)
})

describe('ThreadPanel', () => {
  it('renders the display label on author lines and the header while the name keeps seeding', async () => {
    // The evidence for the label on this surface (the stub screenshot harness
    // cannot fake a live thread transcript): a crew with a display_name shows
    // the LABEL wherever the panel names the speaker, and the immutable name
    // appears nowhere as text — it survives only as the avatar seed.
    render(
      <QueryClientProvider client={qc}>
        <ThreadPanel slot="member-radar" mid="m-1" crewmateName="radar" crewmateLabel="Radar Watch" onClose={vi.fn()} />
      </QueryClientProvider>,
    )
    await screen.findByTestId('thread-parent')
    // Header chip + parent author line + the crewmate run's author line.
    expect(screen.getAllByText('Radar Watch').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText('radar')).not.toBeInTheDocument()
  })

  it('quotes the parent, counts the replies and groups the crewmate run', async () => {
    renderPanel()
    expect(screen.getByRole('complementary', { name: 'Thread' })).toBeInTheDocument()
    await screen.findByTestId('thread-parent')
    expect(screen.getByTestId('thread-parent-bubble')).toHaveTextContent('Overnight triage')
    expect(screen.getByTestId('thread-reply-count')).toHaveTextContent('3 replies')
    const rows = screen.getAllByTestId('thread-reply')
    expect(rows.map((r) => r.getAttribute('data-reply-from'))).toEqual(['user', 'assistant', 'assistant'])
    // Two consecutive crewmate replies within the gap share one run (start,
    // end) on the main chat's rule; the user's row carries no run.
    expect(rows.map((r) => r.getAttribute('data-thread-run'))).toEqual([null, 'start', 'end'])
    // The crewmate's name heads its run once; the user's row says "You".
    expect(screen.getAllByText('Radar').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('You')).toBeInTheDocument()
  })

  it('sends a reply on Enter, clears the draft and disables send while the crewmate replies', async () => {
    renderPanel()
    await screen.findByTestId('thread-parent')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '  Is #4198 among them?  ' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mockReply).toHaveBeenCalledWith('member-radar', 'm-1', 'Is #4198 among them?', expect.stringMatching(/^[0-9a-f]{32}$/)))
    await waitFor(() => expect(box.value).toBe(''))
    // The server now reports the crewmate replying: typing row, send disabled.
    mockDetail.mockResolvedValue({ parent: PARENT, replies: REPLIES, in_flight: true })
    await act(async () => { await qc.invalidateQueries() })
    await screen.findByTestId('thread-replying')
    fireEvent.change(box, { target: { value: 'again' } })
    expect(screen.getByRole('button', { name: 'Send reply' })).toBeDisabled()
  })

  it('streams the crewmate reply from the live store', async () => {
    renderPanel()
    await screen.findByTestId('thread-parent')
    act(() => {
      threadLiveStore.apply({ slot: 'member-radar', mid: 'm-1', run_id: 'run-9', role: 'assistant', content: 'Yes, #4198 ' })
      threadLiveStore.apply({ slot: 'member-radar', mid: 'm-1', run_id: 'run-9', role: 'assistant', content: 'is covered.' })
    })
    expect(screen.getByTestId('thread-reply-live')).toHaveTextContent('Yes, #4198 is covered.')
    act(() => {
      threadLiveStore.apply({ slot: 'member-radar', mid: 'm-1', run_id: 'run-9', role: 'assistant', content: 'Yes, #4198 is covered.', final: true })
    })
    expect(screen.queryByTestId('thread-reply-live')).toBeNull()
  })

  it('names the failure in plain words and keeps the draft', async () => {
    mockReply.mockRejectedValue(new ApiError(409, 'x', JSON.stringify({ error: 'x', code: 'thread_turn_in_flight' })))
    renderPanel()
    await screen.findByTestId('thread-parent')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'one more' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send reply' }))
    await screen.findByTestId('thread-send-error')
    expect(screen.getByTestId('thread-send-error')).toHaveTextContent('Radar is still replying. Wait for that reply.')
    expect(box.value).toBe('one more')
  })

  it('a full chat sidecar points at a new chat, not at a retry', async () => {
    mockReply.mockRejectedValue(new ApiError(409, 'x', JSON.stringify({ error: 'x', code: 'threads_full' })))
    renderPanel()
    await screen.findByTestId('thread-parent')
    fireEvent.change(screen.getByTestId('thread-composer'), { target: { value: 'one more' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send reply' }))
    await screen.findByTestId('thread-send-error')
    expect(screen.getByTestId('thread-send-error')).toHaveTextContent('Start a new chat to keep discussing.')
  })

  it('measures the draft in UTF-8 bytes, not characters', async () => {
    renderPanel()
    await screen.findByTestId('thread-parent')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    // 11,000 three-byte characters: far under a 32,000-character cap, over 32 KiB.
    fireEvent.change(box, { target: { value: '话'.repeat(11_000) } })
    expect(screen.getByRole('button', { name: 'Send reply' })).toBeDisabled()
    // A validation hint, said as muted text -- not an ErrorNotice: nothing failed.
    expect(screen.getByTestId('thread-too-long')).toHaveTextContent('too long')
    expect(screen.queryByTestId('thread-send-error')).toBeNull()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(mockReply).not.toHaveBeenCalled()
    fireEvent.change(box, { target: { value: '话'.repeat(10_000) } })
    expect(screen.getByRole('button', { name: 'Send reply' })).toBeEnabled()
    expect(screen.queryByTestId('thread-too-long')).toBeNull()
  })

  it('close hands the panel back', async () => {
    const onClose = vi.fn()
    renderPanel(onClose)
    fireEvent.click(screen.getByRole('button', { name: 'Close thread' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Escape closes the panel and focus returns to the opener on unmount', async () => {
    const opener = document.createElement('button')
    opener.textContent = 'Reply in thread'
    document.body.appendChild(opener)
    opener.focus()
    const onClose = vi.fn()
    const view = renderPanel(onClose)
    await screen.findByTestId('thread-parent')
    // The composer took focus on open.
    expect(document.activeElement).toBe(screen.getByTestId('thread-composer'))
    fireEvent.keyDown(screen.getByTestId('thread-composer'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    view.unmount()
    expect(document.activeElement).toBe(opener)
    opener.remove()
  })

  it('keeps the draft across close and reopen, and a sent reply clears it', async () => {
    const first = renderPanel()
    await screen.findByTestId('thread-parent')
    fireEvent.change(screen.getByTestId('thread-composer'), { target: { value: 'half a thought' } })
    first.unmount()
    expect(threadDrafts.get('member-radar', 'm-1')).toBe('half a thought')
    renderPanel()
    await screen.findByTestId('thread-parent')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    expect(box.value).toBe('half a thought')
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mockReply).toHaveBeenCalledWith('member-radar', 'm-1', 'half a thought', expect.stringMatching(/^[0-9a-f]{32}$/)))
    await waitFor(() => expect(box.value).toBe(''))
    expect(threadDrafts.get('member-radar', 'm-1')).toBe('')
  })

  it('a retry of the same text re-sends the same reply id; an edited text gets a new one', async () => {
    mockReply.mockRejectedValueOnce(new ApiError(500, 'boom', ''))
    renderPanel()
    await screen.findByTestId('thread-parent')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'Is #4198 among them?' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await screen.findByTestId('thread-send-error')
    // The draft is kept; sending it again is the SAME reply to the server.
    expect(box.value).toBe('Is #4198 among them?')
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mockReply).toHaveBeenCalledTimes(2))
    const [first, second] = mockReply.mock.calls as [unknown, unknown, string, string][]
    expect(first[3]).toMatch(/^[0-9a-f]{32}$/)
    expect(second[3]).toBe(first[3])
    await waitFor(() => expect(box.value).toBe(''))
    // A different text is a different reply.
    fireEvent.change(box, { target: { value: 'Something else' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mockReply).toHaveBeenCalledTimes(3))
    expect((mockReply.mock.calls[2] as string[])[3]).not.toBe(first[3])
  })

  it('words typed while a send is in flight survive its success', async () => {
    let settle: (v: unknown) => void = () => {}
    mockReply.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
    renderPanel()
    await screen.findByTestId('thread-parent')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'first' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mockReply).toHaveBeenCalledTimes(1))
    // The composer stays live during the send; the next reply is being typed.
    fireEvent.change(box, { target: { value: 'second, still typing' } })
    settle({ reply: { id: 'r4', role: 'user', content: 'first', ts: '2026-09-22T07:44:00Z' }, run_id: 'run-1' })
    await waitFor(() => expect(threadDrafts.get('member-radar', 'm-1')).toBe('second, still typing'))
    expect(box.value).toBe('second, still typing')
  })

  it('a retry after close and reopen still carries the failed send\'s reply id', async () => {
    // A 202 lost on the wire: the reply is stored server-side but the panel saw
    // a failure and kept the draft. Closing the panel must not forget the id, or
    // the later retry would append a second reply and start a second turn.
    mockReply.mockRejectedValueOnce(new ApiError(500, 'boom', ''))
    const first = renderPanel()
    await screen.findByTestId('thread-parent')
    fireEvent.change(screen.getByTestId('thread-composer'), { target: { value: 'Is #4198 among them?' } })
    fireEvent.keyDown(screen.getByTestId('thread-composer'), { key: 'Enter' })
    await screen.findByTestId('thread-send-error')
    first.unmount()
    renderPanel()
    await screen.findByTestId('thread-parent')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    expect(box.value).toBe('Is #4198 among them?')
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(mockReply).toHaveBeenCalledTimes(2))
    const [a, b] = mockReply.mock.calls as [unknown, unknown, string, string][]
    expect(b[3]).toBe(a[3])
    await waitFor(() => expect(box.value).toBe(''))
    expect(threadPendingReplies.get('member-radar', 'm-1')).toBeNull()
  })

  it('a thread that failed to load takes no reply until it is retried', async () => {
    mockDetail.mockRejectedValueOnce(new ApiError(500, 'boom', ''))
    threadDrafts.set('member-radar', 'm-1', 'kept draft')
    renderPanel()
    await screen.findByTestId('thread-load-error')
    const box = screen.getByTestId('thread-composer') as HTMLTextAreaElement
    expect(box.disabled).toBe(true)
    expect(box.placeholder).toBe('Press Retry to load the thread, then reply.')
    expect(box.value).toBe('kept draft')
    expect(screen.getByLabelText('Send reply')).toBeDisabled()
    // The disabled composer cannot hold focus; the keyboard lands on Retry so
    // Escape still reaches the panel.
    expect(document.activeElement).toBe(screen.getByTestId('thread-load-retry'))
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(mockReply).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('thread-load-retry'))
    await screen.findByTestId('thread-parent')
    expect(box.disabled).toBe(false)
    expect(box.placeholder).toBe('Reply in thread…')
    expect(screen.getByLabelText('Send reply')).not.toBeDisabled()
  })

  it('a failed load shows the notice with a Retry that re-runs the read', async () => {
    mockDetail.mockRejectedValueOnce(new ApiError(500, 'boom', ''))
    renderPanel()
    await screen.findByTestId('thread-load-error')
    expect(screen.getByTestId('thread-load-error')).toHaveTextContent("Couldn't load this thread.")
    fireEvent.click(screen.getByTestId('thread-load-retry'))
    await screen.findByTestId('thread-parent')
    expect(screen.queryByTestId('thread-load-error')).toBeNull()
    expect(mockDetail).toHaveBeenCalledTimes(2)
  })
})
