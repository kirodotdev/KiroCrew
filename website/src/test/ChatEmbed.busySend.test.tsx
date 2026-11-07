/**
 * While a turn runs, the embed's send follows the person's busy-send choice,
 * the one the dashboard composer reads: the slot's own split-button choice,
 * else the Settings → Chat default (shipped as Steer). A steer asks the gateway
 * to put the message into the running turn, so the button says "Steer" and the
 * message shows as a transcript row. Queue keeps the queue card it always had.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatMessage } from '../types'
import { BUSY_SEND_MODE_LS_KEY, setBusySendDefault } from '../components/BusySendButton'
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

const earlier: ChatMessage[] = [
  { role: 'user', content: 'first', cls: 'user', ts: '2026-01-01T00:00:00Z' },
  { role: 'assistant', content: 'working on it', cls: 'assistant', ts: '2026-01-01T00:00:01Z' },
]
const busy = (extra: Record<string, unknown> = {}) => ({ messages: earlier, queue: [], running: true, title: '', ...extra })

const rows = () => Array.from(screen.getByTestId('rows').querySelectorAll('li'))
  .map(li => `${li.getAttribute('data-role')}${li.getAttribute('data-optimistic') === 'true' ? '*' : ''}:${li.textContent}`)
const sendControl = () => document.querySelector<HTMLButtonElement>('[data-composer-send]')!
/** The body of the embed's last POST to /api/chat. */
const lastBody = () => mockPost.mock.calls.at(-1)?.[1] as Record<string, unknown>
const settle = () => act(async () => { await vi.advanceTimersByTimeAsync(50) })

async function mount(props: Partial<React.ComponentProps<typeof ChatEmbed>> = {}) {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" {...props} />
      </QueryClientProvider>,
    )
  })
  await settle()
}

async function type(text: string) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: text } })
  })
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  vi.useFakeTimers()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  Element.prototype.scrollIntoView = vi.fn()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('ChatEmbed busy send', () => {
  it('steers by default: the button says Steer, the POST asks to steer, and the send is a transcript row', async () => {
    mockGet.mockResolvedValue(busy())
    mockPost.mockResolvedValue({ ok: true, steered: true })
    await mount()
    await type('go left')
    expect(sendControl()).toHaveAttribute('data-composer-send', 'steer')
    expect(sendControl()).toHaveAccessibleName('Steer')
    await act(async () => { fireEvent.click(sendControl()) })
    await settle()
    expect(mockPost).toHaveBeenCalledWith('/api/chat?ws=1', expect.objectContaining({ message: 'go left', steer: true }))
    expect(rows()).toEqual(['user:first', 'assistant:working on it', 'user*:go left'])
    expect(screen.queryByTestId('queue-card')).toBeNull()
  })

  it('Enter steers the same way the button does', async () => {
    mockGet.mockResolvedValue(busy())
    mockPost.mockResolvedValue({ ok: true, steered: true })
    await mount()
    await type('go left')
    await act(async () => { fireEvent.keyDown(screen.getByLabelText('Chat message'), { key: 'Enter' }) })
    await settle()
    expect(lastBody()).toMatchObject({ message: 'go left', steer: true })
  })

  it('the Settings default Queue keeps Queue and sends no steer flag', async () => {
    setBusySendDefault('queue')
    mockGet.mockResolvedValue(busy())
    mockPost.mockResolvedValue({ ok: true, queued: true, queue_id: 'q1' })
    await mount()
    await type('later please')
    expect(sendControl()).toHaveAccessibleName('Queue message')
    await act(async () => { fireEvent.click(sendControl()) })
    await settle()
    expect(lastBody()).toMatchObject({ message: 'later please' })
    expect(lastBody()).not.toHaveProperty('steer')
    expect(screen.getAllByTestId('queue-card')).toHaveLength(1)
  })

  it("the slot's own choice wins over the Settings default, either way", async () => {
    setBusySendDefault('steer')
    localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:slot-1`, 'queue')
    mockGet.mockResolvedValue(busy())
    await mount()
    await type('x')
    expect(sendControl()).toHaveAttribute('data-composer-send', 'queue')
  })

  it("a slot whose own choice is Steer steers under a Queue default", async () => {
    setBusySendDefault('queue')
    localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:slot-1`, 'steer')
    mockGet.mockResolvedValue(busy())
    await mount()
    await type('x')
    expect(sendControl()).toHaveAttribute('data-composer-send', 'steer')
  })

  it('the Auto mode steers, because the embed cannot offer the gateway decision', async () => {
    localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:slot-1`, 'auto')
    mockGet.mockResolvedValue(busy())
    mockPost.mockResolvedValue({ ok: true, steered: true })
    await mount()
    await type('x')
    expect(sendControl()).toHaveAttribute('data-composer-send', 'steer')
    await act(async () => { fireEvent.click(sendControl()) })
    expect(lastBody()).toMatchObject({ steer: true })
  })

  it('a steer the gateway queues instead moves to the queue strip', async () => {
    mockGet.mockResolvedValue(busy())
    mockPost.mockResolvedValue({ ok: true, queued: true, queue_id: 'q7' })
    await mount()
    await type('go left')
    await act(async () => { fireEvent.click(sendControl()) })
    await settle()
    expect(lastBody()).toMatchObject({ steer: true })
    const cards = screen.getAllByTestId('queue-card')
    expect(cards).toHaveLength(1)
    expect(cards[0]).toHaveTextContent('go left')
    expect(rows()).toEqual(['user:first', 'assistant:working on it'])
  })

  it('a stop under way keeps Queue, as in the main composer', async () => {
    mockGet.mockResolvedValue(busy({ stopping: true, stop_state: 'soft_pending' }))
    await mount()
    await type('x')
    expect(sendControl()).toHaveAttribute('data-composer-send', 'queue')
  })

  it('an idle slot sends plainly, with no steer flag', async () => {
    mockGet.mockResolvedValue({ messages: earlier, running: false, title: '' })
    mockPost.mockResolvedValue({ ok: true })
    await mount()
    await type('hello')
    expect(sendControl()).toHaveAccessibleName('Send message')
    await act(async () => { fireEvent.click(sendControl()) })
    expect(lastBody()).not.toHaveProperty('steer')
  })

  it("a host onSend keeps Queue: it posts on the host's own endpoint", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined)
    mockGet.mockResolvedValue(busy())
    await mount({ onSend })
    await type('x')
    expect(sendControl()).toHaveAttribute('data-composer-send', 'queue')
    await act(async () => { fireEvent.click(sendControl()) })
    expect(onSend).toHaveBeenCalledWith('x', expect.any(Object))
    expect(mockPost).not.toHaveBeenCalledWith('/api/chat?ws=1', expect.anything())
  })
})
