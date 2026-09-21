/**
 * useWebSocket `notification` frame -> MC_LIVE_NOTIFICATION_EVENT relay.
 *
 * The in-app banner listens to this event, not to the store, so the socket
 * layer must (a) fire it for a frame received on a live connection and
 * (b) NOT fire it during a reconnect catch-up replay, where the frames are
 * history the bell already holds. The store still receives every frame
 * either way — suppression is about the banner, never about the inbox.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { MC_LIVE_NOTIFICATION_EVENT, type McLiveNotificationDetail } from '../hooks/notificationEvent'

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

  simulateClose() {
    this.readyState = 3
    this.onclose?.(new CloseEvent('close'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

describe('useWebSocket notification -> MC_LIVE_NOTIFICATION_EVENT', () => {
  let queryClient: QueryClient
  let store: ReturnType<typeof createTestStore>
  const seen: string[] = []
  const listener = (e: Event) => { seen.push((e as CustomEvent<McLiveNotificationDetail>).detail.note.ts) }

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    seen.length = 0
    store = createTestStore()
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    window.addEventListener(MC_LIVE_NOTIFICATION_EVENT, listener)
  })

  afterEach(() => {
    window.removeEventListener(MC_LIVE_NOTIFICATION_EVENT, listener)
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store },
      createElement(QueryClientProvider, { client: queryClient }, children))
  }

  it('fires for a frame on a live connection, carrying the whole note', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => {
      ws.simulateMessage({ type: 'notification', data: { kind: 'cron', ts: 'live-1', title: 'done', body: '' } })
    })
    expect(seen).toEqual(['live-1'])
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual(['live-1'])
  })

  it('stays silent for frames replayed during a reconnect catch-up, while the store still receives them', () => {
    vi.useFakeTimers()
    renderHook(() => useWebSocket(), { wrapper })
    const first = WS_INSTANCES[0]
    act(() => { first.simulateOpen() })
    act(() => { first.simulateClose() })
    // The reconnect backoff opens a second socket; its open handler marks the
    // catch-up window, which stays open until fetchSlots settles. Delivering
    // the replayed frame inside the same act keeps it in that window.
    act(() => { vi.runOnlyPendingTimers() })
    const second = WS_INSTANCES[1]
    expect(second).toBeTruthy()
    act(() => {
      second.simulateOpen()
      second.simulateMessage({ type: 'notification', data: { kind: 'cron', ts: 'replayed-1', title: 'old', body: '' } })
    })
    expect(seen).toEqual([])
    expect(store.getState().notifications.items.map(n => n.ts)).toContain('replayed-1')
  })
})
