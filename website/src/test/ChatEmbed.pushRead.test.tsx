/**
 * ChatEmbed under a host push channel (`slotPush`): a change signal re-reads
 * the slot at once, a burst of signals collapses into one trailing read, the
 * poll drops to a backstop while the channel is up and returns to its normal
 * cadence when it goes down, each connect re-reads once, and a failing read
 * still raises the stalled notice on the no-push clock.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { ChatMessage } from '../types'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'
import { AppSdkContext, type AppSdkContextValue } from '../app-sdk/scopedApi'
import type { SlotPush, SlotPushWatcher } from '../app-sdk/slotPush'

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
    return <div data-testid="chat-message-list" data-running={String(running)} data-rows={messages.map(m => m.content).join('|')} />
  }),
}))

import ChatEmbed, {
  EMBED_PUSH_BACKSTOP_IDLE_MS,
  EMBED_PUSH_BACKSTOP_RUNNING_MS,
  EMBED_PUSH_MIN_GAP_MS,
  EMBED_STALL_MS,
  embedPollInterval,
} from '../app-sdk/ChatEmbed'

const IDLE_PAGE = { messages: [{ role: 'user', content: 'hi', ts: 1 }], running: false, title: 'Demo', has_more: false }

/** A host channel the test drives by hand. */
function fakePush(initiallyUp: boolean) {
  let up = initiallyUp
  // Whether the host signals this embed's slot at all (the dashboard: the slot
  // is owned by the watching app). The socket can be up while this is false.
  let signalled = true
  const watchers = new Map<SlotPushWatcher, string>()
  const push: SlotPush = {
    connected: () => up && signalled,
    watch(slotKey, watcher) {
      watchers.set(watcher, slotKey)
      return () => { watchers.delete(watcher) }
    },
  }
  return {
    push,
    watched: () => [...watchers.values()],
    signal: () => { for (const w of [...watchers.keys()]) w.onChange() },
    setUp: (v: boolean) => { up = v; for (const w of [...watchers.keys()]) w.onConnection(v && signalled) },
    setSignalled: (v: boolean) => { signalled = v },
  }
}

let queryClient: QueryClient

async function mount(push: SlotPush | null) {
  const ctx: AppSdkContextValue = {
    api: { get: mockGet, post: mockPost } as unknown as AppSdkContextValue['api'],
    info: { name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } },
    subscribe: () => () => {},
    navigate: () => {},
    notify: () => {},
    ...(push ? { slotPush: push } : {}),
  }
  await act(async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <AppSdkContext.Provider value={ctx}>
          <ChatEmbed slotKey="slot-1" />
        </AppSdkContext.Provider>
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
  vi.setSystemTime(new Date('2026-10-16T12:00:00Z'))
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  queryClient.clear()
  vi.useRealTimers()
})

describe('embedPollInterval with push', () => {
  const ok = (running: boolean) => ({ status: 'success', data: { running }, dataUpdatedAt: 1, errorUpdatedAt: 0 })
  const failing = (running: boolean) => ({ status: 'error', data: { running }, dataUpdatedAt: 1, errorUpdatedAt: 2 })

  it('backs a healthy read off to the backstop while pushed', () => {
    expect(embedPollInterval(ok(true), 0, true)).toBe(EMBED_PUSH_BACKSTOP_RUNNING_MS)
    expect(embedPollInterval(ok(false), 0, true)).toBe(EMBED_PUSH_BACKSTOP_IDLE_MS)
  })

  it('keeps the no-push cadence for a failing read even while pushed', () => {
    expect(embedPollInterval(failing(true), 0, true)).toBe(embedPollInterval(failing(true), 0, false))
    expect(embedPollInterval(failing(false), 0, true)).toBe(5000)
  })
})

describe('ChatEmbed push reads', () => {
  it('watches its own slot', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    expect(host.watched()).toEqual(['slot-1'])
  })

  it('re-reads on a change signal instead of waiting for the poll', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    expect(mockGet).toHaveBeenCalledTimes(1)
    host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('collapses signals during an in-flight read into one trailing read', async () => {
    let release: (v: unknown) => void = () => {}
    mockGet
      .mockResolvedValueOnce(IDLE_PAGE)
      .mockImplementationOnce(() => new Promise((res) => { release = res }))
      .mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    host.signal()
    await advance(10)
    expect(mockGet).toHaveBeenCalledTimes(2)
    for (let i = 0; i < 10; i++) host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS * 2)
    expect(mockGet).toHaveBeenCalledTimes(2)
    await act(async () => { release(IDLE_PAGE) })
    await advance(EMBED_PUSH_MIN_GAP_MS * 2)
    expect(mockGet).toHaveBeenCalledTimes(3)
  })

  it('polls only as a backstop while the channel is up', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    // The idle no-push cadence is 5s; the backstop is 15s.
    await advance(6000)
    expect(mockGet).toHaveBeenCalledTimes(1)
    await advance(EMBED_PUSH_BACKSTOP_IDLE_MS)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('returns to the normal cadence when the channel drops, and re-reads on reconnect', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    act(() => { host.setUp(false) })
    await advance(5100)
    expect(mockGet).toHaveBeenCalledTimes(2)
    const before = mockGet.mock.calls.length
    act(() => { host.setUp(true) })
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet.mock.calls.length).toBe(before + 1)
  })

  it('keeps the normal cadence for a slot the host does not signal, though the socket is up', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    host.setSignalled(false)
    await mount(host.push)
    await advance(5100)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('returns to the normal cadence when the slot stops being signalled mid-life', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    // One backstop read, judged while the slot was still signalled...
    await advance(EMBED_PUSH_BACKSTOP_IDLE_MS + 100)
    expect(mockGet).toHaveBeenCalledTimes(2)
    // ...then it changes hands with the socket still up: the next tick is 5s.
    host.setSignalled(false)
    await advance(EMBED_PUSH_BACKSTOP_IDLE_MS + 100)
    const after = mockGet.mock.calls.length
    await advance(5100)
    expect(mockGet.mock.calls.length).toBe(after + 1)
  })

  it('polls at the normal cadence with no host channel', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    await mount(null)
    await advance(5100)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('still flags a stall on time when reads fail while the channel is up', async () => {
    mockGet.mockResolvedValueOnce(IDLE_PAGE).mockRejectedValue(new Error('session gone'))
    const host = fakePush(true)
    await mount(host.push)
    host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(screen.queryByTestId('chat-embed-stalled')).toBeNull()
    // On the 15s backstop the second failure would not come before 15s.
    await advance(EMBED_STALL_MS + 500)
    expect(screen.getByTestId('chat-embed-stalled')).toBeInTheDocument()
  })
})
