/** WS reconnect must re-hydrate every MOUNTED ChatPane, not only the active
 *  slot and its split members.
 *
 *  The Crew Members DM thread is a ChatPane whose `member-<slug>` slot is never
 *  the Redux active slot and is named by no persisted split, so the two
 *  reconnect resync branches (refreshSlot for the active slot, warmSlotCache for
 *  the split) both pass it by. Its rows arrive only as fire-and-forget frames:
 *  a `tool_result`, a later `tool_call` or the final `_done` broadcast while the
 *  socket is down never reaches this client, and the pane's own hydrate query
 *  is one-shot, so on return the pane keeps rendering the tool-call row it held
 *  at the drop. A phone that backgrounds the tab hits exactly this every time.
 *
 *  The pane's observed hydrate query (`['slot-messages', slot, limit]`) is the
 *  registry of on-screen panes: the reconnect branch warms each observed slot
 *  once through warmSlotCache. Assertions observe `api.chatSlotDetail` as the
 *  sibling suite does: a warm of an uncached slot is the bounded call
 *  `(slot, PANE_HYDRATE_LIMIT)`.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { store as globalStore } from '../store'
import chatReducer, { PANE_HYDRATE_LIMIT, setActiveSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import { slotMessagesQueryKey } from '../api/slotMessagesQuery'
import { saveLayout } from '../hooks/splitLayoutStore'
import type { GridNode } from '../hooks/useSessionGrid'
import type { ChatMessage } from '../types'

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

const MEMBER = 'member-oncall'
const ACTIVE = 'chat-active'
const BG = 'chat-bg'

const sLeaf = (id: string, slot: string): GridNode => ({ type: 'leaf', id, kind: 'session', slot })
const split = (id: string, children: GridNode[]): GridNode => ({
  type: 'split',
  id,
  dir: 'col',
  children,
  sizes: children.map(() => 1 / children.length),
})

/** Every slot-detail fetch the mock saw on the reconnect path, by slot key. */
const fetchedSlots = (): string[] =>
  (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0] as string)
/** The bounded (warm-of-an-uncached-slot) subset. */
const warmedSlots = (): string[] =>
  (api.chatSlotDetail as ReturnType<typeof vi.fn>).mock.calls
    .filter(c => c[1] === PANE_HYDRATE_LIMIT)
    .map(c => c[0] as string)

const row = (mid: string, role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ts: '2026-01-01T00:00:00Z', meta: { mid, ...meta } })

describe('useWebSocket reconnect hydrates every mounted ChatPane (observed hydrate queries)', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient
  const unsubscribers: Array<() => void> = []

  /** What a mounted ChatPane does: hold an observer on its slot's hydrate query. */
  function mountPane(slot: string, limit: number | undefined = PANE_HYDRATE_LIMIT): () => void {
    const observer = new QueryObserver(qc, {
      queryKey: slotMessagesQueryKey(slot, limit),
      queryFn: () => Promise.resolve({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
      staleTime: Infinity,
      enabled: false,
    })
    const unsubscribe = observer.subscribe(() => {})
    unsubscribers.push(unsubscribe)
    return unsubscribe
  }

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    localStorage.clear()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // The hook DISPATCHES through the Provider store but READS `activeSlot`
    // and `dashboard.slots` off the singleton store imported from '../store'.
    globalStore.dispatch(setActiveSlot(ACTIVE))
    globalStore.dispatch(sseSlots([{ key: ACTIVE }, { key: BG }, { key: MEMBER, mode: 'member' }] as never))
    ;(api.chatSlots as ReturnType<typeof vi.fn>).mockResolvedValue([
      { key: ACTIVE }, { key: BG }, { key: MEMBER, mode: 'member' },
    ])
  })

  afterEach(() => {
    for (const u of unsubscribers.splice(0)) u()
    qc.clear()
    vi.unstubAllGlobals()
    vi.useRealTimers()
    localStorage.clear()
    globalStore.dispatch(setActiveSlot(null))
    globalStore.dispatch(sseSlots([] as never))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
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
    dropAndReconnect()
  }

  function dropAndReconnect(): void {
    const dead = WS_INSTANCES[WS_INSTANCES.length - 1]
    act(() => { dead.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(10_000) }) // past any backoff step
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockClear()
    const next = WS_INSTANCES[WS_INSTANCES.length - 1]
    act(() => { next.simulateOpen() })
  }

  const settle = async () => {
    vi.useRealTimers()
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  }

  it('warms a member DM pane that is neither active nor in any split', () => {
    vi.useFakeTimers()
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots()).toEqual([MEMBER])
    // The active slot keeps its own refresh, untouched by the pane warm.
    expect(api.chatSlotDetail).toHaveBeenCalledWith(ACTIVE)

    unmount()
    vi.useRealTimers()
  })

  it('warms the member pane even when no slot is active at all (Members page only)', () => {
    vi.useFakeTimers()
    // A phone that only ever opened the Crew Members page never set an active
    // slot: the split branch has no anchor, so this warm is the only resync.
    globalStore.dispatch(setActiveSlot(null))
    testStore = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: null } })
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(fetchedSlots()).toEqual([MEMBER])

    unmount()
    vi.useRealTimers()
  })

  it('leaves an unobserved (unmounted) pane alone', () => {
    vi.useFakeTimers()
    const unsubscribe = mountPane(MEMBER)
    unsubscribe() // the pane unmounted; its cache entry lingers with no observer
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(fetchedSlots()).not.toContain(MEMBER)

    unmount()
    vi.useRealTimers()
  })

  it('member switch: warms the slot the pane points at NOW, not the one it left', () => {
    vi.useFakeTimers()
    // MembersPage re-points one ChatPane instance (no key prop) from one member
    // to the next; the old hydrate query loses its observer, the new one gains
    // it. A late reconnect must chase the current slot only.
    const leaveA = mountPane('member-a')
    leaveA()
    mountPane('member-b')
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(fetchedSlots()).toContain('member-b')
    expect(fetchedSlots()).not.toContain('member-a')

    unmount()
    vi.useRealTimers()
  })

  it('never warms the active slot even when a pane observes it', () => {
    vi.useFakeTimers()
    mountPane(ACTIVE)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots()).not.toContain(ACTIVE)
    // The active slot's only traffic is its unbounded refresh, once.
    expect(fetchedSlots().filter(s => s === ACTIVE)).toEqual([ACTIVE])

    unmount()
    vi.useRealTimers()
  })

  it('warms a slot that is both a split member and an observed pane exactly once', () => {
    vi.useFakeTimers()
    saveLayout(null, split('s', [sLeaf('a', ACTIVE), sLeaf('b', BG)]))
    mountPane(BG) // the split pane IS a ChatPane, so it observes its hydrate query too
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    expect(warmedSlots().sort()).toEqual([BG, MEMBER].sort())
    expect(fetchedSlots().filter(s => s === BG)).toHaveLength(1)

    unmount()
    vi.useRealTimers()
  })

  it('does not warm on the FIRST connect, only on reconnect', () => {
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    expect(fetchedSlots()).toEqual([])

    unmount()
  })

  it('duplicate reconnects: each reconnect warms the pane once, never accumulating', () => {
    vi.useFakeTimers()
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()
    expect(warmedSlots()).toEqual([MEMBER])
    dropAndReconnect()
    expect(warmedSlots()).toEqual([MEMBER])
    dropAndReconnect()
    expect(warmedSlots()).toEqual([MEMBER])

    unmount()
    vi.useRealTimers()
  })

  it('missed tool completion + _done: the warm lands the server rows and idles the pane', async () => {
    vi.useFakeTimers()
    // At the drop the pane holds a tool_call row and a tool_running indicator.
    // While the socket is dead the gateway emits the tool result, a final
    // reply and _done — none reach this client. The reconnect warm must land
    // the canonical transcript and settle the indicator, with no manual
    // refresh and no re-send.
    const held = [row('u1', 'user', 'run it'), row('t1', 'tool', '🔧 shell', { kind: 'tool_call', tool_call_id: 'tc1' })]
    testStore = createTestStore({
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: ACTIVE,
        slotMessages: { [MEMBER]: held },
        slotRun: { [MEMBER]: { state: 'tool_running' } },
      },
    })
    const canonical = [
      ...held,
      row('t2', 'tool', '✅ done', { kind: 'tool_result', tool_call_id: 'tc1' }),
      row('a1', 'assistant', 'All finished.'),
    ]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: canonical, running: false, has_more: false, total: canonical.length, queue: [],
    })
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()
    // A cached (non-empty) slot warms whole by the thunk's own design.
    expect(api.chatSlotDetail).toHaveBeenCalledWith(MEMBER)

    await settle()
    const chat = testStore.getState().chat
    expect(chat.slotRun[MEMBER]?.state).toBe('idle')
    expect((chat.slotMessages[MEMBER] ?? []).map(m => m.meta?.mid)).toEqual(['u1', 't1', 't2', 'a1'])

    unmount()
  })

  it('still running through the reconnect: rows land, the indicator is not idled', async () => {
    vi.useFakeTimers()
    const held = [row('u1', 'user', 'run it'), row('t1', 'tool', '🔧 shell', { kind: 'tool_call', tool_call_id: 'tc1' })]
    testStore = createTestStore({
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: ACTIVE,
        slotMessages: { [MEMBER]: held },
        slotRun: { [MEMBER]: { state: 'tool_running' } },
      },
    })
    const canonical = [...held, row('t2', 'tool', '✅ done', { kind: 'tool_result', tool_call_id: 'tc1' }), row('t3', 'tool', '🔧 next', { kind: 'tool_call', tool_call_id: 'tc2' })]
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: canonical, running: true, has_more: false, total: canonical.length, queue: [],
    })
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    await settle()
    const chat = testStore.getState().chat
    expect(chat.slotRun[MEMBER]?.state).toBe('tool_running')
    expect((chat.slotMessages[MEMBER] ?? []).map(m => m.meta?.mid)).toEqual(['u1', 't1', 't2', 't3'])

    unmount()
  })

  it('late snapshot vs fresh events: a row streamed after the snapshot survives the warm', async () => {
    vi.useFakeTimers()
    // The warm's response is a point-in-time page. A live frame that lands
    // between the fetch and its fulfilment (a newer row the page does not
    // know) must not be wiped by the older page.
    const held = [row('u1', 'user', 'run it')]
    testStore = createTestStore({
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: ACTIVE,
        slotMessages: { [MEMBER]: held },
      },
    })
    let resolveDetail: (v: unknown) => void = () => {}
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockImplementation((slot: string) => slot === MEMBER
      ? new Promise(r => { resolveDetail = r })
      : Promise.resolve({ messages: [], running: false, has_more: false, total: 0, queue: [] }))
    mountPane(MEMBER)
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    connectDropReconnect()

    // Fresh event first: the pane's store gains a newer row on the live path.
    const fresh = row('a9', 'assistant', 'newer than the page')
    act(() => {
      testStore.dispatch({ type: 'chat/sseChatMessage', payload: { ...fresh, slot: MEMBER } })
    })
    // Then the older snapshot fulfils.
    resolveDetail({ messages: [...held, row('a1', 'assistant', 'older')], running: false, has_more: false, total: 2, queue: [] })
    await settle()

    const mids = (testStore.getState().chat.slotMessages[MEMBER] ?? []).map(m => m.meta?.mid)
    expect(mids).toContain('a9')
    expect(mids).toContain('a1')

    unmount()
  })
})
