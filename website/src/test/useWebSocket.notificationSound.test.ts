/**
 * useWebSocket `notification` event -> MC_NOTIFICATION_EVENT relay.
 *
 * The WS transport must fire MC_NOTIFICATION_EVENT — not merely dispatch
 * addNotification (toast/badge) — for a `notification` frame, so
 * useNotificationSound plays a sound. This pins that relay.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { MC_NOTIFICATION_EVENT, type McNotificationDetail } from '../hooks/notificationEvent'

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

describe('useWebSocket notification -> MC_NOTIFICATION_EVENT', () => {
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

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: createTestStore() },
      createElement(QueryClientProvider, { client: queryClient }, children))
  }

  it('fires MC_NOTIFICATION_EVENT on a notification frame so sounds play', async () => {
    const kinds: (string | undefined)[] = []
    const listener = (e: Event) => {
      kinds.push((e as CustomEvent<McNotificationDetail>).detail?.kind)
    }
    window.addEventListener(MC_NOTIFICATION_EVENT, listener as EventListener)
    try {
      renderHook(() => useWebSocket(), { wrapper })
      const ws = WS_INSTANCES[0]
      act(() => { ws.simulateOpen() })

      act(() => {
        // Frame shape mirrors the transport: { type, data: <Notification> }.
        ws.simulateMessage({ type: 'notification', data: { kind: 'cron', ts: '1.0', title: 'done' } })
      })

      // The relay fired exactly once, carrying the notification kind.
      expect(kinds).toEqual(['cron'])
    } finally {
      window.removeEventListener(MC_NOTIFICATION_EVENT, listener as EventListener)
    }
  })

  it('fires MC_NOTIFICATION_EVENT with kind "approval" on an approval frame so sounds play', async () => {
    const kinds: (string | undefined)[] = []
    const listener = (e: Event) => {
      kinds.push((e as CustomEvent<McNotificationDetail>).detail?.kind)
    }
    window.addEventListener(MC_NOTIFICATION_EVENT, listener as EventListener)
    try {
      renderHook(() => useWebSocket(), { wrapper })
      const ws = WS_INSTANCES[0]
      act(() => { ws.simulateOpen() })

      act(() => {
        ws.simulateMessage({ type: 'approval', data: { id: 'test-1', tool: 'shell', source: 'agent', slot: 'slot-1' } })
      })

      expect(kinds).toEqual(['approval'])
    } finally {
      window.removeEventListener(MC_NOTIFICATION_EVENT, listener as EventListener)
    }
  })
})
