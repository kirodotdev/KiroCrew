/**
 * The artifact library's freshness contract in `useWebSocket.ts` (#10867).
 *
 * The Artifacts page does not poll. Live `artifact_update` frames keep its
 * queries fresh while the socket is up — but a frame pushed while the socket
 * was DOWN was never delivered, and a list query that errored during the gap
 * (gateway restart 403s / connection refused) holds no data at all. Nothing
 * else would ever refetch it, so the tab renders an empty library until a
 * manual hard refresh. Two heal paths close that:
 *
 * 1. The server's generic `refresh` broadcast must invalidate the
 *    ['artifacts'] prefix (filtered list + all-tags) like the other
 *    refresh-driven caches.
 * 2. A reconnect must invalidate it too — same catch-up reasoning as the
 *    session-summary invalidation beside it.
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

describe('useWebSocket artifact library freshness', () => {
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

  it("invalidates ['artifacts'] on the server's refresh broadcast", () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()  // ignore the on-open burst; this asserts the frame's effect
    // The handler reads `data.kinds` — the envelope nests payload under `data`.
    act(() => { ws.simulateMessage({ type: 'refresh', data: { kinds: [] } }) })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['artifacts']))
    // The folder list fails under the same trigger and renders `?? []` the
    // same way — the broadcast heals both or the library recovers half-empty.
    expect(keys).toContain(JSON.stringify(['artifact-folders']))
  })

  it("invalidates ['artifacts'] on a reconnect, not just on a live frame", () => {
    // Catch-up path: `artifact_update` frames pushed while the socket was down
    // were never delivered, and the page does not poll. A prefix invalidation
    // also flips an ERRORED list query back to a refetch, which is the #10867
    // recovery: without it a window that 403'd across a gateway restart shows
    // an empty library until a manual hard refresh.
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    const spy = vi.spyOn(qc, 'invalidateQueries')
    act(() => { ws.onclose?.(new CloseEvent('close')) })
    const reconnected = WS_INSTANCES[WS_INSTANCES.length - 1]
    act(() => { reconnected.simulateOpen() })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['artifacts']))
    expect(keys).toContain(JSON.stringify(['artifact-folders']))
  })

  it('does not invalidate the library on first connect', () => {
    // First connect has no missed-frame gap: the page's own mount fetch is the
    // authoritative read, and invalidating here would double-fetch every load.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).not.toContain(JSON.stringify(['artifacts']))
    expect(keys).not.toContain(JSON.stringify(['artifact-folders']))
  })
})
