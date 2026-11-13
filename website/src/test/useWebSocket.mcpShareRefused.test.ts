/**
 * The gateway records a refused share when it starts, which a tab sees as a new
 * socket connection. The connect handler must mark the shared gateway-status
 * read stale, so the MCP Servers rail count re-reads it without a fast poll.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { MCP_GATEWAY_STATUS_QUERY_KEY } from '../pages/overview/mcpShareRefusedQuery'

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
}

describe('useWebSocket connect and the gateway-status read', () => {
  let qc: QueryClient
  beforeEach(() => {
    WS_INSTANCES.length = 0
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('marks the gateway-status read stale on connect', () => {
    const store = createTestStore()
    renderHook(() => useWebSocket(), {
      wrapper: ({ children }: { children: React.ReactNode }) =>
        createElement(Provider, { store }, createElement(QueryClientProvider, { client: qc }, children)),
    })
    // Seeded BEFORE the open, so only the connect handler can invalidate it.
    qc.setQueryData(MCP_GATEWAY_STATUS_QUERY_KEY, { enabled: true, share_refused: {} })
    expect(qc.getQueryState(MCP_GATEWAY_STATUS_QUERY_KEY)?.isInvalidated).toBe(false)
    act(() => { WS_INSTANCES[0].simulateOpen() })
    expect(qc.getQueryState(MCP_GATEWAY_STATUS_QUERY_KEY)?.isInvalidated).toBe(true)
  })
})
