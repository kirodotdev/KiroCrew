/**
 * The Context tab's two push-side triggers, through the real socket hook: a
 * `usage` session_projection frame, and a reconnect.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { CONTEXT_TRACE_COALESCE_MS as COALESCE_MS, resetContextTraceRefresh } from '../hooks/useWebSocket'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
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

describe('useWebSocket routes usage frames and reconnects to the Context tab', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    resetContextTraceRefresh()
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // A trace the tab already read for this slot.
    qc.setQueryData(['context-trace', 'chat-7'], { slot: 'chat-7', turns: [] })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function traceReads(spy: ReturnType<typeof vi.spyOn>) {
    return spy.mock.calls.filter(c => JSON.stringify((c[0] as { queryKey?: unknown })?.queryKey) === JSON.stringify(['context-trace', 'chat-7']))
  }

  const usage = (revision: number, name = 'usage') => ({
    type: 'session_projection',
    data: { session_id: 'unit-1', slot: 'dashboard:chat-7', name, seq: 1, value: {}, revision },
  })

  it('a usage frame for the slot asks for one trace re-read after the window', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    await act(async () => { ws.simulateOpen(); await new Promise(r => setTimeout(r, 0)) })
    const spy = vi.spyOn(qc, 'invalidateQueries')
    act(() => {
      ws.simulateMessage(usage(3))
      ws.simulateMessage(usage(4))
      ws.simulateMessage(usage(9, 'status'))
    })
    expect(traceReads(spy)).toHaveLength(0)
    await act(async () => { await new Promise(r => setTimeout(r, COALESCE_MS + 20)) })
    expect(traceReads(spy)).toHaveLength(1)
  })

  it('the first connect does not re-read; a reconnect re-reads once', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    const spy = vi.spyOn(qc, 'invalidateQueries')
    await act(async () => { ws.simulateOpen(); await new Promise(r => setTimeout(r, 0)) })
    expect(traceReads(spy)).toHaveLength(0)
    // The same hook opening again is the reconnect path (a gateway restart included).
    await act(async () => { ws.simulateOpen(); await new Promise(r => setTimeout(r, 0)) })
    expect(traceReads(spy)).toHaveLength(1)
  })
})
