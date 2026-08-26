/**
 * Regression: the WS `slots` frame carries the gateway's GitLab-hosts allowlist
 * generation, and the client uses it to invalidate its cached ['dashboardConfig']
 * query instead of polling.
 *
 * The trap this pins: the generation is PROCESS-local to the gateway. After a
 * restart it can hand out a number equal to the one this client last saw even
 * though the allowlist on disk changed — so comparing "changed?" across a
 * reconnect silently skips the refetch and the client keeps a stale allowlist
 * (self-hosted MR links stop being recognized). Each connection must therefore
 * treat its FIRST generation frame as unknown and refetch, then compare
 * normally within that connection.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
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

describe('useWebSocket GitLab allowlist invalidation', () => {
  let testStore: ReturnType<typeof createTestStore>
  let invalidated: unknown[][]
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    invalidated = []
    testStore = createTestStore({})
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const realInvalidate = qc.invalidateQueries.bind(qc)
    qc.invalidateQueries = ((filters?: { queryKey?: unknown[] }) => {
      if (filters?.queryKey) invalidated.push(filters.queryKey)
      return realInvalidate(filters as never)
    }) as typeof qc.invalidateQueries
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  const configInvalidations = () =>
    invalidated.filter(key => Array.isArray(key) && key[0] === 'dashboardConfig').length

  it('invalidates on the first generation frame of a connection', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => { ws.simulateMessage({ type: 'slots', data: [], gitlabHostsGeneration: 3 }) })
    expect(configInvalidations()).toBe(1)

    // Same generation within the connection: no further refetch.
    act(() => { ws.simulateMessage({ type: 'slots', data: [], gitlabHostsGeneration: 3 }) })
    expect(configInvalidations()).toBe(1)

    // A real change within the connection does refetch.
    act(() => { ws.simulateMessage({ type: 'slots', data: [], gitlabHostsGeneration: 4 }) })
    expect(configInvalidations()).toBe(2)
  })

  it('invalidates after a reconnect even when the generation is unchanged', () => {
    vi.useFakeTimers()
    renderHook(() => useWebSocket(), { wrapper })
    const ws1 = WS_INSTANCES[0]
    act(() => { ws1.simulateOpen() })
    act(() => { ws1.simulateMessage({ type: 'slots', data: [], gitlabHostsGeneration: 3 }) })
    expect(configInvalidations()).toBe(1)

    // Gateway restarts: same generation number, possibly a different allowlist.
    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) })
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })
    act(() => { ws2.simulateMessage({ type: 'slots', data: [], gitlabHostsGeneration: 3 }) })

    expect(configInvalidations()).toBe(2)
  })
})
