/** WS reconnect must re-hydrate BACKGROUND split panes, not just the active slot.
 *
 *  The queue event family (queue_push/cancel/edit/reorder) is broadcast
 *  fire-and-forget with no per-client buffer, so a mutation that happens while
 *  the socket is down never reaches this client. The reconnect branch used to
 *  refresh only the ACTIVE slot (refreshSlot self-guards to it), leaving a
 *  co-rendered background pane showing the queue state it held at the drop
 *  (#2348). Warming every other split member through warmSlotCache closes the
 *  window for the whole event family at once: its hydration rebuilds queued
 *  rows from the server's canonical queue, so a stale card cannot survive it.
 *
 *  Assertions observe `api.chatSlotDetail` rather than thunk internals: a warm
 *  is the bounded call `(slot, PANE_HYDRATE_LIMIT)`, the active refresh is the
 *  unbounded call `(slot)` — the same observable the sibling reconnect and
 *  warm-bound suites key on.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { PANE_HYDRATE_LIMIT, setActiveSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { saveLayout } from '../hooks/splitLayoutStore'
import type { GridNode } from '../hooks/useSessionGrid'

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
}

const sLeaf = (id: string, slot: string): GridNode => ({ type: 'leaf', id, kind: 'session', slot })
const split = (id: string, children: GridNode[]): GridNode => ({
  type: 'split',
  id,
  dir: 'col',
  children,
  sizes: children.map(() => 1 / children.length),
})

/** Every bounded (warm) fetch the mock saw, by slot key. */
const warmedSlots = (): string[] =>
  (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
    .filter(c => c[1] === PANE_HYDRATE_LIMIT)
    .map(c => c[0] as string)

describe('useWebSocket reconnect hydrates background split panes', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    localStorage.clear()
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // The hook DISPATCHES through the Provider store but READS `activeSlot`
    // and `dashboard.slots` off the singleton store imported from '../store',
    // so the read paths must be primed there (same harness note as
    // UseWebSocketCoverage.test.tsx).
    globalStore.dispatch(setActiveSlot('chat-active'))
    globalStore.dispatch(sseSlots([{ key: 'chat-active' }, { key: 'chat-bg' }] as never))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    // Unconditional, because the fake-timer installs are restored INLINE at the
    // end of each test. An assertion that fails before its restore line would
    // otherwise leave fake timers installed for every LATER test here, burying
    // the real failure under a cascade of unrelated ones. Idempotent when
    // timers were never faked.
    vi.useRealTimers()
    localStorage.clear()
    globalStore.dispatch(setActiveSlot(null))
    globalStore.dispatch(sseSlots([] as never))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children)
    )
  }

  /** First connect, drop, reconnect — returns after the reconnect branch ran.
   *  The mock is cleared just before the second open so every recorded call
   *  belongs to the reconnect path alone. */
  function connectDropReconnect(): void {
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })
    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) }) // reconnect backoff
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })
  }

  it('warms the non-active member of a two-pane split', () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [sLeaf('a', 'chat-active'), sLeaf('b', 'chat-bg')]))
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    // The background pane is re-hydrated through the bounded warm path…
    expect(warmedSlots()).toEqual(['chat-bg'])
    // …while the active slot keeps its own refresh. With an EMPTY view the
    // count-matched refresh stays unbounded (no count to match -- see
    // refreshSlot's doc), so the call carries no limit argument.
    expect(api.chatSlotDetail).toHaveBeenCalledWith('chat-active')

    unmount()
    vi.useRealTimers()
  })

  it('dispatches no warm at all for a single-pane session', () => {
    vi.useFakeTimers()
    // No persisted split: the common case must not gain a single extra request.
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots()).toEqual([])
    // The only slot-detail traffic on reconnect is the active slot's refresh.
    const calls = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
    expect(calls.every(c => c[0] === 'chat-active')).toBe(true)

    unmount()
    vi.useRealTimers()
  })

  it('never warms the active slot itself', () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [sLeaf('a', 'chat-active'), sLeaf('b', 'chat-bg')]))
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    // Belt (the handler filters it) and braces (warmSlotCache's own
    // active-slot guard returns before fetching): no bounded call for the
    // active slot can exist on either layer.
    expect(warmedSlots()).not.toContain('chat-active')

    unmount()
    vi.useRealTimers()
  })

  it('warms a duplicated member once (tree can name a slot twice)', () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [
      sLeaf('a', 'chat-active'),
      sLeaf('b', 'chat-bg'),
      sLeaf('c', 'chat-bg'),
    ]))
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots()).toEqual(['chat-bg'])

    unmount()
    vi.useRealTimers()
  })

  it('finds the split when the active slot is a member but not the anchor', () => {
    vi.useFakeTimers()
    // Layout keyed by its FIRST session leaf ('chat-bg'), so the lookup must go
    // through anchorForSlot rather than assuming the active slot anchors it.
    saveLayout(null, split('s', [sLeaf('a', 'chat-bg'), sLeaf('b', 'chat-active')]))
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots()).toEqual(['chat-bg'])

    unmount()
    vi.useRealTimers()
  })

  it('does not warm on the FIRST connect, only on reconnect', () => {
    saveLayout(null, split('s', [sLeaf('a', 'chat-active'), sLeaf('b', 'chat-bg')]))
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })

    // First connect takes the non-reconnect branch: nothing was missed, so
    // nothing is warmed and the active slot is not even refreshed.
    expect(warmedSlots()).toEqual([])

    unmount()
  })

  it('does not idle a background member that is still running (mid-turn reconnect)', async () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [sLeaf('a', 'chat-active'), sLeaf('b', 'chat-bg')]))
    // The member's turn is still in flight when the socket drops: the warm's
    // fulfilled reducer must not force its run indicator to idle (there is no
    // server-side recovery for a background slot until the next chunk frame,
    // so a quiet phase would stay mislabeled and unlock the pane's composer).
    testStore = createTestStore({
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: 'chat-active',
        slotRun: { 'chat-bg': { state: 'streaming' } },
      },
    })
    // The reconnect fetchSlots reconcile deletes per-slot caches for sessions
    // absent from the authoritative list — return the live pair so the
    // preloaded run state survives to meet the warm.
    ;(api.chatSlots as ReturnType<typeof vi.fn>).mockResolvedValue([
      { key: 'chat-active' }, { key: 'chat-bg' },
    ])
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [], running: true, has_more: false, total: 0, queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    // A streaming slot warms unbounded by the thunk's own design.
    expect(api.chatSlotDetail).toHaveBeenCalledWith('chat-bg')

    // Let the warm resolve and its fulfilled reducer run.
    vi.useRealTimers()
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(testStore.getState().chat.slotRun['chat-bg']?.state).toBe('streaming')

    unmount()
  })

  it('still idles the run indicator when the server reports not running', async () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [sLeaf('a', 'chat-active'), sLeaf('b', 'chat-bg')]))
    // Counter-case bounding the gate: a slot whose turn ENDED while the socket
    // was down (client state stuck streaming, server says settled) must still
    // be idled by the warm — the belt-and-braces contract of the turn-done
    // caller survives the gate.
    testStore = createTestStore({
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: 'chat-active',
        slotRun: { 'chat-bg': { state: 'streaming' } },
      },
    })
    ;(api.chatSlots as ReturnType<typeof vi.fn>).mockResolvedValue([
      { key: 'chat-active' }, { key: 'chat-bg' },
    ])
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [], running: false, has_more: false, total: 0, queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    vi.useRealTimers()
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(testStore.getState().chat.slotRun['chat-bg']?.state).toBe('idle')

    unmount()
  })

  it('leaves the run entry absent when the server reports the turn live (no promotion)', async () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [sLeaf('a', 'chat-active'), sLeaf('b', 'chat-bg')]))
    // The warm is a point-in-time snapshot racing the ordered live-frame
    // writers, so it never writes the RUNNING direction: any promotion policy
    // has a losing ordering (see the resurrect case below). A turn that
    // started while the socket was down reads idle until its first
    // post-reconnect frame — the same behavior as main, where reconnect never
    // touches background run state; the ordering-token fix is tracked
    // separately.
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
    ;(api.chatSlots as ReturnType<typeof vi.fn>).mockResolvedValue([
      { key: 'chat-active' }, { key: 'chat-bg' },
    ])
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [], running: true, has_more: false, total: 0, queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    vi.useRealTimers()
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(testStore.getState().chat.slotRun['chat-bg']).toBeUndefined()

    unmount()
  })

  it('does not resurrect a pane a _done frame already idled (late warm fulfillment)', async () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [sLeaf('a', 'chat-active'), sLeaf('b', 'chat-bg')]))
    // Ordering pin: snapshot taken while the turn ran (running: true), the
    // _done frame lands BEFORE the warm's fulfillment reduces (entry exists,
    // idle). The snapshot must not overwrite the ordered live writer — an
    // idle-to-streaming promotion here wedged the pane's composer locked with
    // no healer (the turn is over, so no further chunk frame arrives, and the
    // reconnect suppression window skips the turn-done warm).
    testStore = createTestStore({
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: 'chat-active',
        // The _done frame's write: entry exists and reads idle.
        slotRun: { 'chat-bg': { state: 'idle' } },
      },
    })
    ;(api.chatSlots as ReturnType<typeof vi.fn>).mockResolvedValue([
      { key: 'chat-active' }, { key: 'chat-bg' },
    ])
    // The snapshot predates the _done: it still claims the turn is running.
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: [], running: true, has_more: false, total: 0, queue: [],
    })
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    vi.useRealTimers()
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(testStore.getState().chat.slotRun['chat-bg']?.state).toBe('idle')

    unmount()
  })

  it('skips a layout member whose session no longer exists', () => {
    vi.useFakeTimers()
    // Stale persisted layout: 'chat-dead' was deleted while the layout kept
    // naming it. The live-slots filter (ChatPage.splitAnchorForActive pattern)
    // must drop it so a reconnect costs no 404 round-trip.
    saveLayout(null, split('s', [
      sLeaf('a', 'chat-active'),
      sLeaf('b', 'chat-bg'),
      sLeaf('c', 'chat-dead'),
    ]))
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots()).toEqual(['chat-bg'])
    const calls = (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
    expect(calls.some(c => c[0] === 'chat-dead')).toBe(false)

    unmount()
    vi.useRealTimers()
  })

  it('completes reconnect setup even when the persisted layout is corrupt', () => {
    vi.useFakeTimers()
    // Valid JSON, invalid tree shape: sessionSlots would throw on it. The warm
    // block must absorb that so the statements after it (subagent resubscribe,
    // focus re-announce) still run on every reconnect.
    localStorage.setItem('mc-split-layouts', JSON.stringify({ 'chat-active': {} }))
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots()).toEqual([])
    const ws2 = WS_INSTANCES[1]
    // The reconnect branch ran to completion past the corrupt-layout read.
    expect(ws2.send).toHaveBeenCalledWith(JSON.stringify({ type: 'subscribe_subagents' }))
    expect(ws2.send).toHaveBeenCalledWith(JSON.stringify({ type: 'slot_focused', slot: 'chat-active' }))

    warnSpy.mockRestore()
    unmount()
    vi.useRealTimers()
  })
})
