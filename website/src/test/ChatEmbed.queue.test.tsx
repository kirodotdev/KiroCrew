import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The embed reads the slot-detail `queue` field and shows each waiting send as
// a read-only queue card above the composer. Only the transport and the
// virtualized list are stubbed; QueueStack renders for real.
const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: () => <div data-testid="chat-message-list" />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

let queryClient: QueryClient

async function mount() {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" />
      </QueryClientProvider>,
    )
  })
  await act(async () => { vi.advanceTimersByTime(100) })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mockPost.mockResolvedValue({})
})

afterEach(() => {
  vi.useRealTimers()
})

const busy = (queue: unknown[]) => ({
  messages: [{ role: 'user', content: 'first', ts: 1 }],
  running: true,
  title: '',
  queue,
})

describe('ChatEmbed queued messages', () => {
  it('shows a queued send as a card above the composer', async () => {
    mockGet.mockResolvedValue(busy([{ id: 'q1', content: 'please also check the logs' }]))
    await mount()
    const cards = screen.getAllByTestId('queue-card')
    expect(cards).toHaveLength(1)
    expect(cards[0]).toHaveTextContent('please also check the logs')
    // Read-only: the embed wires no cancel/edit/interrupt.
    expect(screen.queryByRole('button', { name: /cancel queued message/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /edit queued message/i })).toBeNull()
  })

  it('shows every waiting entry and a count', async () => {
    mockGet.mockResolvedValue(busy([
      { id: 'q1', content: 'one' },
      { id: 'q2', content: 'two' },
    ]))
    await mount()
    expect(screen.getAllByTestId('queue-card')).toHaveLength(2)
    expect(screen.getByTestId('chat-embed-queue')).toHaveTextContent(/2\s*Queued/i)
  })

  it('folds a sub-agent delivery into the progress line, not a user card', async () => {
    mockGet.mockResolvedValue(busy([
      { id: 'q1', content: '[Subagent completion event] run abc finished' },
    ]))
    await mount()
    expect(screen.queryByTestId('queue-card')).toBeNull()
    expect(screen.getByTestId('subagent-delivery-progress')).toBeInTheDocument()
  })

  it('renders nothing when the queue is empty', async () => {
    mockGet.mockResolvedValue(busy([]))
    await mount()
    expect(screen.queryByTestId('chat-embed-queue')).toBeNull()
  })

  it('shows a send made while busy once the follow-up read returns it', async () => {
    mockGet.mockResolvedValue(busy([]))
    await mount()
    expect(screen.queryByTestId('queue-card')).toBeNull()
    // The busy slot queues the send and answers with a receipt; the embed's
    // settle refetch then carries the entry in `queue`.
    mockPost.mockResolvedValue({ ok: true, queued: true, queue_id: 'q9' })
    mockGet.mockResolvedValue(busy([{ id: 'q9', content: 'second thought' }]))
    fireEvent.change(screen.getByRole('textbox', { name: /chat message/i }), { target: { value: 'second thought' } })
    // While the turn runs, Send is the Send atom's queue state.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /queue message/i })) })
    await act(async () => { vi.advanceTimersByTime(100) })
    expect(screen.getByTestId('queue-card')).toHaveTextContent('second thought')
  })
})
