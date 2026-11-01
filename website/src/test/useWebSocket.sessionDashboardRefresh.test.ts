/**
 * A root session's open Overview re-reads its page when the session's crew log
 * grows, through the real hook and a socket.
 *
 * The Overview is `[SESSION_DASHBOARD_QUERY_PREFIX, slot, ...]`. A work-ledger write
 * reaches the tab as a `slot_projection` frame (or, for a tab that applies deltas, a
 * `slot_fold` frame) naming the slot that wrote; the page of that slot and of every
 * slot it was created under must be read again. The watch set that routes those
 * frames to the tab is in `hooks/websocket/foldWatch.test.ts`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import { store } from '../store'
import { useWebSocket, SESSION_DASHBOARD_QUERY_PREFIX } from '../hooks/useWebSocket'
import { WS_INSTANCES, MockWebSocket } from './UseWebSocketCoverageTestUtils'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error { status = 500 },
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
  },
}))

const TEAM = [
  { key: 'root', messages: 1, running: false },
  { key: 'worker', messages: 1, running: false, created_by: 'root' },
  { key: 'other', messages: 1, running: false },
]

describe('useWebSocket session Overview refresh', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient
  const stops: Array<() => void> = []

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    for (const stop of stops.splice(0)) stop()
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  /** Open the hook, seed the team, and mount *slot*'s Overview; resolves once its first read settled. */
  async function openOverview(slot: string) {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    await act(async () => { ws.simulateOpen(); await new Promise(resolve => setTimeout(resolve, 0)) })
    act(() => { store.dispatch(sseSlots(TEAM)) })
    const read = vi.fn().mockResolvedValue({ html: '<p>board</p>' })
    const observer = new QueryObserver(qc, { queryKey: [SESSION_DASHBOARD_QUERY_PREFIX, slot, 'en', 'live'], queryFn: read })
    stops.push(observer.subscribe(() => {}))
    await vi.waitFor(() => { expect(observer.getCurrentResult().fetchStatus).toBe('idle') })
    expect(read).toHaveBeenCalledTimes(1)
    return { ws, read, observer }
  }

  it('re-reads the Overview when its own slot writes the work ledger', async () => {
    const { ws, read } = await openOverview('root')
    act(() => { ws.simulateMessage({ type: 'slot_projection', data: { slot: 'dashboard:root', fold: 'work', revision: 3 } }) })
    await vi.waitFor(() => { expect(read).toHaveBeenCalledTimes(2) })
  })

  it('re-reads it for a worker created under it, and not for an unrelated slot', async () => {
    const { ws, read, observer } = await openOverview('root')
    act(() => { ws.simulateMessage({ type: 'slot_projection', data: { slot: 'dashboard:other', fold: 'work', revision: 1 } }) })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(read).toHaveBeenCalledTimes(1)
    act(() => { ws.simulateMessage({ type: 'slot_projection', data: { slot: 'dashboard:worker', fold: 'work', revision: 2 } }) })
    await vi.waitFor(() => { expect(read).toHaveBeenCalledTimes(2) })
    await vi.waitFor(() => { expect(observer.getCurrentResult().fetchStatus).toBe('idle') })
  })

  it('a slot_fold delta re-reads it, even when the work board already polled that revision', async () => {
    const { ws, read, observer } = await openOverview('root')
    act(() => { ws.simulateMessage({ type: 'fold_hello', data: { v: 1, folds: ['work'], delta: ['work'] } }) })
    act(() => { ws.simulateMessage({ type: 'slot_fold', data: { v: 1, slot: 'dashboard:root', fold: 'work', kind: 'snapshot_needed', revision: 4, base_revision: 0 } }) })
    await vi.waitFor(() => { expect(read).toHaveBeenCalledTimes(2) })
    await vi.waitFor(() => { expect(observer.getCurrentResult().fetchStatus).toBe('idle') })
    // The board's own poll landed revision 5 before its frame: `stale` for the board.
    qc.setQueryData(['command-center', 'root', 'work'], { value: { items: [] }, published_revision: 5 })
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(read).toHaveBeenCalledTimes(2)
    act(() => { ws.simulateMessage({ type: 'slot_fold', data: { v: 1, slot: 'dashboard:root', fold: 'work', kind: 'delta', revision: 5, base_revision: 4, ops: [] } }) })
    await vi.waitFor(() => { expect(read).toHaveBeenCalledTimes(3) })
  })

})
