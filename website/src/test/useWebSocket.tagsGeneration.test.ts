/**
 * A tag or board column written outside this tab -- by an agent through the
 * dashboard MCP tools, or a second tab -- stayed invisible here until a reload.
 * `['chat-tags']` and `['tag-columns']` inherit the app-wide
 * `staleTime: Infinity` and were only invalidated by this tab's own mutations.
 *
 * The `slots` frame now carries `tagsGeneration`, the folder generation's twin.
 * These tests pin the same three properties as the folder generation test:
 *
 *   1. a changed generation invalidates both keys,
 *   2. an unchanged generation does not (a `slots` frame fires on routine
 *      session activity),
 *   3. the first generation frame of each connection invalidates, because the
 *      counter is process-local to the gateway.
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

describe('useWebSocket tag-vocabulary / board-column generation invalidation', () => {
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

  const count = (name: string) =>
    invalidated.filter(key => Array.isArray(key) && key[0] === name).length

  function mountOpened(): MockWebSocket {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('invalidates both keys on the first generation frame of a connection', () => {
    const ws = mountOpened()

    act(() => { ws.simulateMessage({ type: 'slots', data: [], tagsGeneration: 2 }) })

    expect(count('chat-tags')).toBe(1)
    expect(count('tag-columns')).toBe(1)
  })

  it('invalidates both keys when the generation moves -- an agent wrote a tag', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [], tagsGeneration: 2 }) })

    act(() => { ws.simulateMessage({ type: 'slots', data: [], tagsGeneration: 3 }) })

    expect(count('chat-tags')).toBe(2)
    expect(count('tag-columns')).toBe(2)
  })

  it('does not invalidate on routine slot activity at an unchanged generation', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [], tagsGeneration: 2 }) })

    act(() => { ws.simulateMessage({ type: 'slots', data: [{ key: 'a', title: 'x', agent: 'kirocrew' }], tagsGeneration: 2 }) })
    act(() => { ws.simulateMessage({ type: 'slots', data: [{ key: 'b', title: 'y', agent: 'kirocrew' }], tagsGeneration: 2 }) })

    expect(count('chat-tags')).toBe(1)
    expect(count('tag-columns')).toBe(1)
  })

  it('invalidates after a reconnect even when the generation is unchanged', () => {
    vi.useFakeTimers()
    const ws1 = mountOpened()
    act(() => { ws1.simulateMessage({ type: 'slots', data: [], tagsGeneration: 2 }) })
    expect(count('chat-tags')).toBe(1)

    // The counter is process-local: generation 2 on a restarted gateway names
    // a different vocabulary than generation 2 on the old one.
    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) })
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })
    act(() => { ws2.simulateMessage({ type: 'slots', data: [], tagsGeneration: 2 }) })

    expect(count('chat-tags')).toBe(2)
    expect(count('tag-columns')).toBe(2)
  })
})
