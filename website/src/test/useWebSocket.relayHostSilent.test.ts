/**
 * Through the Kiro relay, a Crew host that drops off the network leaves the
 * page's socket open (the relay keeps it) and the next upgrade is accepted
 * without the host. In the desktop relay window the silence watchdog therefore
 * takes the dashboard offline and only a frame from the host brings it back;
 * the same frame is what ends a Remote Control off outage, whose reason must
 * survive the reconnects in between.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import chatReducer from '../store/chatSlice'

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
  static CLOSED = 3
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })
  constructor() { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateFrame() { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'dashboard' }) })) }
  simulateClose(code: number) { this.readyState = MockWebSocket.CLOSED; this.onclose?.(new CloseEvent('close', { code })) }
}

const last = () => WS_INSTANCES[WS_INSTANCES.length - 1]

describe('useWebSocket in the desktop relay window', () => {
  let testStore: ReturnType<typeof createTestStore>
  const realUa = navigator.userAgent

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    vi.useFakeTimers()
    Object.defineProperty(navigator, 'userAgent', { value: `${realUa} kiroCrewRelayWindow`, configurable: true })
    testStore = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })
  afterEach(() => {
    vi.unstubAllGlobals(); vi.useRealTimers()
    Object.defineProperty(navigator, 'userAgent', { value: realUa, configurable: true })
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore }, createElement(QueryClientProvider, { client: qc }, children))
  }

  it('a silent socket takes the dashboard offline until the host sends a frame', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })
    expect(testStore.getState().dashboard.connected).toBe(true)

    act(() => { vi.advanceTimersByTime(30_000) })
    expect(WS_INSTANCES[0].close).toHaveBeenCalled()
    expect(testStore.getState().dashboard.connected).toBe(false)

    act(() => { vi.advanceTimersByTime(5_000) })
    const next = last()
    expect(next).not.toBe(WS_INSTANCES[0])
    act(() => { next.simulateOpen() })
    // The relay accepted the upgrade without the host: still offline.
    expect(testStore.getState().dashboard.connected).toBe(false)
    act(() => { next.simulateFrame() })
    expect(testStore.getState().dashboard.connected).toBe(true)
    unmount()
  })

  it('drops a reopened socket that carries no frame', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })
    act(() => { vi.advanceTimersByTime(30_000) })
    act(() => { vi.advanceTimersByTime(5_000) })
    const next = last()
    act(() => { next.simulateOpen() })
    act(() => { vi.advanceTimersByTime(16_000) })
    expect(next.close).toHaveBeenCalled()
    expect(testStore.getState().dashboard.connected).toBe(false)
    unmount()
  })

  it('keeps "Remote Control off" across the relay accepting and closing later sockets', () => {
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })
    act(() => { WS_INSTANCES[0].simulateFrame() })
    act(() => { WS_INSTANCES[0].simulateClose(4410) })
    expect(testStore.getState().dashboard.offlineCause).toBe('remote_off')

    act(() => { vi.advanceTimersByTime(5_000) })
    const second = last()
    act(() => { second.simulateOpen() })
    expect(testStore.getState().dashboard.connected).toBe(false)
    expect(testStore.getState().dashboard.offlineCause).toBe('remote_off')
    act(() => { second.simulateClose(4404) })
    expect(testStore.getState().dashboard.offlineCause).toBe('remote_off')

    act(() => { vi.advanceTimersByTime(20_000) })
    const third = last()
    act(() => { third.simulateOpen() })
    act(() => { third.simulateFrame() })
    expect(testStore.getState().dashboard.connected).toBe(true)
    expect(testStore.getState().dashboard.offlineCause).toBeNull()
    unmount()
  })
})
