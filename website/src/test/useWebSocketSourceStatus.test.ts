import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { PullRequestStatusBatch } from '../types'

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

  constructor() { WS_INSTANCES.push(this) }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const URL_A = 'https://github.com/acme/repo/pull/7'
const STATUS_KEY = ['pull-request-statuses', [URL_A]] as const

/**
 * The sidebar chips and the Changes-strip detail panel read two independent
 * caches that a plain agent turn does not invalidate, so without a push they can
 * render different lifecycles for the same pull request until a manual Refresh.
 * The gateway pushes a `source_status` delta when a status changes and forces a
 * re-read at turn boundaries; these tests pin the client half.
 */
describe('useWebSocket pull-request status sync', () => {
  let qc: QueryClient
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData<PullRequestStatusBatch>(STATUS_KEY, {
      statuses: { [URL_A]: { state: 'open', ci: 'running' } },
      refreshing: [URL_A],
      ttlSecs: 60,
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    store.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function connect() {
    const rendered = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { ws, unmount: rendered.unmount }
  }

  it('patches the cached strip status from a delta without waiting for a poll', () => {
    const { ws, unmount } = connect()

    act(() => {
      ws.simulateMessage({ type: 'source_status', data: { url: URL_A, state: 'merged', origin: 'chip' } })
    })

    const batch = qc.getQueryData<PullRequestStatusBatch>(STATUS_KEY)
    expect(batch?.statuses[URL_A]).toEqual({ state: 'merged' })
    // No longer awaiting a refresh, so the panel returns to TTL pacing.
    expect(batch?.refreshing).toEqual([])
    unmount()
  })

  it('refetches the detail payload when the change came from the chip path', () => {
    const { ws, unmount } = connect()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    act(() => {
      ws.simulateMessage({ type: 'source_status', data: { url: URL_A, ci: 'failed', origin: 'chip' } })
    })

    // The gateway dropped its full payload for this URL, so the panel must not
    // keep rendering the copy it fetched on mount (staleTime: Infinity).
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-source', URL_A] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-checks', URL_A] })
    unmount()
  })

  it('refetches on a detail-origin delta so other owner windows converge', () => {
    const { ws, unmount } = connect()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    act(() => {
      ws.simulateMessage({ type: 'source_status', data: { url: URL_A, state: 'merged', origin: 'detail' } })
    })

    // A detail-origin delta is produced by ONE window's full fetch; only that
    // window received the fresh payload. Every other owner window still holds a
    // staleTime:Infinity detail query, so it must refetch too — otherwise it
    // keeps rendering the pre-change lifecycle (the cross-window echo of the
    // very divergence this feature fixes). The initiator's refetch just hits the
    // gateway's warm cache, so it costs nothing and cannot loop.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-source', URL_A] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-checks', URL_A] })
    expect(qc.getQueryData<PullRequestStatusBatch>(STATUS_KEY)?.statuses[URL_A]).toEqual({ state: 'merged' })
    unmount()
  })

  it('ignores a delta with no usable url', () => {
    const { ws, unmount } = connect()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    act(() => { ws.simulateMessage({ type: 'source_status', data: { state: 'merged' } }) })

    expect(invalidate).not.toHaveBeenCalled()
    expect(qc.getQueryData<PullRequestStatusBatch>(STATUS_KEY)?.statuses[URL_A]).toEqual({
      state: 'open', ci: 'running',
    })
    unmount()
  })

  it('refetches the open pull request when the active slot finishes a turn', () => {
    store.dispatch(setActiveSlot('chat-active'))
    const { ws, unmount } = connect()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-active' } }) })

    // Lifecycle/CI deltas don't cover review comments or mergeability, so the
    // turn boundary itself re-reads the payload the panel is showing.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-source'], refetchType: 'active' })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-statuses'], refetchType: 'active' })
    unmount()
  })

  it('marks a background slot\'s PR queries stale without refetching them', () => {
    store.dispatch(setActiveSlot('chat-active'))
    const { ws, unmount } = connect()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: 'chat-other' } }) })

    // A background slot's detail query is staleTime:Infinity, so if its turn is
    // never marked stale it renders pre-turn data when the user later switches
    // to it. Mark it stale (refetchType: 'none') so it refetches on next mount —
    // but do NOT refetch an off-screen PR now (pure provider load).
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-source'], refetchType: 'none' })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pull-request-statuses'], refetchType: 'none' })
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['pull-request-source'], refetchType: 'active' })
    unmount()
  })

  it('patches the sidebar chip (Redux slots) from a delta, not just react-query', () => {
    // Seed a slot that links URL_A — the sidebar renders its chip from this.
    testStore.dispatch(sseSlots([
      {
        key: 'chat-a', messages: 1, running: false,
        source_links: [{ provider: 'github', number: 7, url: URL_A, state: 'open', ci: 'running' }],
      },
    ] as never))
    const { ws, unmount } = connect()

    act(() => {
      ws.simulateMessage({ type: 'source_status', data: { url: URL_A, state: 'merged', ci: 'passed', origin: 'chip' } })
    })

    // BOTH caches updated: the react-query strip batch...
    expect(qc.getQueryData<PullRequestStatusBatch>(STATUS_KEY)?.statuses[URL_A]).toEqual({ state: 'merged', ci: 'passed' })
    // ...and the Redux slots the sidebar chip reads.
    const link = testStore.getState().dashboard.slots[0].source_links?.[0]
    expect(link).toMatchObject({ url: URL_A, state: 'merged', ci: 'passed' })
    unmount()
  })
})
