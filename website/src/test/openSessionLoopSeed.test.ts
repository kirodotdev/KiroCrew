/**
 * #30331 — the sidebar "In progress" filter must count a session whose idle goal
 * loop is armed WHETHER OR NOT that session is the open one.
 *
 * The sidebar (and the board's state lanes) derive goal-loop membership from the
 * Redux automation collection via `selectSidebarAutomationRunningKeys`. The open
 * session is the one case that diverged: its composer re-reads the per-slot key
 * (`['session-automation', slot]`) on every connect, so when the cold seed ran
 * while that read was still IN FLIGHT, the slot got "protected" out of the
 * seed's records and the carry-across had no resolved record to carry — the
 * armed loop never reached Redux, and the chip the composer still showed fell
 * out of the In-progress count/list.
 *
 * The seed is the one place that feeds every Redux reader, so the fix lives
 * there: a per-slot read that has not resolved no longer protects its slot, so
 * the authoritative list record lands. This test drives the real seed through
 * `useWebSocket` and asserts the armed loop is present in the exact key set the
 * In-progress filter reads — red without the seed fix.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { normalizeAutomationRecord } from '../monitoring/automation'
import { selectSidebarAutomationRunningKeys } from '../store/chatSlice'

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
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
}

// The loop from the issue's repro: idle 10 min, cap 5, armed before connect.
const OPEN_LOOP = { id: 'lp-open', slot_key: 'chat-open-1', message: 'keep going', idle_secs: 600, max_cycles: 5, cycle_count: 2, active: true, last_fire_ts: 0 }
const OTHER_LOOP = { ...OPEN_LOOP, id: 'lp-other', slot_key: 'chat-other-2' }

describe('#30331 open session goal loop reaches the sidebar In-progress set', () => {
  let testStore: ReturnType<typeof createTestStore>
  let queryClient: QueryClient
  beforeEach(() => {
    vi.clearAllMocks(); WS_INSTANCES.length = 0
    seedDeferred = newDeferred(); monitorSeedDeferred = newDeferred()
    testStore = createTestStore({})
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })
  afterEach(() => { vi.unstubAllGlobals() })
  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: queryClient }, children))
  }
  const runningKeys = () => selectSidebarAutomationRunningKeys(testStore.getState())
  async function resolveEmptyMonitors() { monitorSeedDeferred.resolve({ enabled: true, monitors: [] }); await monitorSeedDeferred.promise }

  it('counts the OPEN session whose per-slot read is still in flight when the seed lands', async () => {
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    // The open session's composer cold read (`useSessionAutomation`) starts
    // during the seed window and has NOT resolved when the snapshot lands —
    // exactly what happens when you open the loop's own session.
    const openSlotRead = newDeferred()
    void queryClient.fetchQuery({
      queryKey: ['session-automation', OPEN_LOOP.slot_key],
      queryFn: () => openSlotRead.promise,
    }).catch(() => {})

    await act(async () => {
      // The list is authoritative and includes BOTH loops.
      seedDeferred.resolve({ enabled: true, loops: [OPEN_LOOP, OTHER_LOOP] })
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })

    // Both the open session and the non-open session are counted, from one set.
    expect(runningKeys()).toEqual(expect.arrayContaining([OPEN_LOOP.slot_key, OTHER_LOOP.slot_key]))

    // The composer's read resolving later keeps it counted; nothing regresses.
    await act(async () => {
      openSlotRead.resolve(normalizeAutomationRecord(OPEN_LOOP))
      await openSlotRead.promise
      await Promise.resolve()
    })
    expect(runningKeys()).toEqual(expect.arrayContaining([OPEN_LOOP.slot_key, OTHER_LOOP.slot_key]))
  })

  it('still ignores an unresolved per-slot read for a loop the list does NOT arm', async () => {
    // Guard: dropping protection for a pending read must not invent an entry.
    renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })
    const openSlotRead = newDeferred()
    void queryClient.fetchQuery({
      queryKey: ['session-automation', OPEN_LOOP.slot_key],
      queryFn: () => openSlotRead.promise,
    }).catch(() => {})
    await act(async () => {
      seedDeferred.resolve({ enabled: true, loops: [] })  // nothing armed
      await Promise.all([seedDeferred.promise, resolveEmptyMonitors()])
    })
    expect(runningKeys()).not.toContain(OPEN_LOOP.slot_key)
  })
})
