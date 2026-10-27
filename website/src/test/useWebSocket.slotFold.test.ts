/**
 * The work board's `slot_fold` delta frames through the real hook and a socket.
 *
 * A gateway that knows `fold_delta.2` sends this tab `slot_fold` instead of the
 * board's `slot_projection`. The hook applies a delta on the cached board when its
 * base is the board's `published_revision`, re-reads the board on a gap or a
 * `snapshot_needed`, and forgets every base when a new socket opens. A gateway
 * that predates the frame keeps sending whole `slot_projection` values, which the
 * hook applies exactly as before. The base rule itself is in
 * `hooks/websocket/workDelta.test.ts`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { WS_INSTANCES, MockWebSocket } from './UseWebSocketCoverageTestUtils'
import cases from './fixtures/work-delta-cases.json'

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
  },
}))

type Case = { name: string; before: unknown; ops: unknown; after: unknown }
const reports = (cases.cases as Case[]).find(c => c.name === 'one item reports')!
const KEY = ['command-center', 'board-a', 'work'] as const

function delta(revision: number, base: number) {
  return { type: 'slot_fold', data: { v: 1, slot: 'dashboard:board-a', fold: 'work', kind: 'delta', revision, base_revision: base, ops: reports.ops } }
}

describe('useWebSocket slot_fold', () => {
  let testStore: ReturnType<typeof createTestStore>
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => { vi.unstubAllGlobals() })

  function wrapper({ children }: { children: ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function opened() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'fold_hello', data: { v: 1, folds: ['work'], delta: ['work'] } }) })
    return ws
  }

  it('applies a delta on the board the snapshot labelled, and drops its duplicate', () => {
    const ws = opened()
    qc.setQueryData(KEY, { session_id: 'board-a', value: reports.before, published_revision: 5 })
    act(() => { ws.simulateMessage(delta(6, 5)) })
    expect(qc.getQueryData(KEY)).toEqual({ session_id: 'board-a', value: reports.after, published_revision: 6 })
    act(() => { ws.simulateMessage(delta(6, 5)) })
    expect(qc.getQueryData(KEY)).toEqual({ session_id: 'board-a', value: reports.after, published_revision: 6 })
  })

  it('a gap or a snapshot_needed re-reads the board once', async () => {
    const ws = opened()
    const read = vi.fn().mockResolvedValue({ value: reports.before, published_revision: 5 })
    const observer = new QueryObserver(qc, { queryKey: KEY, queryFn: read })
    const stop = observer.subscribe(() => {})
    await vi.waitFor(() => { expect(qc.getQueryData(KEY)).toEqual({ value: reports.before, published_revision: 5 }) })
    expect(read).toHaveBeenCalledTimes(1)
    act(() => { ws.simulateMessage(delta(9, 8)) })
    await vi.waitFor(() => { expect(read).toHaveBeenCalledTimes(2) })
    await vi.waitFor(() => { expect(observer.getCurrentResult().fetchStatus).toBe('idle') })
    act(() => { ws.simulateMessage({ type: 'slot_fold', data: { v: 1, slot: 'board-a', fold: 'work', kind: 'snapshot_needed', revision: 10, base_revision: 0 } }) })
    await vi.waitFor(() => { expect(read).toHaveBeenCalledTimes(3) })
    // The read answered base 5 again; a delta on it now applies with no read.
    await vi.waitFor(() => { expect(observer.getCurrentResult().fetchStatus).toBe('idle') })
    act(() => { ws.simulateMessage(delta(11, 5)) })
    expect(qc.getQueryData<{ value: unknown }>(KEY)?.value).toEqual(reports.after)
    expect(read).toHaveBeenCalledTimes(3)
    stop()
  })

  it('a new socket forgets the base, so the old process numbers never apply', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    qc.setQueryData(KEY, { value: reports.before, published_revision: 5 })
    act(() => { ws.simulateOpen() })
    expect(qc.getQueryData(KEY)).toEqual({ value: reports.before })
    act(() => { ws.simulateMessage(delta(6, 5)) })
    expect(qc.getQueryData(KEY)).toEqual({ value: reports.before })
  })

  it('an older gateway: a whole slot_projection value seeds the board as before', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'slot_projection', data: { slot: 'board-a', fold: 'work', revision: 4, value: reports.after } }) })
    expect(qc.getQueryData(KEY)).toEqual({ value: reports.after, revision: 4 })
  })
})
