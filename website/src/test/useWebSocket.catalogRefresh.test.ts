/**
 * Which `refresh` frames move the counter the agent catalog re-reads on.
 *
 * `refreshTrigger` moves for every frame; `catalogRefreshTrigger` only for a frame that
 * can have changed an agent or a crewmate. A cron run pushes a frame when it starts and
 * when it ends, and no cron run changes either.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'

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

describe('useWebSocket refresh frames and the catalog counter', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
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

  function counters() {
    const d = testStore.getState().dashboard
    return { refresh: d.refreshTrigger, catalog: d.catalogRefreshTrigger }
  }

  function send(kinds: string[]) {
    const before = counters()
    act(() => { WS_INSTANCES[0].simulateMessage({ type: 'refresh', data: { kinds } }) })
    const after = counters()
    return { refresh: after.refresh - before.refresh, catalog: after.catalog - before.catalog }
  }

  beforeEach(() => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })
  })

  it.each([['crons'], ['cron_history'], ['lessons'], ['taskrunner'], ['update_available']])(
    'moves the refresh counter but not the catalog counter for %s',
    kind => {
      expect(send([kind])).toEqual({ refresh: 1, catalog: 0 })
    },
  )

  it.each([['agents'], ['config'], ['something-new'], []])(
    'moves both counters for %j',
    (...kinds) => {
      expect(send(kinds.flat() as string[])).toEqual({ refresh: 1, catalog: 1 })
    },
  )

  it('moves both counters for the first turn end after a quiet window', () => {
    expect(send(['turn_end'])).toEqual({ refresh: 1, catalog: 1 })
  })

  it('moves both counters for a cron frame that also names an agent write', () => {
    expect(send(['crons', 'agents'])).toEqual({ refresh: 1, catalog: 1 })
  })
})
