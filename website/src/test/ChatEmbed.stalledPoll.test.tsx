/**
 * ChatEmbed's failing poll on a page already shown: a short outage stays
 * silent, a long one flags the page as stale (no longer "running") with a
 * retry, and the failing poll backs off instead of hitting the endpoint every
 * second forever.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { ChatMessage } from '../types'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList(
    { running, messages }: { running: boolean; messages: ChatMessage[] },
    ref: React.Ref<unknown>,
  ) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    return (
      <div
        data-testid="chat-message-list"
        data-running={String(running)}
        data-tail-role={messages[messages.length - 1]?.role ?? ''}
        data-rows={messages.map(m => `${m.role}${m.meta?.optimistic ? '*' : ''}:${m.content}`).join('|')}
      />
    )
  }),
}))

import ChatEmbed, { EMBED_STALL_MS, EMBED_STALL_POLL_MAX_MS, embedOutageMs, embedPollInterval } from '../app-sdk/ChatEmbed'

const RUNNING_PAGE = {
  messages: [{ role: 'user', content: 'hi', ts: 1 }],
  running: true,
  title: 'Demo',
  has_more: false,
}

let queryClient: QueryClient

async function mount() {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" />
      </QueryClientProvider>,
    )
  })
  await act(async () => { await vi.advanceTimersByTimeAsync(100) })
}

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  mockPost.mockResolvedValue({})
  Element.prototype.scrollIntoView = vi.fn()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-13T12:00:00Z'))
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  queryClient.clear()
  vi.useRealTimers()
})

describe('ChatEmbed stalled poll', () => {
  it('keeps a single failed poll silent', async () => {
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValueOnce(new Error('blip')).mockResolvedValue(RUNNING_PAGE)
    await mount()
    await advance(1000)
    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('chat-embed-stalled')).toBeNull()
    expect(screen.getByText('streaming')).toBeInTheDocument()
  })

  it('flags a page whose poll keeps failing as stale, not running', async () => {
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValue(new Error('gateway down'))
    await mount()
    expect(screen.getByText('streaming')).toBeInTheDocument()
    await advance(EMBED_STALL_MS + 2000)
    expect(screen.getByTestId('chat-embed-stalled')).toBeInTheDocument()
    expect(screen.queryByText('streaming')).toBeNull()
    expect(screen.getByTestId('chat-message-list').dataset.running).toBe('false')
  })

  it('backs the failing poll off instead of polling every second', async () => {
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValue(new Error('gateway down'))
    await mount()
    await advance(60_000)
    // At the 1s running cadence a minute of outage would be ~60 reads.
    expect(mockGet.mock.calls.length).toBeLessThan(20)
  })

  it('drops the live caret: a stale half-written reply renders as settled', async () => {
    const page = { ...RUNNING_PAGE, messages: [...RUNNING_PAGE.messages, { role: 'streaming', content: 'part', ts: 2 }] }
    mockGet.mockResolvedValueOnce(page).mockRejectedValue(new Error('gateway down'))
    await mount()
    expect(screen.getByTestId('chat-message-list').dataset.tailRole).toBe('streaming')
    await advance(EMBED_STALL_MS + 2000)
    expect(screen.getByTestId('chat-message-list').dataset.tailRole).toBe('assistant')
  })

  it('times the outage from its first failure, not from a success long before', async () => {
    // The poll pauses while the tab is hidden, so the last success can be far
    // older than the first failure; one failed read on return is not a stall.
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValueOnce(new Error('blip')).mockResolvedValue(RUNNING_PAGE)
    await mount()
    vi.setSystemTime(Date.now() + 10 * 60_000)
    await advance(1000)
    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('chat-embed-stalled')).toBeNull()
    expect(screen.getByText('streaming')).toBeInTheDocument()
    // ...and the next read goes out on the running cadence, not the ceiling.
    await advance(1000)
    expect(mockGet).toHaveBeenCalledTimes(3)
  })

  it('mutes the header dot while stalled instead of showing it idle', async () => {
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValue(new Error('gateway down'))
    await mount()
    const dot = () => document.querySelector('span.rounded-full') as HTMLElement
    expect(dot().className).toContain('bg-ok')
    await advance(EMBED_STALL_MS + 2000)
    expect(dot().className).toContain('bg-muted')
    expect(dot().className).not.toContain('bg-accent')
    expect(dot().className).not.toContain('bg-ok')
  })

  it('marks Reconnect busy while its read is in flight', async () => {
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValue(new Error('gateway down'))
    await mount()
    await advance(EMBED_STALL_MS + 2000)
    const button = screen.getByRole('button', { name: 'Reconnect' })
    expect(button).not.toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'false')
    let fail: (e: Error) => void = () => {}
    mockGet.mockReturnValueOnce(new Promise((_, reject) => { fail = reject }))
    await act(async () => { fireEvent.click(button) })
    // React Query batches its notifications on a zero-delay timer.
    await advance(10)
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'true')
    await act(async () => { fail(new Error('still down')) })
    await advance(50)
    expect(button).not.toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'false')
  })

  it('announces the notice again when a Reconnect read fails', async () => {
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValue(new Error('gateway down'))
    await mount()
    await advance(EMBED_STALL_MS + 2000)
    const before = screen.getByTestId('chat-embed-stalled')
    const callsBefore = mockGet.mock.calls.length
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Reconnect' })) })
    await advance(50)
    expect(mockGet.mock.calls.length).toBe(callsBefore + 1)
    const after = screen.getByTestId('chat-embed-stalled')
    // A fresh role="alert" node is what makes a screen reader read it again.
    expect(after).not.toBe(before)
    expect(after).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('button', { name: 'Reconnect' })).not.toBeDisabled()
  })

  it('keeps an optimistic send after the settled stale reply, and sending stays enabled', async () => {
    const page = { ...RUNNING_PAGE, messages: [...RUNNING_PAGE.messages, { role: 'streaming', content: 'part', ts: 2 }] }
    mockGet.mockResolvedValueOnce(page).mockRejectedValue(new Error('gateway down'))
    mockPost.mockReturnValue(new Promise(() => {}))
    await mount()
    await advance(EMBED_STALL_MS + 2000)
    expect(screen.getByTestId('chat-embed-stalled')).toBeInTheDocument()
    await act(async () => {
      fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'are you there' } })
    })
    const send = screen.getByLabelText('Send message')
    expect(send).not.toBeDisabled()
    await act(async () => { fireEvent.click(send) })
    expect(mockPost).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('chat-message-list').dataset.rows)
      .toBe('user:hi|assistant:part|user*:are you there')
  })

  it('clears the notice when Reconnect succeeds', async () => {
    mockGet.mockResolvedValueOnce(RUNNING_PAGE).mockRejectedValue(new Error('gateway down'))
    await mount()
    await advance(EMBED_STALL_MS + 2000)
    expect(screen.getByTestId('chat-embed-stalled')).toBeInTheDocument()
    mockGet.mockResolvedValue({ ...RUNNING_PAGE, running: false })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Reconnect' })) })
    await advance(100)
    expect(screen.queryByTestId('chat-embed-stalled')).toBeNull()
  })
})

describe('embedPollInterval', () => {
  const ok = (running: boolean) => ({ status: 'success', data: { running }, dataUpdatedAt: 0, errorUpdatedAt: 0 })
  const failing = { status: 'error', data: { running: true }, dataUpdatedAt: 0, errorUpdatedAt: 0 }

  it('uses the running and idle cadence while healthy', () => {
    expect(embedPollInterval(ok(true), 0)).toBe(1000)
    expect(embedPollInterval(ok(false), 0)).toBe(5000)
  })

  it('keeps the cadence until the stall, then waits as long as the outage, up to the ceiling', () => {
    expect(embedPollInterval(failing, 500)).toBe(1000)
    expect(embedPollInterval(failing, EMBED_STALL_MS - 1)).toBe(1000)
    expect(embedPollInterval(failing, 12_000)).toBe(12_000)
    expect(embedPollInterval(failing, 600_000)).toBe(EMBED_STALL_POLL_MAX_MS)
  })
})

describe('embedOutageMs', () => {
  const err = (dataUpdatedAt: number, errorUpdatedAt: number) => ({ status: 'error', dataUpdatedAt, errorUpdatedAt })

  it('starts at zero for a key that never succeeded', () => {
    const runs = new Map()
    expect(embedOutageMs(runs, 'k', err(0, 1_791_900_000_000))).toBe(0)
    expect(embedOutageMs(runs, 'k', err(0, 1_791_900_004_000))).toBe(4000)
  })

  it('measures from the first failure of the run and resets on success', () => {
    const runs = new Map()
    expect(embedOutageMs(runs, 'k', err(1000, 600_000))).toBe(0)
    expect(embedOutageMs(runs, 'k', err(1000, 615_000))).toBe(15_000)
    expect(embedOutageMs(runs, 'k', { status: 'success', dataUpdatedAt: 616_000, errorUpdatedAt: 615_000 })).toBe(0)
    expect(embedOutageMs(runs, 'k', err(616_000, 620_000))).toBe(0)
  })
})
