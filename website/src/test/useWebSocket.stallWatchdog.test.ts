/** The row-delivery watchdog: a turn that is still running while its transcript
 *  has stopped moving must be re-hydrated from the server. Nothing else on the
 *  client can bring it back -- a live socket never trips the reconnect path,
 *  and the health probe only polls while `dashboard.connected === false`. */
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

  it('discards a stall refresh page once the transcript moves past the stall', async () => {
    /* A page fetched while the socket resumed delivering is older than the
     * view it would replace: applying it drops the newer rows and restores the
     * stale `running` flag. The stale-page guard must discard the fetch -- and
     * with no page there is no missed-row proof, so no reconnect either. */
    testStore.dispatch({ type: 'chat/setSlotRunning', payload: true })
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
      await vi.advanceTimersByTimeAsync(ROW_STALL_MS)
      expect(detailCalls()).toBeGreaterThan(0) // GET is in flight
      const before = socketCount()
      // The socket resumes delivering while the page is still in the air.
      testStore.dispatch({
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
    expect(testStore.getState().chat.slotRunning).toBe(true)
    unmount()
  })
})
