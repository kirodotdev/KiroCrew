/**
 * useWebSocket `notifications_clear` -> empty the Redux notification list.
 *
 * Pins the cross-view desync from the missing clear broadcast: the view that
 * clears the inbox self-heals via `clearNotifications.fulfilled`, but every
 * OTHER connected view (second window, another tab, embedded Instances
 * viewport) only converges through the WS event. Without this case the other
 * view keeps stale `items`, and the bell badge — derived as
 * `items.filter(n => !n.acked && n.priority !== 'passive' && !n.silenced)` in
 * `NotificationsBellButton` — keeps showing the pre-clear unread count.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { addNotification } from '../store/notificationsSlice'
import type { Notification } from '../types'

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

/** The badge derivation from NotificationsBellButton (App.tsx): unacked,
 *  non-passive, non-silenced rows. */
const badgeCount = (items: Notification[]) =>
  items.filter(n => !n.acked && n.priority !== 'passive' && !n.silenced).length

describe('useWebSocket notifications_clear', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('empties items (and therefore the bell badge) on a notifications_clear frame', async () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    // This store models the NON-clearing view: it never dispatched the clear
    // thunk, so only the WS frame can drain its copy of the list.
    act(() => {
      store.dispatch(addNotification({ kind: 'cron', title: 'A', body: 'b', ts: '1' }))
      store.dispatch(addNotification({ kind: 'approval', title: 'B', body: 'b', ts: '2' }))
    })
    expect(badgeCount(store.getState().notifications.items)).toBe(2)

    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'notifications_clear', data: {} })
    })

    expect(store.getState().notifications.items).toEqual([])
    expect(badgeCount(store.getState().notifications.items)).toBe(0)
  })

  it('is idempotent: a notifications_clear frame on an empty list is a no-op', async () => {
    const store = createTestStore()
    function wrapper({ children }: { children: React.ReactNode }) {
      return createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    }
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({ type: 'notifications_clear', data: {} })
    })

    expect(store.getState().notifications.items).toEqual([])
  })
})
