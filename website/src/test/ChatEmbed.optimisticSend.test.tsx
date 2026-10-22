/**
 * ChatEmbed shows the user's own message the moment it is sent, as an
 * optimistic row, and swaps it for the server's copy once a poll returns THIS
 * send by its `sendId`. Before this the message was missing until the next
 * poll (up to 5 s idle). A send whose text goes back to the composer (refused
 * or unconfirmed) drops the row, so the user never sees two copies.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatMessage } from '../types'
import { ApiError } from '../api/apiError'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList({ messages }: { messages: ChatMessage[] }, ref: React.Ref<unknown>) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    return (
      <ol data-testid="rows">
        {messages.map((m, i) => (
          <li key={i} data-role={m.role} data-optimistic={String(!!m.meta?.optimistic)}>{m.content}</li>
        ))}
      </ol>
    )
  }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

const rows = () => Array.from(screen.getByTestId('rows').querySelectorAll('li'))
  .map(li => `${li.getAttribute('data-role')}${li.getAttribute('data-optimistic') === 'true' ? '*' : ''}:${li.textContent}`)

/** The sendId the embed stamped on its last POST. */
const lastSendId = () => (mockPost.mock.calls.at(-1)?.[1] as { meta?: { sendId?: string } })?.meta?.sendId

async function mount(props: Partial<React.ComponentProps<typeof ChatEmbed>> = {}) {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" {...props} />
      </QueryClientProvider>,
    )
  })
}

/** Types `text` and presses the composer's send control. On a busy slot that
 *  control is the Send atom's queue state, named "Queue message", so a send
 *  that is meant to wait in the queue says `{ queue: true }` and the test
 *  also checks the slot offered the queue control rather than a plain send. */
async function sendText(text: string, { queue = false }: { queue?: boolean } = {}) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: text } })
  })
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: queue ? 'Queue message' : 'Send message' }))
  })
}

const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(50) })
const nextPoll = () => act(async () => { await vi.advanceTimersByTimeAsync(5100) })

const earlier: ChatMessage[] = [
  { role: 'user', content: 'first', cls: 'user', ts: '2026-01-01T00:00:00Z' },
  { role: 'assistant', content: 'reply', cls: 'assistant', ts: '2026-01-01T00:00:01Z' },
]

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: earlier, running: false, title: '' })
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ChatEmbed optimistic own message', () => {
  it('shows the sent message at once, before any poll returns it', async () => {
    mockPost.mockReturnValue(new Promise(() => {}))
    await mount()
    await sendText('hello there')
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user*:hello there'])
  })

  it('replaces the optimistic row with the server copy carrying its sendId, without a duplicate', async () => {
    mockPost.mockResolvedValue({ ok: true })
    await mount()
    await sendText('hello there')
    mockGet.mockResolvedValue({
      messages: [...earlier, { role: 'user', content: 'hello there', cls: 'user', meta: { sendId: lastSendId() } }],
      running: true,
      title: '',
    })
    await nextPoll()
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user:hello there'])
  })

  it('keeps the optimistic row while the poll has only some other user row', async () => {
    // A same-text row under another id (a cron, another surface) is not this send.
    mockPost.mockResolvedValue({ ok: true })
    await mount()
    await sendText('hello there')
    mockGet.mockResolvedValue({
      messages: [...earlier, { role: 'user', content: 'hello there', cls: 'user', meta: { sendId: 'someone-else' } }],
      running: true,
      title: '',
    })
    await nextPoll()
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user:hello there', 'user*:hello there'])
  })

  it('on a busy slot shows the send as one queue card, which the server entry takes over in place', async () => {
    mockGet.mockResolvedValue({ messages: earlier, queue: [], running: true, title: '' })
    let resolvePost: (v: unknown) => void = () => {}
    mockPost.mockReturnValue(new Promise(r => { resolvePost = r }))
    await mount()
    await sendText('later please', { queue: true })
    // Shown at once, in the queue strip only -- never in the transcript.
    expect(rows()).toEqual(['user:first', 'assistant:reply'])
    const card = screen.getByTestId('queue-card')
    expect(card).toHaveTextContent('later please')
    mockGet.mockResolvedValue({
      messages: earlier,
      queue: [{ id: 'q9', content: 'later please' }],
      running: true,
      title: '',
    })
    await act(async () => { resolvePost({ ok: true, queued: true, queue_id: 'q9' }) })
    await nextPoll()
    // Still exactly one card, and the same element: it did not leave and re-enter.
    expect(screen.getAllByTestId('queue-card')).toHaveLength(1)
    expect(screen.getByTestId('queue-card')).toBe(card)
    expect(rows()).toEqual(['user:first', 'assistant:reply'])
  })

  it('keeps both copies when a second send goes out before the poll shows the first', async () => {
    mockPost.mockResolvedValue({ ok: true })
    await mount()
    await sendText('one')
    await settle()
    const firstId = lastSendId()
    await sendText('two')
    await settle()
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user*:one', 'user*:two'])
    // The poll shows only the first: its copy is replaced, the second stays.
    mockGet.mockResolvedValue({
      messages: [...earlier, { role: 'user', content: 'one', cls: 'user', meta: { sendId: firstId } }],
      running: true,
      title: '',
    })
    await nextPoll()
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user:one', 'user*:two'])
  })

  it('keeps both queue cards in place when two busy-slot sends go out before the poll shows either', async () => {
    mockGet.mockResolvedValue({ messages: earlier, queue: [], running: true, title: '' })
    mockPost.mockResolvedValueOnce({ ok: true, queued: true, queue_id: 'q1' })
      .mockResolvedValueOnce({ ok: true, queued: true, queue_id: 'q2' })
    await mount()
    await sendText('one', { queue: true })
    await settle()
    const cardOne = screen.getByTestId('queue-card')
    await sendText('two', { queue: true })
    await settle()
    // The poll then shows only the first entry. Both cards stay, and the first
    // is the same element it was before the second send: it never left.
    mockGet.mockResolvedValue({ messages: earlier, queue: [{ id: 'q1', content: 'one' }], running: true, title: '' })
    await nextPoll()
    const cards = screen.getAllByTestId('queue-card')
    expect(cards).toHaveLength(2)
    expect(cards[0]).toBe(cardOne)
    expect(cards[1]).toHaveTextContent('two')
  })

  it('does not bring the card back once the server entry has been seen and leaves', async () => {
    // The entry is cancelled from another surface: the poll then has neither
    // the entry nor a row, and the optimistic card must not reappear.
    mockGet.mockResolvedValue({ messages: earlier, queue: [], running: true, title: '' })
    mockPost.mockResolvedValue({ ok: true, queued: true, queue_id: 'q9' })
    await mount()
    mockGet.mockResolvedValue({ messages: earlier, queue: [{ id: 'q9', content: 'later please' }], running: true, title: '' })
    await sendText('later please', { queue: true })
    await nextPoll()
    expect(screen.getAllByTestId('queue-card')).toHaveLength(1)
    mockGet.mockResolvedValue({ messages: earlier, queue: [], running: true, title: '' })
    await nextPoll()
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(screen.queryAllByTestId('queue-card')).toHaveLength(0)
  })

  it('moves to the queue strip when an idle-looking slot answers queued', async () => {
    let resolvePost: (v: unknown) => void = () => {}
    mockPost.mockReturnValue(new Promise(r => { resolvePost = r }))
    await mount()
    await sendText('later please')
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user*:later please'])
    await act(async () => { resolvePost({ ok: true, queued: true, queue_id: 'q9' }) })
    await settle()
    expect(rows()).toEqual(['user:first', 'assistant:reply'])
    expect(screen.getByTestId('queue-card')).toHaveTextContent('later please')
  })

  it('drops the optimistic row when the send is refused, leaving only the error row', async () => {
    mockPost.mockRejectedValue(new ApiError(409, 'API 409: busy', JSON.stringify({ error: 'busy' })))
    await mount()
    await sendText('hello there')
    await settle()
    expect(rows().filter(r => r.startsWith('user'))).toEqual(['user:first'])
    expect(rows().some(r => r.startsWith('error:'))).toBe(true)
    expect((screen.getByLabelText('Chat message') as HTMLTextAreaElement).value).toBe('hello there')
  })

  it('drops the optimistic row when delivery cannot be confirmed and the text comes back', async () => {
    mockPost.mockRejectedValue(new TypeError('Failed to fetch'))
    await mount()
    await sendText('hello there')
    await settle()
    expect(rows().filter(r => r.startsWith('user'))).toEqual(['user:first'])
    expect(rows().some(r => r.startsWith('notice:'))).toBe(true)
  })

  it('shows the row for a host onSend too', async () => {
    const onSend = vi.fn(() => new Promise<void>(() => {}))
    await mount({ onSend })
    await sendText('via host')
    expect(onSend).toHaveBeenCalledWith('via host', { sendId: expect.any(String) })
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user*:via host'])
  })

  it('a host onSend row is replaced by a new same-text user row, without a duplicate', async () => {
    const onSend = vi.fn(() => Promise.resolve())
    await mount({ onSend })
    await sendText('first')
    mockGet.mockResolvedValue({
      messages: [...earlier, { role: 'user', content: 'first', cls: 'user', ts: '2026-01-01T00:00:05Z' }],
      running: true,
      title: '',
    })
    await nextPoll()
    expect(rows()).toEqual(['user:first', 'assistant:reply', 'user:first'])
  })
})
