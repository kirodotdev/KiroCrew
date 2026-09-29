import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The embed's working indicator is the main chat's own ChatFooter, rendered
// for real here; only the transport and the virtualized list are stubbed.
const mockGet = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: vi.fn().mockResolvedValue({}) }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: ({ transcript }: { transcript?: { belowRows?: React.ReactNode } }) => (
    <div data-testid="chat-message-list">{transcript?.belowRows}</div>
  ),
}))

import ChatEmbed, { EMBED_STREAM_IDLE_MS } from '../app-sdk/ChatEmbed'
import { STREAM_IDLE_MS } from '../pages/chat/ChatFooter'

let queryClient: QueryClient

async function mount() {
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatEmbed slotKey="slot-1" />
      </QueryClientProvider>,
    )
  })
  // Let React Query's scheduled first read settle into the render.
  await act(async () => { vi.advanceTimersByTime(100) })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChatEmbed working indicator', () => {
  it('shows the main chat indicator while the agent works', async () => {
    mockGet.mockResolvedValue({
      messages: [{ role: 'user', content: 'hi', ts: 1 }],
      running: true,
      title: '',
    })
    await mount()
    expect(screen.getByTestId('chat-footer')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: /Thinking/ })).toBeInTheDocument()
  })

  it('shows nothing once the turn is over', async () => {
    mockGet.mockResolvedValue({
      messages: [
        { role: 'user', content: 'hi', ts: 1 },
        { role: 'assistant', content: 'hello', ts: 2 },
      ],
      running: false,
      title: '',
    })
    await mount()
    expect(screen.queryByTestId('chat-footer')).toBeNull()
  })

  it('yields to a streaming reply, and returns only after a quiet window wider than a poll', async () => {
    mockGet.mockResolvedValue({
      messages: [
        { role: 'user', content: 'hi', ts: 1 },
        { role: 'streaming', content: 'partial', ts: 2 },
      ],
      running: true,
      title: '',
    })
    await mount()
    expect(screen.queryByTestId('chat-footer')).toBeNull()
    // The live-socket window alone must not bring it back between two polls.
    await act(async () => { vi.advanceTimersByTime(STREAM_IDLE_MS + 100) })
    expect(screen.queryByTestId('chat-footer')).toBeNull()
    await act(async () => { vi.advanceTimersByTime(EMBED_STREAM_IDLE_MS) })
    expect(screen.getByTestId('chat-footer')).toBeInTheDocument()
  })
})
