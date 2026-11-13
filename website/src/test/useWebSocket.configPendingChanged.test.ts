/**
 * `config_pending_changed` (owner sockets only): a settings file edited outside
 * the dashboard went pending or was resolved. The frame must mark the shared
 * config-pending read stale so the Settings nav dot and the Security cards
 * refresh without polling.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { CONFIG_PENDING_QUERY_KEY } from '../pages/settings/configPendingQuery'

vi.mock('../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../api/apiError')>('../api/apiError')).ApiError,
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

describe('useWebSocket config_pending_changed frame', () => {
  let qc: QueryClient
  beforeEach(() => {
    WS_INSTANCES.length = 0
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('marks the config-pending read stale', () => {
    const store = createTestStore()
    renderHook(() => useWebSocket(), {
      wrapper: ({ children }: { children: React.ReactNode }) =>
        createElement(Provider, { store }, createElement(QueryClientProvider, { client: qc }, children)),
    })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    // Seeded AFTER the open, so only the frame can invalidate it.
    qc.setQueryData(CONFIG_PENDING_QUERY_KEY, { enforcing: true, count: 0, pending: [] })
    expect(qc.getQueryState(CONFIG_PENDING_QUERY_KEY)?.isInvalidated).toBe(false)
    act(() => { ws.simulateMessage({ type: 'config_pending_changed', data: { count: 1 } }) })
    expect(qc.getQueryState(CONFIG_PENDING_QUERY_KEY)?.isInvalidated).toBe(true)
  })
})
