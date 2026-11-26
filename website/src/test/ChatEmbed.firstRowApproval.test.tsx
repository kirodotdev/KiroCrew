/**
 * ChatEmbed's live region when a turn started elsewhere reaches an embed that
 * opened on an empty transcript: the poll that brings the first row can bring
 * the approval that turn is waiting on, and that approval is spoken (#43169).
 * An approval already waiting in the page the embed opens on stays silent.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { ChatMessage } from '../types'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const mockGet = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: (p: string, ...a: unknown[]) => (p.endsWith('/question') ? Promise.resolve({ pending: [] }) : mockGet(p, ...a)), post: vi.fn() }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: React.forwardRef(function MockList(
    { messages, transcript }: { messages: ChatMessage[]; transcript?: { announcement?: string } },
    ref: React.Ref<unknown>,
  ) {
    React.useImperativeHandle(ref, () => ({ scrollToBottom: vi.fn() }), [])
    return <div data-testid="chat-message-list" data-rows={messages.length} data-announcement={transcript?.announcement ?? ''} />
  }),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

const EMPTY_PAGE = { messages: [], running: false, title: 'Demo', has_more: false }
const WAITING_PAGE = {
  messages: [
    { role: 'user', content: 'ship it', ts: 1, meta: { mid: 'u1' } },
    { role: 'permission', content: '[agent] npm publish', ts: 2, meta: { mid: 'p1', approval_id: 'a1' } },
  ],
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
const list = () => screen.getByTestId('chat-message-list')

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  Element.prototype.scrollIntoView = vi.fn()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-26T12:00:00Z'))
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  queryClient.clear()
  vi.useRealTimers()
})

describe('ChatEmbed approval with the first row', () => {
  it('speaks an approval that arrives with the first row of an embed that opened empty', async () => {
    mockGet.mockResolvedValueOnce(EMPTY_PAGE).mockResolvedValue(WAITING_PAGE)
    await mount()
    expect(list().dataset.rows).toBe('0')
    // The idle cadence is five seconds.
    await advance(5000)
    expect(list().dataset.rows).toBe('2')
    expect(list().dataset.announcement).toBe('Kiro Crew is waiting for your approval: npm publish')
  })

  it('stays silent for an approval already waiting in the page the embed opens on', async () => {
    mockGet.mockResolvedValue(WAITING_PAGE)
    await mount()
    expect(list().dataset.rows).toBe('2')
    await advance(1000)
    expect(list().dataset.announcement).toBe('')
  })
})
