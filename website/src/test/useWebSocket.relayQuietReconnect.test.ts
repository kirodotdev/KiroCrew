/**
 * A dashboard opened through the Kiro relay (a Kiro Mobile WebView or a
 * desktop relay Crew window) loses its socket every few minutes on purpose:
 * the relay closes it with 4401 when the token it opened with expires, and with
 * 1012 when it drains. The app has already renewed the token, so the next
 * socket opens about a second later. Those closes must not flash "offline";
 * only a reconnect that fails says so, with the relay's reason.
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

// `QUIET_RECONNECT_GRACE_MS` in hooks/websocket/relayClose.ts, pinned by its
// own test: only the facade may import an owner from here.
const QUIET_RECONNECT_GRACE_MS = 5000

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
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateClose(code: number) {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close', { code }))
  }
}

describe('useWebSocket quiet reconnect through the relay', () => {
  let testStore: ReturnType<typeof createTestStore>
  let disconnects: number

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    vi.useFakeTimers()
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: 'chat-active' },
    })
    disconnects = 0
    let was = testStore.getState().dashboard.connected
    testStore.subscribe(() => {
      const now = testStore.getState().dashboard.connected
      if (was && !now) disconnects++
      was = now
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function openFirst() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })
    expect(testStore.getState().dashboard.connected).toBe(true)
    return hook
  }

  it.each([4401, 1012])('a %i close reconnects at once without ever showing offline', (code) => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(code) })
    expect(testStore.getState().dashboard.connected).toBe(true)

    act(() => { vi.advanceTimersByTime(0) })
    expect(WS_INSTANCES).toHaveLength(2)
    act(() => { WS_INSTANCES[1].simulateOpen() })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS * 2) })

    expect(testStore.getState().dashboard.connected).toBe(true)
    expect(testStore.getState().dashboard.offlineCause).toBeNull()
    expect(disconnects).toBe(0)
    unmount()
  })

  it('stays quiet across repeated token renewals', () => {
    const { unmount } = openFirst()
    for (let i = 0; i < 3; i++) {
      act(() => { WS_INSTANCES[i].simulateClose(4401) })
      act(() => { vi.advanceTimersByTime(0) })
      act(() => { WS_INSTANCES[i + 1].simulateOpen() })
    }
    expect(disconnects).toBe(0)
    unmount()
  })

  it('says the session ended when the socket after a 4401 is refused at its upgrade', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(0) })
    // The relay's plain 401 at the upgrade: the browser reports 1006, never an open.
    act(() => { WS_INSTANCES[1].simulateClose(1006) })

    expect(testStore.getState().dashboard.connected).toBe(false)
    expect(testStore.getState().dashboard.offlineCause).toBe('session_ended')
    unmount()
  })

  it('says the session ended when the reconnect after a 4401 does not open within the grace', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS - 1) })
    expect(testStore.getState().dashboard.connected).toBe(true)
    act(() => { vi.advanceTimersByTime(1) })

    expect(testStore.getState().dashboard.connected).toBe(false)
    expect(testStore.getState().dashboard.offlineCause).toBe('session_ended')
    // That socket's own close later keeps the reason.
    act(() => { WS_INSTANCES[1].simulateClose(1006) })
    expect(testStore.getState().dashboard.offlineCause).toBe('session_ended')
    unmount()
  })

  it('stops retrying once the reconnect after a 4401 fails at its handshake', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(0) })
    // The relay's 401 at the upgrade: 1006, never an open.
    act(() => { WS_INSTANCES[1].simulateClose(1006) })
    act(() => { vi.advanceTimersByTime(120_000) })

    expect(WS_INSTANCES).toHaveLength(2)
    expect(testStore.getState().dashboard.connected).toBe(false)
    expect(testStore.getState().dashboard.offlineCause).toBe('session_ended')
    unmount()
  })

  it('stops retrying when the reconnect after a 4401 outlives the grace and then fails', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS) })
    act(() => { WS_INSTANCES[1].simulateClose(1006) })
    act(() => { vi.advanceTimersByTime(120_000) })

    expect(WS_INSTANCES).toHaveLength(2)
    expect(testStore.getState().dashboard.offlineCause).toBe('session_ended')
    unmount()
  })

  it('the health probe still brings a stopped session back, and an open clears the reason', () => {
    const { result, unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(0) })
    act(() => { WS_INSTANCES[1].simulateClose(1006) })
    // With no live socket the probe brings the reconnect forward to at most 1s.
    act(() => { result.current.forceReconnect() })
    act(() => { vi.advanceTimersByTime(1000) })
    expect(WS_INSTANCES).toHaveLength(3)
    act(() => { WS_INSTANCES[2].simulateOpen() })
    expect(testStore.getState().dashboard.connected).toBe(true)
    expect(testStore.getState().dashboard.offlineCause).toBeNull()
    unmount()
  })

  it('the health probe brings a stopped session back after an earlier early drop', () => {
    const { result, unmount } = openFirst()
    // A drop before the socket proved healthy, then a reconnect that opens.
    act(() => { WS_INSTANCES[0].simulateClose(1006) })
    act(() => { vi.advanceTimersByTime(1000) })
    act(() => { WS_INSTANCES[1].simulateOpen() })
    // Its token expires and the reconnect after the 4401 is refused at its upgrade.
    act(() => { WS_INSTANCES[1].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(0) })
    act(() => { WS_INSTANCES[2].simulateClose(1006) })
    act(() => { result.current.forceReconnect() })
    act(() => { vi.advanceTimersByTime(1000) })

    expect(WS_INSTANCES).toHaveLength(4)
    unmount()
  })

  it('a health-probe reconnect that drops after the stop goes back to retrying', () => {
    const { result, unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(0) })
    act(() => { WS_INSTANCES[1].simulateClose(1006) })
    act(() => { result.current.forceReconnect() })
    act(() => { vi.advanceTimersByTime(1000) })
    // A network blip before the probe's socket opens.
    act(() => { WS_INSTANCES[2].simulateClose(1006) })
    act(() => { vi.advanceTimersByTime(1000) })

    expect(WS_INSTANCES).toHaveLength(4)
    expect(testStore.getState().dashboard.offlineCause).toBe('session_ended')
    unmount()
  })

  it('a direct dashboard whose handshakes keep failing stays plain offline and keeps retrying', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(1006) })
    for (let i = 1; i <= 6; i++) {
      act(() => { vi.advanceTimersByTime(10_000) })
      expect(WS_INSTANCES).toHaveLength(i + 1)
      act(() => { WS_INSTANCES[i].simulateClose(1006) })
      expect(testStore.getState().dashboard.connected).toBe(false)
      expect(testStore.getState().dashboard.offlineCause).toBeNull()
    }
    unmount()
  })

  it('keeps the reason when the health probe replaces a reconnect that outlived the grace', () => {
    const { result, unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4401) })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS) })
    act(() => { result.current.forceReconnect() })
    act(() => { vi.advanceTimersByTime(0) })
    act(() => { WS_INSTANCES[WS_INSTANCES.length - 1].simulateClose(1006) })

    expect(testStore.getState().dashboard.offlineCause).toBe('session_ended')
    unmount()
  })

  it('a drain whose reconnect fails is a plain offline', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(1012) })
    act(() => { vi.advanceTimersByTime(0) })
    act(() => { WS_INSTANCES[1].simulateClose(1006) })

    expect(testStore.getState().dashboard.connected).toBe(false)
    expect(testStore.getState().dashboard.offlineCause).toBeNull()
    unmount()
  })

  it('a relay refusal shows at once with its reason', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(4429) })

    expect(testStore.getState().dashboard.connected).toBe(false)
    expect(testStore.getState().dashboard.offlineCause).toBe('refused')
    unmount()
  })

  it('a plain drop still shows offline at once, and the next open clears the reason', () => {
    const { unmount } = openFirst()
    act(() => { WS_INSTANCES[0].simulateClose(1006) })
    expect(testStore.getState().dashboard.connected).toBe(false)
    expect(testStore.getState().dashboard.offlineCause).toBeNull()

    act(() => { vi.advanceTimersByTime(1000) })
    act(() => { WS_INSTANCES[1].simulateOpen() })
    expect(testStore.getState().dashboard.connected).toBe(true)
    unmount()
  })
})
