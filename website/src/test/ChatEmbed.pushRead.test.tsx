/**
 * ChatEmbed under a host push channel (`slotPush`): a change signal re-reads
 * the slot at once, a burst of signals collapses into one trailing read, the
 * poll drops to a backstop while the channel is up and returns to its normal
 * cadence when it goes down, each connect re-reads once, a hidden page or
 * inactive host tab holds every push read until it is visible, and a failing
 * read still raises the stalled notice on the no-push clock.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'

import type { ChatMessage } from '../types'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'
import { AppSdkContext, type AppSdkContextValue } from '../app-sdk/scopedApi'
import type { SlotPush, SlotPushWatcher } from '../app-sdk/slotPush'

const mockGet = vi.fn()
const mockPost = vi.fn()
// The host tab's `info.active`; undefined is a host that predates the flag.
let mockHostActive: boolean | undefined

vi.mock('../app-sdk/index', () => ({
  // The question-card poll is answered here so mockGet counts transcript reads only.
  useAppApi: () => ({ get: (p: string, ...a: unknown[]) => (p.endsWith('/question') ? Promise.resolve({ pending: [] }) : mockGet(p, ...a)), post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] }, active: mockHostActive }),
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
  EMBED_PAGE_LIMIT,
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
  const tree = () => (
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <ChatEmbed slotKey="slot-1" />
      </AppSdkContext.Provider>
    </QueryClientProvider>
  )
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(tree()) })
  await act(async () => { await vi.advanceTimersByTimeAsync(100) })
  /** Re-render the same tree, e.g. after `mockHostActive` changed. */
  return { rerender: () => act(async () => { view.rerender(tree()) }) }
}

/** The dashboard's client: `staleTime: Infinity`, so focus alone refetches
 *  nothing and every read counted here is the embed's own. */
const withHostClient = () => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
}

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })

beforeEach(() => {
  vi.clearAllMocks()
  mockHostActive = undefined
  resetEmbedRecoveryForTests()
  mockPost.mockResolvedValue({})
  Element.prototype.scrollIntoView = vi.fn()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-16T12:00:00Z'))
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  focusManager.setFocused(undefined)
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

  it('reads again after a signal that joined a poll read already in flight', async () => {
    let release: (v: unknown) => void = () => {}
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    expect(mockGet).toHaveBeenCalledTimes(1)
    // The backstop poll starts a read the push loop did not start...
    mockGet.mockImplementationOnce(() => new Promise((res) => { release = res }))
    await advance(EMBED_PUSH_BACKSTOP_IDLE_MS + 10)
    expect(mockGet).toHaveBeenCalledTimes(2)
    // ...and the change lands while it is still in flight.
    host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(2)
    await act(async () => { release(IDLE_PAGE) })
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(3)
  })

  it('re-reads a cached page when it attaches to a channel that is already up', async () => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    queryClient.setQueryData(['app-sdk-embed', 'slot-1', EMBED_PAGE_LIMIT], IDLE_PAGE)
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('sends no push read while the page is hidden, then one catch-up read', async () => {
    withHostClient()
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    expect(mockGet).toHaveBeenCalledTimes(1)
    act(() => { focusManager.setFocused(false) })
    for (let i = 0; i < 20; i++) {
      host.signal()
      await advance(EMBED_PUSH_MIN_GAP_MS)
    }
    act(() => { host.setUp(false); host.setUp(true) })
    await advance(EMBED_PUSH_MIN_GAP_MS * 4)
    expect(mockGet).toHaveBeenCalledTimes(1)
    act(() => { focusManager.setFocused(true) })
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(2)
    await advance(EMBED_PUSH_MIN_GAP_MS * 4)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('holds a signal that lands between scheduling and sending while the page hides', async () => {
    withHostClient()
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(2)
    // The next read waits out the gap; the page hides before it is sent.
    host.signal()
    act(() => { focusManager.setFocused(false) })
    await advance(EMBED_PUSH_MIN_GAP_MS * 4)
    expect(mockGet).toHaveBeenCalledTimes(2)
    act(() => { focusManager.setFocused(true) })
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(3)
  })

  it('sends no push read while the host tab is inactive, then one when it is active', async () => {
    withHostClient()
    mockGet.mockResolvedValue(IDLE_PAGE)
    mockHostActive = false
    const host = fakePush(true)
    const view = await mount(host.push)
    expect(mockGet).toHaveBeenCalledTimes(1)
    for (let i = 0; i < 10; i++) host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS * 4)
    expect(mockGet).toHaveBeenCalledTimes(1)
    mockHostActive = true
    await view.rerender()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('sends nothing on becoming visible when no signal came while hidden', async () => {
    withHostClient()
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    await mount(host.push)
    act(() => { focusManager.setFocused(false) })
    await advance(1000)
    act(() => { focusManager.setFocused(true) })
    await advance(EMBED_PUSH_MIN_GAP_MS * 4)
    expect(mockGet).toHaveBeenCalledTimes(1)
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

  it('moves to the backstop once a slot listed after mount becomes signalled', async () => {
    mockGet.mockResolvedValue(IDLE_PAGE)
    const host = fakePush(true)
    // The socket is up, but the slot is not yet the app's at mount.
    host.setSignalled(false)
    await mount(host.push)
    await advance(5100)
    expect(mockGet).toHaveBeenCalledTimes(2)
    // The slot list now carries it, with no reconnect: the next tick is 15s.
    host.setSignalled(true)
    await advance(5100)
    const after = mockGet.mock.calls.length
    await advance(5100)
    expect(mockGet.mock.calls.length).toBe(after)
    await advance(EMBED_PUSH_BACKSTOP_IDLE_MS)
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

  it('sends no push read while the read is failing, and resumes once it recovers', async () => {
    mockGet.mockResolvedValueOnce(IDLE_PAGE).mockRejectedValueOnce(new Error('session gone'))
    const host = fakePush(true)
    await mount(host.push)
    host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(2)
    // Frames keep coming while the read fails: none of them reads.
    mockGet.mockRejectedValue(new Error('session gone'))
    for (let i = 0; i < 8; i++) {
      host.signal()
      await advance(EMBED_PUSH_MIN_GAP_MS)
    }
    expect(mockGet).toHaveBeenCalledTimes(2)
    // The poll still retries on its own cadence and recovers the read...
    mockGet.mockResolvedValue(IDLE_PAGE)
    await advance(5100)
    expect(mockGet).toHaveBeenCalledTimes(3)
    // ...after which a signal reads at once again.
    host.signal()
    await advance(EMBED_PUSH_MIN_GAP_MS + 50)
    expect(mockGet).toHaveBeenCalledTimes(4)
  })
})
