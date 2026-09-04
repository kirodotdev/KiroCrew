/**
 * Regression: the goal-loop cold seed (`GET /api/autonudge`, fired on every WS
 * connect) does a FULL REPLACE of `chat.goalLoops`, because loops that ended
 * while this client was disconnected must disappear.
 *
 * The trap this pins: that full replace races the live `autonudge_state` stream.
 * If a loop's `removed` frame is handled while the seed request is still in
 * flight, the seed's older snapshot resurrects the dead loop — and because
 * frames fire only on CHANGE, nothing ever corrects it. The row keeps a phantom
 * "Loop N/M" subtitle and its unread dot stays suppressed until the next
 * reconnect. So a seed response must be discarded when a frame landed while it
 * was in flight.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'

/** Resolved by hand inside the test so the in-flight window is controllable. */
let seedDeferred: { resolve: (v: unknown) => void; promise: Promise<unknown> }
const newDeferred = () => {
  let resolve!: (v: unknown) => void
  const promise = new Promise(res => { resolve = res })
  return { resolve, promise }
}

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn(() => seedDeferred.promise),
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

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const LOOP = { id: 'lp1', slot_key: 'chat-1-1721', message: 'go', idle_secs: 60, max_cycles: 24, cycle_count: 7, active: true, last_fire_ts: 0 }

describe('useWebSocket goal-loop seed vs live frames', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    seedDeferred = newDeferred()
    testStore = createTestStore({})
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  const loops = () => testStore.getState().chat.goalLoops

  it('seeds the map when no frame arrives while the request is in flight', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      await seedDeferred.promise
    })
    expect(loops()['chat-1-1721']).toEqual({ cycle_count: 7, max_cycles: 24 })
  })

  it('discards a seed response that a `removed` frame superseded mid-flight', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    // The loop ends while the seed request is still open.
    act(() => {
      WS_INSTANCES[0].simulateMessage({
        type: 'autonudge_state', data: { event: 'removed', slot: 'chat-1-1721', loop: LOOP },
      })
    })
    expect(loops()['chat-1-1721']).toBeUndefined()

    // The stale snapshot resolves afterwards and must NOT resurrect it.
    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      await seedDeferred.promise
    })
    expect(loops()['chat-1-1721']).toBeUndefined()
  })

  it('keeps a live `fired` frame rather than reverting it to the seed snapshot', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    act(() => {
      WS_INSTANCES[0].simulateMessage({
        type: 'autonudge_state',
        data: { event: 'fired', slot: 'chat-1-1721', loop: { ...LOOP, cycle_count: 9 } },
      })
    })
    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })  // stale cycle_count 7
      await seedDeferred.promise
    })
    expect(loops()['chat-1-1721']).toEqual({ cycle_count: 9, max_cycles: 24 })
  })

  it('ignores an inactive loop in the seed payload', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [{ ...LOOP, active: false }] })
      await seedDeferred.promise
    })
    expect(loops()['chat-1-1721']).toBeUndefined()
  })
})
