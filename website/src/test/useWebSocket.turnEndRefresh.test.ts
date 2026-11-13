/**
 * A `turn_end` refresh frame defers the cron caches and never re-reads the
 * Older Sessions listing, which drops open slots and so cannot change when a
 * turn ends. A `history` frame (a close or a delete) still re-reads it.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark', colorTheme: 'default', themeVersion: 0 }) }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
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

describe('useWebSocket turn_end refresh', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    // The turn-end cron window is a module timer; fake timers keep it from
    // firing into another test.
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    qc.clear()
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function openSocket() {
    const spy = vi.spyOn(qc, 'invalidateQueries')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    spy.mockClear()
    vi.mocked(api.sessions).mockClear()
    return { ws, spy }
  }

  /** Cron-job invalidations that would refetch a cache whose last read succeeded. */
  const unfilteredCronCalls = (spy: ReturnType<typeof vi.spyOn>) =>
    spy.mock.calls.filter(c => JSON.stringify(c[0]?.queryKey) === JSON.stringify(['cron-jobs']) && !c[0]?.predicate)

  it('turn_end defers the cron caches and skips the history fetch', () => {
    const { ws, spy } = openSocket()
    act(() => { ws.simulateMessage({ type: 'refresh', data: { kinds: ['turn_end'] } }) })

    expect(api.sessions).not.toHaveBeenCalled()
    expect(unfilteredCronCalls(spy)).toEqual([])
    const keys = spy.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
    expect(keys).toContain(JSON.stringify(['kirocrewConfig']))
  })

  it('history still fetches the Older Sessions listing', () => {
    const { ws } = openSocket()
    act(() => { ws.simulateMessage({ type: 'refresh', data: { kinds: ['history'] } }) })

    expect(api.sessions).toHaveBeenCalledTimes(1)
  })
})
