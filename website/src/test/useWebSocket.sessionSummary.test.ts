/**
 * The two halves of the session summary panel's freshness contract, both in
 * `useWebSocket.ts`.
 *
 * The panel deliberately does not poll, so a push it never receives is not a
 * late update — it is no update at all until the tab remounts. That makes both
 * of these paths load-bearing rather than nice-to-have:
 *
 * 1. The live `session_summary` frame must invalidate that slot's query. The
 *    backend has to send a TYPED envelope for this to be reachable at all; when
 *    it fell into the generic `notification` envelope the handler below was dead
 *    code (see `test_session_summary_api.py::TestSessionSummaryBroadcast`).
 * 2. A reconnect must invalidate too. A summary regenerated while the socket was
 *    down pushed a frame nobody received, and nothing else would ever refetch it.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'

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

describe('useWebSocket session_summary freshness', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
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

  it('invalidates that slot\'s summary on a live frame', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()  // ignore the on-open burst; this asserts the frame's effect
    act(() => { ws.simulateMessage({ type: 'session_summary', data: { key: 'dashboard:chat-7' } }) })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['session-summary', 'dashboard:chat-7']))
  })

  it('does not invalidate when the frame carries no key', () => {
    // A keyless frame cannot name a slot, and invalidating every summary on a
    // malformed push would refetch each open panel for nothing.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()
    act(() => { ws.simulateMessage({ type: 'session_summary', data: {} }) })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys.some(k => k?.includes('session-summary'))).toBe(false)
  })

  it('invalidates summaries on a reconnect, not just on a live frame', () => {
    // Catch-up path: a summary pushed while the socket was down was never
    // delivered, and the panel does not poll, so only this invalidation gets it.
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    const spy = vi.spyOn(qc, 'invalidateQueries')
    act(() => { ws.onclose?.(new CloseEvent('close')) })
    const reconnected = WS_INSTANCES[WS_INSTANCES.length - 1]
    act(() => { reconnected.simulateOpen() })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['session-summary']))
  })
})
