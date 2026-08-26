/**
/**
 * The sidebar groups sessions by folder, but sessions arrive on the `slots` WS
 * frame (instant on connect) while the folder tree otherwise comes only from a
 * separate `GET /api/chat/folders`. Until that GET resolved, every session fell
 * into the Unfiled bucket and then visibly re-shuffled into its folder once the
 * folders arrived. The fix piggybacks the folder tree onto the `slots` frame and
 * seeds the ['chat-folders'] query cache from it, so grouping is correct on the
 * first paint.
 *
 * These tests pin: (1) a frame carrying `folders` seeds the query cache when it
 * is empty (first paint), (2) a frame does NOT overwrite an already-populated
 * cache — so a live frame arriving mid-optimistic-mutation, or after the HTTP
 * GET has loaded counts, leaves the cache alone — and (3) the first-paint seed
 * invalidates the query so the real GET backfills the omitted `history_count`.
 *
 * Uses the SINGLETON store for the same reason as slotsFrameDedupe: the hook
 * reads slots off the imported singleton.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store as globalStore } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatFolder } from '../types'
import { useWebSocket } from '../hooks/useWebSocket'

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

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

describe('useWebSocket seeds chat-folders from the slots frame', () => {
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    globalStore.dispatch(sseSlots([]))
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store: globalStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  function mountOpened() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  const folders = (): ChatFolder[] | undefined =>
    qc.getQueryData<ChatFolder[]>(['chat-folders'])

  it('seeds an empty cache with the folder tree carried on the frame', () => {
    const ws = mountOpened()
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [{ key: 'slot-a', title: 's', agent: 'kirocrew', folder_id: 'f1' }],
        folders: [{ id: 'f1', name: 'Work', order: 0 }],
      })
    })
    expect(folders()).toEqual([{ id: 'f1', name: 'Work', order: 0 }])
  })

  it('invalidates the chat-folders query after a first-paint seed so counts backfill', () => {
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const ws = mountOpened()
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [],
        folders: [{ id: 'f1', name: 'Work', order: 0 }],
      })
    })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['chat-folders'] })
  })

  it('does NOT overwrite an already-populated cache (optimistic edits / loaded counts survive)', () => {
    // The HTTP GET (or an in-flight folder mutation's optimistic update) has
    // already put data in the cache; a later live `slots` frame must leave it be.
    qc.setQueryData<ChatFolder[]>(['chat-folders'], [
      { id: 'f1', name: 'Work (optimistic rename)', order: 0, history_count: 7, collapsed: true },
    ])
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const ws = mountOpened()
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [],
        folders: [{ id: 'f1', name: 'Work', order: 0 }],
      })
    })
    // Untouched: the optimistic name, count, and collapsed state all survive,
    // and no invalidate fired to clobber the in-flight mutation.
    expect(folders()).toEqual([
      { id: 'f1', name: 'Work (optimistic rename)', order: 0, history_count: 7, collapsed: true },
    ])
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['chat-folders'] })
  })

  it('does NOT re-seed a populated-but-EMPTY cache (no refetch storm for zero-folder users)', () => {
    // A user with genuinely zero folders has the GET cache `[]`. Guarding on
    // `existing.length === 0` would re-match every slots frame and loop
    // invalidate→refetch against the session-scanning endpoint. The guard is
    // `existing === undefined`, so an empty (but present) cache is left alone.
    qc.setQueryData<ChatFolder[]>(['chat-folders'], [])
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    const ws = mountOpened()
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [],
        folders: [{ id: 'f1', name: 'Work', order: 0 }],
      })
    })
    expect(folders()).toEqual([])
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['chat-folders'] })
  })

  it('does not touch the cache when the frame carries no folders field', () => {
    qc.setQueryData<ChatFolder[]>(['chat-folders'], [
      { id: 'f1', name: 'Work', order: 0, history_count: 3 },
    ])
    const ws = mountOpened()
    act(() => {
      ws.simulateMessage({ type: 'slots', data: [{ key: 'slot-a', agent: 'kirocrew' }] })
    })
    expect(folders()).toEqual([{ id: 'f1', name: 'Work', order: 0, history_count: 3 }])
  })
})
