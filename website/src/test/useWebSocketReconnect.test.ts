import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import chatReducer, { sseSubagentSpawn, sseSubagentPending, sseSubagentDone } from '../store/chatSlice'
import type { RootState } from '../store'

// Track markSlotUnread dispatches
const markSlotUnreadCalls: string[] = []

vi.mock('../store/dashboardSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/dashboardSlice')>()
  return {
    ...actual,
    markSlotUnread: (slot: string) => {
      markSlotUnreadCalls.push(slot)
      return actual.markSlotUnread(slot)
    },
  }
})

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

describe('useWebSocket reconnect unread suppression', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    markSlotUnreadCalls.length = 0
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { activeSlot: 'chat-active', slotMessages: {}, slotRun: {}, slotHydrated: {}, slotActivity: {} } as RootState['chat'],
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    // Unconditional, because the fake-timer installs in this file are restored
    // INLINE at the end of the tests that need them. An assertion that fails
    // before its restore line leaves fake timers installed for every LATER test
    // here, so one real failure reports as a cascade of unrelated ones and the
    // real cause is buried. Idempotent when timers were never faked.
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)
    )
  }

  it('clears stale subagents before authoritative snapshot replay', () => {
    vi.useFakeTimers()
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
    testStore.dispatch(sseSubagentSpawn({ slot: 'chat-active', id: 'stale-active', task: 'Old active task', agent: 'kirocrew' }))
    testStore.dispatch(sseSubagentSpawn({ slot: 'chat-other', id: 'stale-background', task: 'Old background task', agent: 'kirocrew' }))

    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })

    expect(testStore.getState().chat.subagents).toEqual({})
    expect(testStore.getState().chat.slotActivity['chat-other']?.subagents).toEqual({})

    act(() => {
      ws1.simulateMessage({
        type: 'subagent_snapshot',
        data: { id: 'live-1', slot: 'chat-active', task: 'Live task', agent: 'kirocrew', streaming: '', last_tool: '', started: Date.now() / 1000 },
      })
    })
    expect(testStore.getState().chat.subagents['live-1']?.status).toBe('running')

    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) })
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })

    expect(testStore.getState().chat.subagents).toEqual({})

    act(() => {
      ws2.simulateMessage({
        type: 'subagent_snapshot',
        data: { id: 'live-2', slot: 'chat-active', task: 'Current live task', agent: 'kirocrew', streaming: '', last_tool: '', started: Date.now() / 1000 },
      })
    })
    expect(testStore.getState().chat.subagents['live-2']?.status).toBe('running')
    expect(testStore.getState().chat.subagents['live-1']).toBeUndefined()

    unmount()
    vi.useRealTimers()
  })

  it('preserves pending spawn-approval cards through the reconnect clear', () => {
    vi.useFakeTimers()
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
    // A running card (has a backend record, will be re-hydrated by replay) and a
    // pending spawn-approval card (no backend SubagentInfo yet, so nothing replays it).
    testStore.dispatch(sseSubagentSpawn({ slot: 'chat-active', id: 'stale-running', task: 'Old task', agent: 'kirocrew' }))
    testStore.dispatch(sseSubagentPending({ slot: 'chat-active', id: 'spawn:pending-1', task: 'Awaiting approval', approval_id: 'appr-1' }))

    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })

    // Running card is cleared; the pending approval card survives with its approval_id intact.
    expect(testStore.getState().chat.subagents['stale-running']).toBeUndefined()
    const pending = testStore.getState().chat.subagents['spawn:pending-1']
    expect(pending?.status).toBe('pending')
    expect(pending?.approval_id).toBe('appr-1')

    unmount()
    vi.useRealTimers()
  })

  it('hydrates a native card that completed while disconnected as a terminal done card', () => {
    vi.useFakeTimers()
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
    // A native card is live before the drop.
    testStore.dispatch(sseSubagentSpawn({ slot: 'chat-active', id: 'native:s1', task: 'Summarize', agent: 'worker' }))

    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })
    // Reconnect clears the (now stale) running card.
    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) })
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })
    expect(testStore.getState().chat.subagents['native:s1']).toBeUndefined()

    // The gateway replays the completion (it finished while the socket was down)
    // as a subagent_done — the terminal card must be rebuilt, not lost.
    act(() => {
      ws2.simulateMessage({
        type: 'subagent_done',
        data: { slot: 'chat-active', id: 'native:s1', elapsed: 3, task: 'Summarize', agent: 'worker', result: 'done feed' },
      })
    })
    const done = testStore.getState().chat.subagents['native:s1']
    expect(done?.status).toBe('done')
    expect(done?.elapsed).toBe(3)
    expect(done?.task).toBe('Summarize')
    // Native cards cannot lazy-load from disk, so the replayed result must be
    // preserved inline on the rebuilt terminal card.
    expect(done?.result).toBe('done feed')

    // Subscription starts before replay, so a live completion can arrive before
    // a stale running snapshot captured for the same card. Terminal state is
    // monotonic and must retain its result and elapsed time.
    act(() => {
      ws2.simulateMessage({
        type: 'subagent_snapshot',
        data: { id: 'native:s1', slot: 'chat-active', task: 'Summarize', agent: 'worker', streaming: 'stale', last_tool: 'read', started: Date.now() / 1000 },
      })
    })
    const afterStaleSnapshot = testStore.getState().chat.subagents['native:s1']
    expect(afterStaleSnapshot?.status).toBe('done')
    expect(afterStaleSnapshot?.elapsed).toBe(3)
    expect(afterStaleSnapshot?.result).toBe('done feed')

    unmount()
    vi.useRealTimers()
  })

  it('stores done result inline for native cards but not managed cards', () => {
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
    // Native card: result stored inline (no SubagentManager record to disk-load).
    testStore.dispatch(sseSubagentDone({ slot: 'chat-active', id: 'native:s9', elapsed: 2, task: 'T', agent: 'w', result: 'native feed' }))
    expect(testStore.getState().chat.subagents['native:s9']?.result).toBe('native feed')
    // Managed card: result left unset so the memory-friendly DiskLoader path is
    // preserved even though the event carries a (potentially large) result.
    testStore.dispatch(sseSubagentDone({ slot: 'chat-active', id: 'mgr-1', elapsed: 2, task: 'T', agent: 'w', result: 'x'.repeat(50000) }))
    expect(testStore.getState().chat.subagents['mgr-1']?.result).toBeUndefined()
  })

  it('suppresses markSlotUnread during reconnect catch-up window', async () => {
    vi.useFakeTimers()
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })

    // First connect
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })

    // Verify normal messages DO mark unread
    act(() => {
      ws1.simulateMessage({ type: 'chat_message', data: { slot: 'chat-other', role: 'assistant', content: 'hi', ts: '1' } })
    })
    expect(markSlotUnreadCalls).toContain('chat-other')
    markSlotUnreadCalls.length = 0

    // Simulate disconnect + wait for reconnect timer
    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) }) // reconnect backoff

    const ws2 = WS_INSTANCES[1]
    expect(ws2).toBeDefined()
    act(() => { ws2.simulateOpen() }) // triggers reconnect path (wasConnectedRef = true)

    // Messages during reconnect window should NOT mark unread
    act(() => {
      ws2.simulateMessage({ type: 'chat_message', data: { slot: 'chat-other', role: 'assistant', content: 'catch-up', ts: '2' } })
      ws2.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-other', content: 'chunk', seq: 1 } })
    })
    expect(markSlotUnreadCalls).toEqual([])

    // After fetchSlots resolves, messages SHOULD mark unread again
    vi.useRealTimers()
    await act(async () => { await new Promise(r => setTimeout(r, 50)) }) // flush fetchSlots promise
    act(() => {
      ws2.simulateMessage({ type: 'chat_message', data: { slot: 'chat-other', role: 'assistant', content: 'real', ts: '3' } })
    })
    expect(markSlotUnreadCalls).toContain('chat-other')

    unmount()
  })
})

describe('unread fires on chat_done not chat_chunk', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    markSlotUnreadCalls.length = 0
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { activeSlot: 'chat-active', slotMessages: {}, slotRun: {}, slotHydrated: {}, slotActivity: {} } as RootState['chat'],
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    // Unconditional, because the fake-timer installs in this file are restored
    // INLINE at the end of the tests that need them. An assertion that fails
    // before its restore line leaves fake timers installed for every LATER test
    // here, so one real failure reports as a cascade of unrelated ones and the
    // real cause is buried. Idempotent when timers were never faked.
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)
    )
  }

  it('chat_chunk on non-active slot does NOT mark unread', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-other', content: 'thinking...', seq: 1 } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-other', content: 'more thinking', seq: 2 } })
    })
    expect(markSlotUnreadCalls).toEqual([])
    unmount()
  })

  it('chat_done on non-active slot DOES mark unread', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-other' } })
    })
    expect(markSlotUnreadCalls).toContain('chat-other')
    unmount()
  })

  it('chat_done on active slot does NOT mark unread', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-active' } })
    })
    expect(markSlotUnreadCalls).toEqual([])
    unmount()
  })
})

describe('chat-stream-perf: chunk coalescing + background cache warm', () => {
  let testStore: ReturnType<typeof createTestStore>
  let rafCbs: FrameRequestCallback[]

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    rafCbs = []
    testStore = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // Capture rAF callbacks instead of running them, so we can prove that
    // multiple chunks within a frame coalesce into a single flush.
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  it('coalesces multiple chunks in a frame into one deferred flush', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'a', seq: 1 } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'b', seq: 2 } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'c', seq: 3 } })
    })
    // Deferred: nothing in the store yet, and only one flush scheduled for the 3 chunks.
    expect(testStore.getState().chat.messages.find(m => m.role === 'streaming')).toBeUndefined()
    expect(rafCbs.length).toBe(1)

    act(() => { rafCbs[0](0) })
    const streaming = testStore.getState().chat.messages.find(m => m.role === 'streaming')
    expect(streaming?.content).toBe('abc')
    unmount()
  })

  it('flushes buffered chunks before finalizing on chat_done', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'hello ', seq: 1 } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'world', seq: 2 } })
      // No rAF run; chat_done must flush synchronously so content isn't lost.
      ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-active' } })
    })
    const assistant = testStore.getState().chat.messages.find(m => m.role === 'assistant')
    expect(assistant?.content).toBe('hello world')
    unmount()
  })

  it('warms the per-slot cache when a background slot finishes', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-other' } }) })
    expect(api.chatSlotDetail).toHaveBeenCalledWith('chat-other')
    unmount()
  })

  it('surfaces a missed-chunk marker when buffered chunks have a seq gap', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    // seq jumps 1 -> 4: the buffer (not the reducer, since these coalesce into
    // one batched dispatch) must inline the gap marker for the 2 missed chunks.
    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'a', seq: 1 } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'b', seq: 4 } })
    })
    act(() => { rafCbs[0](0) })
    const streaming = testStore.getState().chat.messages.find(m => m.role === 'streaming')
    expect(streaming?.content).toBe('a\n[2 chunk(s) missed]\nb')
    unmount()
  })

  it('falls back to setTimeout to flush when requestAnimationFrame is unavailable', () => {
    vi.stubGlobal('requestAnimationFrame', undefined)
    vi.useFakeTimers()
    try {
      const { unmount } = renderHook(() => useWebSocket(), { wrapper })
      const ws = WS_INSTANCES[0]
      act(() => { ws.simulateOpen() })
      act(() => {
        ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'x', seq: 1 } })
      })
      // Not flushed until the timer fires.
      expect(testStore.getState().chat.messages.find(m => m.role === 'streaming')).toBeUndefined()
      act(() => { vi.advanceTimersByTime(16) })
      expect(testStore.getState().chat.messages.find(m => m.role === 'streaming')?.content).toBe('x')
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('flushes buffered chunks before finalizing on chat_segment', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => {
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'hello ', seq: 1 } })
      ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'world', seq: 2 } })
      // No rAF run; chat_segment must flush synchronously so content isn't lost
      // before the streaming message is finalized into an assistant message.
      ws.simulateMessage({ type: 'chat_segment', data: { slot: 'chat-active' } })
    })
    const finalized = testStore.getState().chat.messages.find(m => m.role === 'assistant')
    expect(finalized?.content).toBe('hello world')
    unmount()
  })

  it('cancels a pending chunk flush on unmount', () => {
    const cancelSpy = vi.fn()
    vi.stubGlobal('cancelAnimationFrame', cancelSpy)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    // Buffer a chunk → schedules a rAF flush (id = rafCbs.length = 1) we never run.
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'x', seq: 1 } }) })
    expect(rafCbs.length).toBe(1)
    act(() => { unmount() })
    expect(cancelSpy).toHaveBeenCalledWith(1)
  })

  it('cancels a pending chunk flush on reconnect', () => {
    const cancelSpy = vi.fn()
    vi.stubGlobal('cancelAnimationFrame', cancelSpy)
    vi.useFakeTimers()
    // Re-install the capturing rAF stub over vitest's fake-timer rAF so the
    // pending frame id is deterministic (rafCbs.length).
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafCbs.push(cb); return rafCbs.length })
    try {
      const { unmount } = renderHook(() => useWebSocket(), { wrapper })
      const ws1 = WS_INSTANCES[0]
      act(() => { ws1.simulateOpen() })
      // Buffer a chunk → schedules a rAF flush (id = 1) we never run.
      act(() => { ws1.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'x', seq: 1 } }) })
      expect(rafCbs.length).toBe(1)
      // Disconnect → reconnect: the reconnect branch must cancel the pending frame.
      act(() => { ws1.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(2000) })
      const ws2 = WS_INSTANCES[1]
      act(() => { ws2.simulateOpen() })
      expect(cancelSpy).toHaveBeenCalledWith(1)
      unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancels a pending scheduled frame when flushed synchronously (no orphaned flush)', () => {
    const cancelSpy = vi.fn()
    vi.stubGlobal('cancelAnimationFrame', cancelSpy)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    // Buffer a chunk → schedules a rAF flush (id = 1) we never run directly.
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'hi', seq: 1 } }) })
    expect(rafCbs.length).toBe(1)
    // A synchronous flush (chat_done) must cancel the still-pending frame so it
    // can't fire a stale flush after the buffer is drained.
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-active' } }) })
    expect(cancelSpy).toHaveBeenCalledWith(1)
    // And if the (now-cancelled) frame somehow still fires, it is a harmless
    // no-op: buffer already drained, so no duplicate/extra content.
    const before = testStore.getState().chat.messages.find(m => m.role === 'assistant')?.content
    act(() => { rafCbs[0](0) })
    expect(testStore.getState().chat.messages.find(m => m.role === 'assistant')?.content).toBe(before)
    expect(testStore.getState().chat.messages.filter(m => m.role === 'streaming')).toHaveLength(0)
    unmount()
  })

  it('skips slots with no buffered content on flush (empty-entry guard)', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    // Active slot buffers + flushes; its buffer entry is retained but emptied.
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-active', content: 'A', seq: 1 } }) })
    act(() => { rafCbs[0](0) })
    expect(testStore.getState().chat.messages.find(m => m.role === 'streaming')?.content).toBe('A')
    // A second flush (triggered by a different slot) iterates the now-empty
    // active entry — the guard `continue`s, so no empty re-dispatch duplicates it.
    act(() => { ws.simulateMessage({ type: 'chat_chunk', data: { slot: 'chat-bg', content: 'B', seq: 1 } }) })
    act(() => { rafCbs[1](0) })
    expect(testStore.getState().chat.messages.find(m => m.role === 'streaming')?.content).toBe('A')
    unmount()
  })
})
