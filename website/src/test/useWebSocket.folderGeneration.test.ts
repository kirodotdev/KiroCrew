/**
 * A folder created outside this tab — by an agent, a second tab, another device —
 * was invisible here until a reload. The sessions arrived on the `slots` frame,
 * but their `folder_id` named a folder this tab had never heard of, so they
 * rendered as Unfiled and the folder looked like it had never been created.
 *
 * Why nothing recovered on its own: `['chat-folders']` inherits the app-wide
 * `staleTime: Infinity` (queryClient.ts), the sidebar's useQuery does not poll,
 * and the `folders` payload the frame already carries is deliberately seeded ONCE
 * (see useWebSocket.folderSeed.test.ts for why re-seeding is unsafe). So the tree
 * only ever changed through a mutation THIS tab issued.
 *
 * The fix is a generation number on the frame: when it moves, the store really
 * changed, and the client invalidates so the real GET refills the tree — counts
 * included. These tests pin the three properties that make it safe:
 *
 *   1. a changed generation invalidates (the bug being fixed),
 *   2. an UNCHANGED generation does not (the guardrail: a `slots` frame fires on
 *      routine session activity, and invalidating on every one of them would
 *      refetch over an in-flight optimistic folder edit and hammer the
 *      session-scanning GET),
 *   3. the first generation frame of a connection always invalidates, because
 *      the counter is process-local to the gateway and a restart can hand back a
 *      number equal to the one this client last saw over a different tree.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ChatFolder } from '../types'
import { createTestStore } from './helpers'
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

describe('useWebSocket folder-tree generation invalidation', () => {
  let testStore: ReturnType<typeof createTestStore>
  let invalidated: unknown[][]
  let qc: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    invalidated = []
    testStore = createTestStore({})
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // Populated up front in every case: an EMPTY cache takes the separate
    // first-paint seed arm, which invalidates for its own reason (backfilling
    // history_count) and would mask what these tests measure.
    qc.setQueryData<ChatFolder[]>(['chat-folders'], [
      { id: 'f1', name: 'Work', order: 0, history_count: 2 } as ChatFolder,
    ])
    const realInvalidate = qc.invalidateQueries.bind(qc)
    qc.invalidateQueries = ((filters?: { queryKey?: unknown[] }) => {
      if (filters?.queryKey) invalidated.push(filters.queryKey)
      return realInvalidate(filters as never)
    }) as typeof qc.invalidateQueries
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  const folderInvalidations = () =>
    invalidated.filter(key => Array.isArray(key) && key[0] === 'chat-folders').length

  function mountOpened(): MockWebSocket {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('invalidates on the first generation frame of a connection', () => {
    const ws = mountOpened()

    act(() => { ws.simulateMessage({ type: 'slots', data: [], foldersGeneration: 4 }) })

    expect(folderInvalidations()).toBe(1)
  })

  it('invalidates when the generation moves — an agent created a folder', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [], foldersGeneration: 4 }) })
    expect(folderInvalidations()).toBe(1)

    // The agent's create landed: same tab, same connection, new tree.
    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [{ key: 'chat-1', title: 's', agent: 'kirocrew', folder_id: 'f2' }],
        folders: [{ id: 'f1', name: 'Work', order: 0 }, { id: 'f2', name: 'Crew', order: 1 }],
        foldersGeneration: 5,
      })
    })

    expect(folderInvalidations()).toBe(2)
  })

  it('does NOT invalidate when the generation is unchanged (optimistic edits survive)', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [], foldersGeneration: 4 }) })
    expect(folderInvalidations()).toBe(1)

    // Routine session activity: three more frames, same tree. A refetch here
    // would land over an in-flight collapse/rename/reorder and snap it back.
    act(() => { ws.simulateMessage({ type: 'slots', data: [{ key: 'a', title: 'x', agent: 'kirocrew' }], foldersGeneration: 4 }) })
    act(() => { ws.simulateMessage({ type: 'slots', data: [{ key: 'b', title: 'y', agent: 'kirocrew' }], foldersGeneration: 4 }) })
    act(() => { ws.simulateMessage({ type: 'slots', data: [{ key: 'c', title: 'z', agent: 'kirocrew' }], foldersGeneration: 4 }) })

    expect(folderInvalidations()).toBe(1)
  })

  it('invalidates after a reconnect even when the generation is unchanged', () => {
    vi.useFakeTimers()
    const ws1 = mountOpened()
    act(() => { ws1.simulateMessage({ type: 'slots', data: [], foldersGeneration: 4 }) })
    expect(folderInvalidations()).toBe(1)

    // Gateway restarts: the counter is process-local, so generation 4 on the new
    // process is a different tree than generation 4 on the old one.
    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) })
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })
    act(() => { ws2.simulateMessage({ type: 'slots', data: [], foldersGeneration: 4 }) })

    expect(folderInvalidations()).toBe(2)
  })

  it('ignores a frame with no generation field (older gateway)', () => {
    const ws = mountOpened()

    act(() => {
      ws.simulateMessage({
        type: 'slots',
        data: [],
        folders: [{ id: 'f1', name: 'Work', order: 0 }],
      })
    })

    expect(folderInvalidations()).toBe(0)
  })
})
