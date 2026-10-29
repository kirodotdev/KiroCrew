/**
 * Verifies that a `reactions_changed` WebSocket frame triggers
 * `invalidateQueries` for the named slot's chat-reactions cache entry.
 *
 * A reaction toggled on a second tab or pop-out (or via the API) would
 * otherwise be invisible in the current tab until remount, because React
 * Query's ['chat-reactions', slotKey] cache carries the app-wide
 * staleTime: Infinity and has no other external invalidation path. Mirrors
 * useWebSocket.pinsChanged.test.ts.
 *
 * The frame carries slot_key only — no reaction content — so nothing crosses
 * the WebSocket to any listener.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { registerRecoverableQueryClient } from '../api/queryClient'
import {
  reactionTogglePending,
  __resetReactionToggleLedgerForTests,
  __setReactionTogglePendingForTests,
} from '../hooks/useChatReactions'

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

  constructor() { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

describe('useWebSocket reactions_changed frame', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    __resetReactionToggleLedgerForTests()
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  it('invalidates the chat-reactions query for the named slot', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'reactions_changed', data: { slot_key: 'dashboard:chat-42' } })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'dashboard:chat-42']))
  })

  it('does not invalidate when slot_key is missing', () => {
    // A malformed frame must not trigger a broad invalidation over all slots.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'reactions_changed', data: {} })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys.some(k => k?.includes('chat-reactions'))).toBe(false)
  })

  it('invalidates only the named slot, not other slots', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'reactions_changed', data: { slot_key: 'dashboard:chat-1' } })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'dashboard:chat-1']))
    expect(keys).not.toContain(JSON.stringify(['chat-reactions', 'dashboard:chat-2']))
  })

  it('DEFERS invalidation while a local toggle is in flight for that slot', () => {
    // The race the AI review flagged: a `reactions_changed` broadcast (from
    // reacting on message A) must not refetch and drop message B's pending
    // optimistic entry, or B's queued off-toggle reads B as absent and POSTs
    // instead of DELETEs. With a toggle pending for the slot, the frame holds
    // the refetch back rather than firing it.
    __setReactionTogglePendingForTests(qc, 'dashboard:chat-42', 1)
    expect(reactionTogglePending(qc, 'dashboard:chat-42')).toBe(true)

    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'reactions_changed', data: { slot_key: 'dashboard:chat-42' } })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['chat-reactions', 'dashboard:chat-42']))
  })

  it('still invalidates a DIFFERENT slot while one slot has a toggle in flight', () => {
    // The deferral is per-slot: a toggle pending on chat-42 must not hold back
    // a cross-tab change on chat-7.
    __setReactionTogglePendingForTests(qc, 'dashboard:chat-42', 1)

    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'reactions_changed', data: { slot_key: 'dashboard:chat-7' } })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'dashboard:chat-7']))
  })

  it('also invalidates the slot on a registered app client (an embedded app chat)', () => {
    // An app's embedded ChatPanel holds this slot's reactions on its own
    // QueryClient; the frame must refresh that copy as well as the dashboard's.
    const app = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    registerRecoverableQueryClient(app)
    const appSpy = vi.spyOn(app, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    appSpy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'reactions_changed', data: { slot_key: 'dashboard:chat-42' } })
    })

    const keys = appSpy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-reactions', 'dashboard:chat-42']))
  })
})
