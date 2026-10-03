/** Row recovery must work while non-chat frames keep the socket healthy,
 *  without polling idle transcripts or overwriting concurrent chat changes. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket, ROW_STALL_MS, ROW_STALL_TICK_MS } from '../hooks/useWebSocket'
import { api } from '../api/client'
import chatReducer from '../store/chatSlice'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi
      .fn()
      .mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
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
}

describe('row-delivery stall watchdog', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.mocked(api.chatSlotDetail).mockReset().mockResolvedValue({
      messages: [], running: false, has_more: false, total: 0, queue: [],
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(
      Provider,
      { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  const detailCalls = () => vi.mocked(api.chatSlotDetail).mock.calls.length
  // A reconnect constructs a new socket, so the count is the observable for it.
  const socketCount = () => WS_INSTANCES.length

  it('re-hydrates the active slot when a running turn stops delivering rows', async () => {
    // Upstream has no `startRemoteTurn` reducer; the plain running setter is
    // what its send path uses to mark the slot busy.
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    expect(testStore.getState().chat.slotRunning).toBe(true)

    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS * 2)
    })
    const before = detailCalls()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS * 2)
    })

    expect(detailCalls()).toBeGreaterThan(before)
    unmount()
  })

  it('recovers stalled rows while status frames keep an open socket healthy', async () => {
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    ws.readyState = MockWebSocket.OPEN
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false })
    try {
      for (let elapsed = 0; elapsed < ROW_STALL_MS + ROW_STALL_TICK_MS * 2; elapsed += 5000) {
        await act(async () => {
          ws.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'dashboard', data: {} }) }))
          await vi.advanceTimersByTimeAsync(5000)
        })
      }
      expect(detailCalls()).toBe(1)
      expect(socketCount()).toBe(1)
      expect(testStore.getState().chat.slotRunning).toBe(false)
    } finally {
      unmount()
      Reflect.deleteProperty(document, 'hidden')
    }
  })

  it('recovers during concurrent background streaming without losing that activity', async () => {
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    let resolvePage!: (value: unknown) => void
    vi.mocked(api.chatSlotDetail).mockReturnValue(new Promise(resolve => { resolvePage = resolve }) as never)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS * 2) })
      expect(detailCalls()).toBe(1)
      await act(async () => {
        for (let i = 0; i < 3; i++) {
          testStore.dispatch({ type: 'chat/sseChatMessage', payload: { slot: 'background', role: 'chunk', content: 'x' } })
          await vi.advanceTimersByTimeAsync(100)
        }
        resolvePage({
          messages: [{ role: 'assistant', content: 'recovered', meta: { mid: 'missed' } }],
          running: false, has_more: false, total: 1, queue: [],
        })
      })
      expect(testStore.getState().chat.messages.at(-1)?.content).toBe('recovered')
      expect(testStore.getState().chat.slotMessages.background.at(-1)?.content).toBe('xxx')
      expect(testStore.getState().chat.slotRunning).toBe(false)
      await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS) })
      expect(socketCount()).toBe(2)
      await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS * 3) })
      expect(detailCalls()).toBe(1)
    } finally {
      unmount()
    }
  })

  it('settles a missed completion and stops polling the recovered idle slot', async () => {
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    testStore.dispatch({ type: 'chat/setSlotState', payload: 'tool_running' })
    testStore.dispatch({ type: 'chat/setSlotStopping', payload: true })
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [{ role: 'streaming', content: 'finished reply' }],
      running: false, has_more: false, total: 1, queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS * 2) })
    expect(detailCalls()).toBe(1)
    expect(testStore.getState().chat.slotRunning).toBe(false)
    expect(testStore.getState().chat.slotState).toBe('idle')
    expect(testStore.getState().chat.slotStopping).toBe(false)
    expect(testStore.getState().chat.messages.at(-1)?.role).toBe('assistant')
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS * 3) })
    expect(detailCalls()).toBe(1)
    unmount()
  })

  it('retries a failed recovery and stops polling after unmount', async () => {
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    vi.mocked(api.chatSlotDetail).mockRejectedValueOnce(new Error('offline'))
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS) })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS) })
    expect(detailCalls()).toBe(1)
    expect(socketCount()).toBe(1)
    expect(testStore.getState().chat.slotRunning).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS) })
    expect(detailCalls()).toBe(2)
    expect(testStore.getState().chat.slotRunning).toBe(false)
    unmount()
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS * 2) })
    expect(detailCalls()).toBe(2)
  })

  it('does not reconnect when an in-flight recovery resolves after unmount', async () => {
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    let resolvePage: (value: unknown) => void = () => {}
    vi.mocked(api.chatSlotDetail).mockReturnValue(new Promise(resolve => { resolvePage = resolve }) as never)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS) })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS) })
    expect(detailCalls()).toBe(1)
    unmount()
    await act(async () => {
      resolvePage({
        messages: [{ role: 'assistant', content: 'missed', meta: { mid: 'late' } }],
        running: true, has_more: false, total: 1, queue: [],
      })
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS * 2)
    })
    expect(detailCalls()).toBe(1)
    expect(socketCount()).toBe(1)
  })

  it('resets the stall clock when switching to a slot with the same row shape', async () => {
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS) })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS - ROW_STALL_TICK_MS) })
    testStore.dispatch({ type: 'chat/switchSlot/pending', meta: { arg: 'other-slot', requestId: 'switch' } })
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS) })
    expect(detailCalls()).toBe(0)
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS) })
    expect(detailCalls()).toBe(1)
    expect(api.chatSlotDetail).toHaveBeenCalledWith('other-slot', 50)
    unmount()
  })

  it('leaves an idle slot alone, and asks the server nothing, however long its rows sit still', async () => {
    /* The watchdog's steady state must cost nothing: its tick reads app state
     * and issues no request at all unless a slot it believes is RUNNING has
     * stopped moving. A slot this client believes idle is not its business --
     * re-checking that belief needs a server round trip per visible tab, which
     * this fix deliberately does not charge. */
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS * 2)
    })
    const before = detailCalls()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS * 3)
    })

    expect(detailCalls()).toBe(before)
    expect(api.chatSlots).not.toHaveBeenCalled()
    unmount()
  })

  it('reconnects once a refresh proves the socket missed rows', async () => {
    /* The page came back over HTTP carrying a durable row this client never
     * held while the turn is still believed running: the socket is the broken
     * half, so the recovery escalates to the reconnect whose catch-up re-reads
     * every frame family, not this slot's rows alone. */
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [
        { id: 'srv-1', role: 'assistant', content: 'row the socket missed', meta: { mid: 'm-1' } },
      ],
      running: true,
      has_more: false,
      total: 1,
      queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS)
    })
    const before = socketCount()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS * 2)
    })

    expect(detailCalls()).toBeGreaterThan(0)
    expect(socketCount()).toBeGreaterThan(before)
    unmount()
  })

  it('does not reconnect when the refresh returns nothing this client lacked', async () => {
    /* A slow turn is the ordinary reading of 100s of silence, and a teardown
     * there would discard buffered partial chunks for nothing. With no row the
     * client never held there is no proof, so the cheap re-fetch stands alone. */
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [],
      running: true,
      has_more: false,
      total: 0,
      queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS)
    })
    const before = socketCount()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS * 2)
    })

    expect(detailCalls()).toBeGreaterThan(0)
    expect(socketCount()).toBe(before)
    unmount()
  })

  it('counts a chunk reduced above a queued bubble as progress, firing no stall GET', async () => {
    /* The progress probe cannot rely on the row count or the TAIL row's text:
     * a user who queues a message mid-turn makes the queued/user bubble the
     * tail, so every subsequent chunk accumulates into the streaming row ABOVE
     * it -- the count and the tail length both sit still while the turn is
     * plainly alive. The `liveFrameSeq` signal (bumped by every active-slot
     * live frame) is what proves progress here, so no recovery GET fires. */
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    // Streaming row first, queued bubble pushed last so it is the tail.
    testStore.dispatch({
      type: 'chat/replaceMessages',
      payload: [
        { id: 's-1', role: 'streaming', content: 'answer so far', cls: 'msg msg-a', rawText: 'answer so far' },
        { id: 'q-1', role: 'queued', content: 'my next question', cls: 'msg msg-queued', ts: 'q-ts', meta: { queueId: 'q1' } },
      ],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    try {
      // Each tick, a chunk accumulates into the streaming row above the queued
      // tail: row count stays 2, the tail (queued) length never moves, but
      // liveFrameSeq advances -- so the watchdog must keep re-stamping and
      // never declare a stall.
      for (let elapsed = 0; elapsed < ROW_STALL_MS + ROW_STALL_TICK_MS * 2; elapsed += ROW_STALL_TICK_MS) {
        await act(async () => {
          testStore.dispatch({ type: 'chat/sseChatMessage', payload: { slot: 'chat-active', role: 'chunk', content: '.' } })
          await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS)
        })
      }
      expect(detailCalls()).toBe(0)
      // The tail is still the queued bubble; the streaming row above it grew.
      expect(testStore.getState().chat.messages.at(-1)?.role).toBe('queued')
    } finally {
      unmount()
    }
  })

  it('does not reconnect while the view holds an unidentified durable row the proof cannot key', async () => {
    /* A drained queue entry is rebuilt client-side as `{ role: 'user', ts, ... }`
     * with no server mid, and the server's own copy on the recovery page carries
     * a mid the client never held AND a different (drain-time vs enqueue-time)
     * ts -- so neither a mid nor a ts match recognises it, and it would read as a
     * missed delivery on every stall. While the view holds such an unidentified
     * durable row the missed-row proof is untrustworthy, so the escalation is
     * gated off and no healthy socket is torn down on a merely-slow turn. */
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    // The client holds the drained user row WITHOUT a mid, stamped with a ts.
    testStore.dispatch({
      type: 'chat/replaceMessages',
      payload: [
        { id: 'u-1', role: 'user', content: 'queued then sent', ts: 'row-ts-1' },
      ],
    })
    // The recovery page returns the server's own copy of that same row, now
    // carrying a mid the client never held -- same ts+role.
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: [
        { id: 'srv-u-1', role: 'user', content: 'queued then sent', ts: 'row-ts-2-drain', meta: { mid: 'server-mid-1' } },
      ],
      running: true, has_more: false, total: 1, queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS) })
      const before = socketCount()
      await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS * 2) })
      expect(detailCalls()).toBeGreaterThan(0) // the cheap re-fetch still runs
      expect(socketCount()).toBe(before)       // but no reconnect teardown
    } finally {
      unmount()
    }
  })

  it.each(['empty view', 'same-length edit'])('discards a stall refresh after a live change: %s', async shape => {
    /* A page fetched while the socket resumed delivering is older than the
     * view it would replace: applying it drops the newer rows and restores the
     * stale `running` flag. The stale-page guard must discard the fetch -- and
     * with no page there is no missed-row proof, so no reconnect either. */
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
    if (shape === 'same-length edit') {
      testStore.dispatch({
        type: 'chat/replaceMessages',
        payload: [{ id: 'live-1', role: 'assistant', content: 'old', ts: 'row-ts', meta: { mid: 'live-mid' } }],
      })
    }
    let resolvePage: (v: unknown) => void = () => {}
    vi.mocked(api.chatSlotDetail).mockReturnValue(
      new Promise((resolve) => {
        resolvePage = resolve
      }) as never,
    )
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS)
      expect(detailCalls()).toBe(1) // GET is in flight
      const before = socketCount()
      // The socket resumes delivering while the page is still in the air.
      testStore.dispatch(shape === 'same-length edit' ? {
        type: 'chat/sseChatMessageUpdate',
        payload: { slot: 'chat-active', ts: 'row-ts', content: 'new' },
      } : {
        type: 'chat/appendSlotMessage',
        payload: { slot: 'chat-active', message: { id: 'live-1', role: 'assistant', content: 'live frame' } },
      })
      resolvePage({
        messages: [
          { id: 'srv-1', role: 'assistant', content: 'stale page row', meta: { mid: 'm-1' } },
        ],
        running: false,
        has_more: false,
        total: 1,
        queue: [],
      })
      await vi.advanceTimersByTimeAsync(ROW_STALL_TICK_MS)
      expect(socketCount()).toBe(before)
    })
    // The live frame survives; the stale page never replaced the transcript.
    expect(testStore.getState().chat.messages.map((m) => m.id)).toEqual(['live-1'])
    expect(testStore.getState().chat.messages[0].content).toBe(shape === 'same-length edit' ? 'new' : 'live frame')
    expect(testStore.getState().chat.slotRunning).toBe(true)
    const liveRows = testStore.getState().chat.messages
    vi.mocked(api.chatSlotDetail).mockResolvedValue({
      messages: liveRows, running: false, has_more: false, total: liveRows.length, queue: [],
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(ROW_STALL_MS + ROW_STALL_TICK_MS) })
    expect(testStore.getState().chat.slotRunning).toBe(false)
    expect(testStore.getState().chat.messages[0].content).toBe(shape === 'same-length edit' ? 'new' : 'live frame')
    unmount()
  })
})
