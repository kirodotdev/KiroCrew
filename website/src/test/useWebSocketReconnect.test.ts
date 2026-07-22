import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'
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

// Mock WebSocket
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
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)
    )
  }

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
