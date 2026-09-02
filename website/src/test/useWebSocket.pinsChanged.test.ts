/**
 * Verifies that a `pins_changed` WebSocket frame triggers `invalidateQueries`
 * for the named slot's chat-pins cache entry.
 *
 * A second tab's pin (or an API-created pin) would otherwise be invisible in
 * the current tab until remount because React Query's ['chat-pins', slotKey]
 * cache has no other external invalidation path.
 *
 * The frame carries slot_key only — no pin content — so nothing sensitive
 * crosses the WebSocket to any listener.
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

describe('useWebSocket pins_changed frame', () => {
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

  it('invalidates the chat-pins query for the named slot', () => {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'pins_changed', data: { slot_key: 'dashboard:chat-42' } })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-pins', 'dashboard:chat-42']))
  })

  it('does not invalidate when slot_key is missing', () => {
    // A malformed frame must not trigger a broad invalidation over all slots.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'pins_changed', data: {} })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys.some(k => k?.includes('chat-pins'))).toBe(false)
  })

  it('invalidates only the named slot, not other slots', () => {
    // A pin change on slot A must not invalidate slot B's cache.
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()

    act(() => {
      ws.simulateMessage({ type: 'pins_changed', data: { slot_key: 'dashboard:chat-1' } })
    })

    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['chat-pins', 'dashboard:chat-1']))
    expect(keys).not.toContain(JSON.stringify(['chat-pins', 'dashboard:chat-2']))
  })
})
