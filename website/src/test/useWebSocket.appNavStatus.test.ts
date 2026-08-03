/**
 * app_event -> appStatus slice (issue #520), through the real dispatch adapter
 * in `useWebSocket.ts`. All app-published events ride under one namespaced WS
 * type: the gateway's EventBus (build_broadcast_fn) sends
 * ``{type:'app_event', data:{app, event, data}}`` — the real event name is in
 * `data.event`, the app in `data.app`, and the event payload nested at
 * `data.data`. The client routes an ``app_nav_status`` app event to the sidebar
 * slice and ignores every other app event.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { selectAppNavState } from '../store/appStatusSlice'

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
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

/** Build the wire frame the gateway actually sends for an app event. */
function appEvent(app: string | undefined, event: string, payload: object) {
  const envelope: Record<string, unknown> = { event, data: payload }
  if (app !== undefined) envelope.app = app
  return { type: 'app_event', data: envelope }
}

describe('useWebSocket app_event -> nav status', () => {
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

  function send(frame: object) {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage(frame) })
  }

  it('dispatches an app_nav_status app event into the appStatus slice', () => {
    send(appEvent('midway-status', 'app_nav_status', { tone: 'caution', label: 'Expiring 12m' }))
    expect(selectAppNavState(testStore.getState(), 'midway-status')).toEqual({ tone: 'caution', label: 'Expiring 12m' })
  })

  it('ignores an app event whose event name is not app_nav_status', () => {
    send(appEvent('midway-status', 'something_else', { tone: 'caution', label: 'x' }))
    expect(selectAppNavState(testStore.getState(), 'midway-status')).toBeNull()
  })

  it('ignores a frame with no app', () => {
    send(appEvent(undefined, 'app_nav_status', { tone: 'critical', label: 'x' }))
    expect(selectAppNavState(testStore.getState(), 'midway-status')).toBeNull()
  })

  it('ignores a frame with no tone', () => {
    send(appEvent('midway-status', 'app_nav_status', { label: 'x' }))
    expect(selectAppNavState(testStore.getState(), 'midway-status')).toBeNull()
  })
})
