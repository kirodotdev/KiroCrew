/**
 * Regression: the automation cold seed fetches legacy loops and structured
 * monitors together, then replaces the one authoritative slot collection.
 *
 * The trap this pins: snapshot replacement races the live `autonudge_state`
 * stream. A newer frame must protect its own slot from stale seed data, including
 * a removal tombstone, without discarding unaffected rows from either REST feed.
 * Frames fire only on CHANGE, so resurrecting a removed loop or dropping an
 * unrelated record may otherwise persist until reconnect.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { normalizeAutomationRecord } from '../monitoring/automation'
import { removeAutomation, sseAutomation } from '../store/chatSlice'

/** Resolved by hand inside the test so the in-flight window is controllable. */
let seedDeferred: { resolve: (v: unknown) => void; promise: Promise<unknown> }
let monitorSeedDeferred: { resolve: (v: unknown) => void; promise: Promise<unknown> }
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
    monitorsList: vi.fn(() => monitorSeedDeferred.promise),
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
const OTHER_LOOP = { ...LOOP, id: 'lp2', slot_key: 'chat-2-1721', cycle_count: 4 }
const CHANNEL_LOOP = { ...LOOP, id: 'lp-channel', slot_key: 'slack:1785370133.085469' }
const CHANNEL_SLOT = 'slack_1785370133.085469'
const TERMINAL_MONITOR = {
  id: 'monitor-1', slot_key: 'chat-3-1721', message: '', idle_secs: 300,
  max_cycles: 0, cycle_count: 0, active: false, last_fire_ts: 0, next_due_ts: 0,
  stopped_reason: 'token_budget',
  monitor: {
    version: 1, config_generation: 1, kind: 'github_pull_request',
    target: 'https://github.com/kirodotdev/KiroCrew/pull/42', objective: 'review_ready',
    budgets: {
      max_runtime_secs: 14_400, max_agent_turns: 8, max_tokens: 250_000,
      max_provider_errors: 3,
    },
    cadence_secs: 300, wake_instructions: '', last_observation: {},
    last_observation_status: null, last_observation_reason_code: '',
    last_fingerprint: '', last_observed_at: 0, last_wake_fingerprint: '',
    wake_in_flight: false, wake_delivery: null, wake_count: 1,
    completion_evidence_deadline: 0, last_completion_fingerprint: '',
    last_completion_disposition: null, last_completed_at: 0, token_usage_known: true,
    agent_turns: 1, input_tokens: 20, output_tokens: 10, probe_count: 2,
    provider_error_count: 0, consecutive_provider_errors: 0, last_probe_at: 10,
    last_decision: 'stop_budget', last_provider_error: null, next_probe_at: 0,
    outcome: 'budget', stopped_reason: 'token_budget', stopped_at: 20,
  },
}
const ACTIVE_MONITOR = {
  ...TERMINAL_MONITOR, active: true, stopped_reason: '',
  monitor: {
    ...TERMINAL_MONITOR.monitor, outcome: null, stopped_reason: '', stopped_at: 0,
    last_decision: null,
  },
}

describe('useWebSocket automation seed vs live frames', () => {
  let testStore: ReturnType<typeof createTestStore>
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    seedDeferred = newDeferred()
    monitorSeedDeferred = newDeferred()
    testStore = createTestStore({})
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: queryClient }, children),
    )
  }

  const automations = () => testStore.getState().chat.automations

  async function resolveEmptyMonitors() {
    monitorSeedDeferred.resolve({ enabled: true, monitors: [] })
    await monitorSeedDeferred.promise
  }

  it('seeds the map when no frame arrives while the request is in flight', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    // Both independent snapshots start before either can resolve.
    expect(api.autonudgeList).toHaveBeenCalledTimes(1)
    expect(api.monitorsList).toHaveBeenCalledTimes(1)

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })
    expect(automations()['chat-1-1721']).toMatchObject({
      kind: 'legacy_goal_loop', cycleCount: 7, maxCycles: 24,
    })
  })

  it('does not tombstone a per-slot snapshot written after the seed started', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    const newer = normalizeAutomationRecord({ ...LOOP, cycle_count: 9 })
    expect(newer).not.toBeNull()
    queryClient.setQueryData(['session-automation', LOOP.slot_key], newer)

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })

    expect(queryClient.getQueryData(['session-automation', LOOP.slot_key])).toEqual(newer)
  })

  it('does not overwrite a per-slot snapshot written after the seed started', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    const newer = normalizeAutomationRecord({ ...LOOP, cycle_count: 9 })
    expect(newer).not.toBeNull()
    queryClient.setQueryData(['session-automation', LOOP.slot_key], newer)
    act(() => { testStore.dispatch(sseAutomation(newer!)) })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })

    expect(automations()[LOOP.slot_key]).toMatchObject({ cycleCount: 9 })
  })

  it('does not resurrect a record tombstoned after the seed started', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    const stale = normalizeAutomationRecord(LOOP)!
    queryClient.setQueryData(['session-automation', LOOP.slot_key], stale)
    testStore.dispatch(sseAutomation(stale))
    act(() => { WS_INSTANCES[0].simulateOpen() })

    queryClient.setQueryData(['session-automation', LOOP.slot_key], null)
    act(() => { testStore.dispatch(removeAutomation(LOOP.slot_key)) })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })

    expect(automations()[LOOP.slot_key]).toBeUndefined()
  })

  it('coalesces overlapping reconnect seeds onto one request per feed', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => {
      WS_INSTANCES[0].simulateOpen()
      WS_INSTANCES[0].simulateOpen()
    })

    expect(api.autonudgeList).toHaveBeenCalledTimes(1)
    expect(api.monitorsList).toHaveBeenCalledTimes(1)

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      monitorSeedDeferred.resolve({ enabled: true, monitors: [] })
      await Promise.all([seedDeferred.promise, monitorSeedDeferred.promise])
    })
    expect(automations()['chat-1-1721']).toMatchObject({ cycleCount: 7 })
  })

  it('queues a fresh snapshot when reconnect overlaps an older seed', async () => {
    const freshLegacy = newDeferred()
    const freshMonitors = newDeferred()
    vi.mocked(api.autonudgeList)
      .mockImplementationOnce(() => seedDeferred.promise as ReturnType<typeof api.autonudgeList>)
      .mockImplementationOnce(() => freshLegacy.promise as ReturnType<typeof api.autonudgeList>)
    vi.mocked(api.monitorsList)
      .mockImplementationOnce(() => monitorSeedDeferred.promise as ReturnType<typeof api.monitorsList>)
      .mockImplementationOnce(() => freshMonitors.promise as ReturnType<typeof api.monitorsList>)

    renderHook(() => useWebSocket(), { wrapper })
    act(() => {
      WS_INSTANCES[0].simulateOpen()
      WS_INSTANCES[0].simulateOpen()
    })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      monitorSeedDeferred.resolve({ enabled: true, monitors: [] })
      await Promise.all([seedDeferred.promise, monitorSeedDeferred.promise])
    })

    expect(api.autonudgeList).toHaveBeenCalledTimes(2)
    expect(api.monitorsList).toHaveBeenCalledTimes(2)
    await act(async () => {
      freshLegacy.resolve({ enabled: true, loops: [{ ...LOOP, cycle_count: 9 }] })
      freshMonitors.resolve({ enabled: true, monitors: [] })
      await Promise.all([freshLegacy.promise, freshMonitors.promise])
    })
    expect(automations()[LOOP.slot_key]).toMatchObject({ cycleCount: 9 })
  })

  it('keeps a removal tombstone until the queued reconnect snapshot refreshes it', async () => {
    const freshLegacy = newDeferred()
    const freshMonitors = newDeferred()
    vi.mocked(api.autonudgeList)
      .mockImplementationOnce(() => seedDeferred.promise as ReturnType<typeof api.autonudgeList>)
      .mockImplementationOnce(() => freshLegacy.promise as ReturnType<typeof api.autonudgeList>)
    vi.mocked(api.monitorsList)
      .mockImplementationOnce(() => monitorSeedDeferred.promise as ReturnType<typeof api.monitorsList>)
      .mockImplementationOnce(() => freshMonitors.promise as ReturnType<typeof api.monitorsList>)

    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    act(() => {
      WS_INSTANCES[0].simulateMessage({
        type: 'autonudge_state', data: { event: 'removed', slot: LOOP.slot_key, loop: LOOP },
      })
      WS_INSTANCES[0].simulateOpen()
    })

    expect(api.autonudgeList).toHaveBeenCalledTimes(1)
    expect(api.monitorsList).toHaveBeenCalledTimes(1)
    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })
    expect(api.autonudgeList).toHaveBeenCalledTimes(2)
    expect(automations()[LOOP.slot_key]).toBeUndefined()
    await act(async () => {
      freshLegacy.resolve({ enabled: true, loops: [] })
      freshMonitors.resolve({ enabled: true, monitors: [] })
      await Promise.all([freshLegacy.promise, freshMonitors.promise])
    })
    expect(automations()[LOOP.slot_key]).toBeUndefined()
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
    expect(automations()['chat-1-1721']).toBeUndefined()

    // The stale snapshot resolves afterwards and must NOT resurrect it.
    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [LOOP] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })
    expect(automations()['chat-1-1721']).toBeUndefined()
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
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })
    expect(automations()['chat-1-1721']).toMatchObject({ cycleCount: 9, maxCycles: 24 })
  })

  it('reconciles channel frames and snapshots under the dashboard slot key', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    act(() => {
      WS_INSTANCES[0].simulateMessage({
        type: 'autonudge_state',
        data: {
          event: 'fired', slot: CHANNEL_LOOP.slot_key,
          loop: { ...CHANNEL_LOOP, cycle_count: 9 },
        },
      })
    })
    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [CHANNEL_LOOP] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })

    expect(automations()[CHANNEL_SLOT]).toMatchObject({ cycleCount: 9 })
    expect(automations()[CHANNEL_LOOP.slot_key]).toBeUndefined()
  })

  it('removes a channel automation by its dashboard slot key', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    act(() => {
      WS_INSTANCES[0].simulateMessage({
        type: 'autonudge_state',
        data: { event: 'fired', slot: CHANNEL_LOOP.slot_key, loop: CHANNEL_LOOP },
      })
    })
    expect(automations()[CHANNEL_SLOT]).toBeDefined()

    act(() => {
      WS_INSTANCES[0].simulateMessage({
        type: 'autonudge_state',
        data: { event: 'removed', slot: CHANNEL_LOOP.slot_key, loop: CHANNEL_LOOP },
      })
    })
    expect(automations()[CHANNEL_SLOT]).toBeUndefined()
  })

  it('reconciles active slots without globally retaining terminal records', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    act(() => {
      WS_INSTANCES[0].simulateMessage({
        type: 'autonudge_state',
        data: {
          event: 'fired', slot: LOOP.slot_key,
          loop: { ...LOOP, cycle_count: 9 },
        },
      })
    })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [OTHER_LOOP] })
      monitorSeedDeferred.resolve({ enabled: true, monitors: [TERMINAL_MONITOR] })
      await Promise.all([seedDeferred.promise, monitorSeedDeferred.promise])
    })

    expect(automations()[LOOP.slot_key]).toMatchObject({ cycleCount: 9 })
    expect(automations()[OTHER_LOOP.slot_key]).toMatchObject({ cycleCount: 4 })
    expect(automations()[TERMINAL_MONITOR.slot_key]).toBeUndefined()
    expect(queryClient.getQueryData([
      'session-automation', TERMINAL_MONITOR.slot_key,
    ])).toBeUndefined()
  })

  it.each([
    ['terminal', TERMINAL_MONITOR],
    ['active', ACTIVE_MONITOR],
    ['empty', null],
    ['predecessor monitor', { ...ACTIVE_MONITOR, id: 'previous-monitor' }],
    ['predecessor legacy loop', { ...LOOP, slot_key: TERMINAL_MONITOR.slot_key }],
  ])('reconciles terminal evidence into an existing %s cache', async (_label, initial) => {
    const terminal = normalizeAutomationRecord(TERMINAL_MONITOR)
    expect(terminal).not.toBeNull()
    const cached = normalizeAutomationRecord(initial)
    queryClient.setQueryData(['session-automation', TERMINAL_MONITOR.slot_key], cached)

    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [] })
      monitorSeedDeferred.resolve({ enabled: true, monitors: [TERMINAL_MONITOR] })
      await Promise.all([seedDeferred.promise, monitorSeedDeferred.promise])
    })

    expect(queryClient.getQueryData([
      'session-automation', TERMINAL_MONITOR.slot_key,
    ])).toEqual(terminal)
    expect(automations()[TERMINAL_MONITOR.slot_key]).toBeUndefined()
  })

  it.each(['rest', 'frame', 'removed'])('terminal seed preserves newer %s evidence', async source => {
    const slot = TERMINAL_MONITOR.slot_key
    const queryKey = ['session-automation', slot]
    const active = normalizeAutomationRecord(ACTIVE_MONITOR)!
    queryClient.setQueryData(queryKey, active)
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    if (source === 'frame') {
      act(() => {
        WS_INSTANCES[0].simulateMessage({
          type: 'autonudge_state',
          data: { event: 'fired', slot, loop: ACTIVE_MONITOR },
        })
      })
    } else {
      queryClient.setQueryData(queryKey, source === 'removed' ? null : active)
    }
    const newer = queryClient.getQueryData(queryKey)
    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [] })
      monitorSeedDeferred.resolve({ enabled: true, monitors: [TERMINAL_MONITOR] })
      await Promise.all([seedDeferred.promise, monitorSeedDeferred.promise])
    })
    expect(queryClient.getQueryData(queryKey)).toEqual(newer)
  })

  it('ignores an inactive loop in the seed payload', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [{ ...LOOP, active: false }] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })
    expect(automations()['chat-1-1721']).toBeUndefined()
  })
})
