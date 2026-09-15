/**
 * Reload-after-update, for the install whose version never moves.
 *
 * A git checkout's in-app update (`POST /api/update`) pulls, rebuilds, and
 * restarts the gateway WITHOUT changing `version` — so the 'dashboard' frame's
 * version comparison (the SPA's only reload-on-upgrade trigger before this)
 * never fired, and the tab that clicked Update sat on its stale bundle behind
 * the update overlay's spinner forever. Two independent recoveries fix it:
 *
 *   1. The RESTART LATCH: the `update_progress` frame's `restarting` step is
 *      the last event before the gateway execs itself and the socket dies.
 *      It latches sessionStorage; the next successful RECONNECT consumes the
 *      latch and reloads. Per-tab on purpose — only the tab that watched the
 *      update needs it, and a failed update (`failed`/`error`) disarms it so
 *      an unrelated later blip cannot trigger a surprise reload.
 *
 *   2. The BUNDLE ID: the status frame carries a content hash of the served
 *      index.html; when it moves between pushes the tab's JS is stale no
 *      matter what `version` says. This is what recovers OTHER tabs (and a
 *      tab whose latch was lost), because they never saw the progress events.
 *
 * These tests pin both, plus the guardrails: no reload on an unchanged or
 * empty bundle id, no reload on a reconnect without a fresh latch, and the
 * latch being one-shot.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import {
  useWebSocket,
  consumeUpdateRestartLatch,
  UPDATE_RESTART_LATCH_KEY,
  UPDATE_RESTART_LATCH_TTL_MS,
} from '../hooks/useWebSocket'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })

  constructor(public url: string) { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }

  simulateClose() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close'))
  }
}

describe('consumeUpdateRestartLatch', () => {
  beforeEach(() => sessionStorage.clear())

  it('is false when no latch is set', () => {
    expect(consumeUpdateRestartLatch()).toBe(false)
  })

  it('consumes a fresh latch exactly once', () => {
    sessionStorage.setItem(UPDATE_RESTART_LATCH_KEY, String(Date.now()))
    expect(consumeUpdateRestartLatch()).toBe(true)
    // One-shot: the reload it licenses must not repeat on the next reconnect.
    expect(consumeUpdateRestartLatch()).toBe(false)
    expect(sessionStorage.getItem(UPDATE_RESTART_LATCH_KEY)).toBeNull()
  })

  it('rejects and clears a stale latch', () => {
    const stale = Date.now() - UPDATE_RESTART_LATCH_TTL_MS - 1
    sessionStorage.setItem(UPDATE_RESTART_LATCH_KEY, String(stale))
    expect(consumeUpdateRestartLatch()).toBe(false)
    // Cleared even when rejected, so an abandoned update cannot linger.
    expect(sessionStorage.getItem(UPDATE_RESTART_LATCH_KEY)).toBeNull()
  })

  it('rejects an unparseable latch value', () => {
    sessionStorage.setItem(UPDATE_RESTART_LATCH_KEY, 'not-a-number')
    expect(consumeUpdateRestartLatch()).toBe(false)
  })
})

describe('useWebSocket update-restart reload', () => {
  let testStore: ReturnType<typeof createTestStore>
  let reloadSpy: ReturnType<typeof vi.fn>
  let originalReload: typeof window.location.reload

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    sessionStorage.clear()
    testStore = createTestStore({})
    vi.stubGlobal('WebSocket', MockWebSocket)
    originalReload = window.location.reload
    reloadSpy = vi.fn()
    Object.defineProperty(window.location, 'reload', { configurable: true, value: reloadSpy })
  })

  afterEach(() => {
    Object.defineProperty(window.location, 'reload', { configurable: true, value: originalReload })
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { ...hook, ws }
  }

  it('latches on the restarting step and disarms on failure', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'pulling', detail: '' } }) })
    expect(sessionStorage.getItem(UPDATE_RESTART_LATCH_KEY)).toBeNull()

    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'restarting', detail: '' } }) })
    expect(sessionStorage.getItem(UPDATE_RESTART_LATCH_KEY)).not.toBeNull()

    // A failure after `restarting` (invalid exe path) means no exec is coming:
    // an armed latch would reload over the next unrelated blip.
    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'error', detail: 'bad exe' } }) })
    expect(sessionStorage.getItem(UPDATE_RESTART_LATCH_KEY)).toBeNull()
  })

  it('reloads once on reconnect after a latched restart', () => {
    vi.useFakeTimers()
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'update_progress', data: { step: 'restarting', detail: '' } }) })

    // The gateway execs itself: socket dies, hook schedules a reconnect.
    act(() => { ws.simulateClose() })
    act(() => { vi.advanceTimersByTime(1500) })
    const ws2 = WS_INSTANCES[1]
    expect(ws2).toBeDefined()

    // The reconnected socket is the post-update gateway.
    act(() => { ws2.simulateOpen() })
    expect(reloadSpy).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem(UPDATE_RESTART_LATCH_KEY)).toBeNull()
  })

  it('does not reload on a reconnect with no latch', () => {
    vi.useFakeTimers()
    const { ws } = mount()
    act(() => { ws.simulateClose() })
    act(() => { vi.advanceTimersByTime(1500) })
    act(() => { WS_INSTANCES[1].simulateOpen() })
    expect(reloadSpy).not.toHaveBeenCalled()
  })

  it('reloads when the served bundle id changes across status frames', () => {
    const { ws } = mount()
    act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0', bundle_id: 'aaaa' } }) })
    expect(reloadSpy).not.toHaveBeenCalled()

    // Same bundle: no reload.
    act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0', bundle_id: 'aaaa' } }) })
    expect(reloadSpy).not.toHaveBeenCalled()

    // Rebuilt bundle, SAME version — the case the version check cannot see.
    act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0', bundle_id: 'bbbb' } }) })
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })

  it('never reloads over an empty or absent bundle id', () => {
    const { ws } = mount()
    // '' = no built bundle (dev tree) or older gateway: UNKNOWN, not a change.
    act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0', bundle_id: 'aaaa' } }) })
    act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0', bundle_id: '' } }) })
    act(() => { ws.simulateMessage({ type: 'dashboard', data: { version: '1.0.0' } }) })
    expect(reloadSpy).not.toHaveBeenCalled()
  })
})
