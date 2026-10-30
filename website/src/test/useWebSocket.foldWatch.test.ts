/**
 * Per-tab fold watch through the real hook and a socket: the socket declares
 * `fold_delta.1`, and `fold_watch` goes out only after the gateway's
 * `fold_hello`, carrying the roots of the mounted
 * `['command-center', root, 'work']` queries, and again whenever that set
 * changes. A gateway that sends no hello (one that predates the capability)
 * never receives a `fold_watch`. The controller's own cases are co-located in
 * `hooks/websocket/foldWatch.test.ts`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider, QueryObserver } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
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
  },
}))

/** Mount an observer on `['command-center', root, 'work']`; returns its unmount. */
function mountBoard(qc: QueryClient, root: string): () => void {
  const observer = new QueryObserver(qc, {
    queryKey: ['command-center', root, 'work'],
    queryFn: () => new Promise<never>(() => {}),
  })
  return observer.subscribe(() => {})
}

function watchFrames(ws: MockWebSocket): unknown[] {
  return ws.send.mock.calls
    .map(([raw]) => JSON.parse(String(raw)) as { type?: string; slots?: unknown })
    .filter((frame) => frame.type === 'fold_watch')
    .map((frame) => frame.slots)
}

describe('useWebSocket fold watch', () => {
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

  it('declares the capability on the socket url', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const caps = new URL(WS_INSTANCES[0].url).searchParams.get('caps')
    expect(caps?.split(',')).toEqual(['slot_patch', 'fold_delta.1', 'fold_delta.2'])
  })

  it('an old gateway (no hello) is never sent fold_watch', () => {
    mountBoard(qc, 'board-a')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'slots', data: [] }) })
    mountBoard(qc, 'board-b')
    expect(watchFrames(ws)).toEqual([])
  })

  it('after the hello it sends the shown boards, then each change; a reconnect waits for a new hello', () => {
    mountBoard(qc, 'board-a')
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => { ws.simulateMessage({ type: 'fold_hello', data: { v: 1, folds: ['work'] } }) })
    expect(watchFrames(ws)).toEqual([['board-a']])
    act(() => { mountBoard(qc, 'board-b') })
    expect(watchFrames(ws)).toEqual([['board-a'], ['board-a', 'board-b']])

    vi.useFakeTimers()
    try {
      act(() => { ws.readyState = MockWebSocket.CLOSED; ws.onclose?.(new CloseEvent('close')) })
      act(() => { vi.advanceTimersByTime(1500) })
    } finally {
      vi.useRealTimers()
    }
    const next = WS_INSTANCES[WS_INSTANCES.length - 1]
    expect(next).not.toBe(ws)
    act(() => { next.simulateOpen() })
    expect(watchFrames(next)).toEqual([])
    act(() => { next.simulateMessage({ type: 'fold_hello', data: { v: 1, folds: ['work'] } }) })
    expect(watchFrames(next)).toEqual([['board-a', 'board-b']])
    unmount()
  })
})
