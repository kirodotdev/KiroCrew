/**
 * The gateway re-broadcasts the whole slot list on any change, so a client
 * receives `slots` frames byte-identical to the previous one. The hook skips an
 * identical repeat, but only while no other writer has replaced the list since.
 *
 * Four controls. The middle two exist because a dedupe that never passes
 * anything through is indistinguishable from a working one if only the dedup
 * case is tested; the last pins the store-identity half of the guard, without
 * which a late `fetchSlots` snapshot wins permanently.
 *
 * Deliberately the SINGLETON store: useWebSocket dispatches via useAppDispatch()
 * but reads slots off the imported singleton, so a separate Provider store would
 * let reads and writes diverge and every control below would pass vacuously.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store as globalStore } from '../store'
import { sseSlots, fetchSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
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

describe('useWebSocket slots frame dedupe', () => {
  let dispatchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    globalStore.dispatch(sseSlots([]))
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    dispatchSpy?.mockRestore()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: globalStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  /** Mount, open the socket, and start counting sseSlots dispatches from zero.
   *  The spy must precede mount: useAppDispatch() captures store.dispatch once. */
  function mountOpened() {
    dispatchSpy = vi.spyOn(globalStore, 'dispatch')
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    dispatchSpy.mockClear()
    return ws
  }

  const slotsDispatches = () =>
    dispatchSpy.mock.calls.filter(([a]) => (a as { type?: string })?.type === sseSlots.type).length

  const frame = (title: string) => ({
    type: 'slots',
    data: [{ key: 'slot-a', title, agent: 'kirocrew' }],
  })

  const title = () => globalStore.getState().dashboard.slots[0]?.title

  it('dispatches once for two byte-identical consecutive frames', () => {
    const ws = mountOpened()

    act(() => { ws.simulateMessage(frame('session')) })
    expect(slotsDispatches()).toBe(1)

    act(() => { ws.simulateMessage(frame('session')) })
    expect(slotsDispatches()).toBe(1)
  })

  it('dispatches twice when the second frame differs', () => {
    const ws = mountOpened()

    act(() => { ws.simulateMessage(frame('session')) })
    act(() => { ws.simulateMessage(frame('renamed')) })

    expect(slotsDispatches()).toBe(2)
    expect(title()).toBe('renamed')
  })

  it('dispatches twice when a reconnect separates two identical frames', () => {
    vi.useFakeTimers()
    const ws1 = mountOpened()
    act(() => { ws1.simulateMessage(frame('session')) })
    expect(slotsDispatches()).toBe(1)

    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) })
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })
    act(() => { ws2.simulateMessage(frame('session')) })

    expect(slotsDispatches()).toBe(2)
  })

  it('re-applies an identical frame after a late fetchSlots snapshot overwrote it', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage(frame('session')) })
    expect(title()).toBe('session')

    // An in-flight fetchSlots resolves after the frame and replaces the list —
    // the ordering the reconnect path already documents as expected.
    const stale = [{ key: 'slot-a', title: 'from-http', agent: 'kirocrew' }] as ChatSlot[]
    act(() => { globalStore.dispatch(fetchSlots.fulfilled(stale, 'req-1', undefined as never)) })
    expect(title()).toBe('from-http')

    act(() => { ws.simulateMessage(frame('session')) })
    expect(slotsDispatches()).toBe(2)
    expect(title()).toBe('session')
  })

  it('dispatches the first frame of a reconnect even while the refetch is still pending', async () => {
    // Isolates the onopen reset. With the refetch unresolved the slot list is
    // untouched, so the store-identity half of the guard cannot carry this case.
    const { api } = await import('../api/client')
    vi.mocked(api.chatSlots).mockReturnValue(new Promise(() => {}) as never)

    vi.useFakeTimers()
    const ws1 = mountOpened()
    act(() => { ws1.simulateMessage(frame('session')) })
    expect(slotsDispatches()).toBe(1)

    act(() => { ws1.onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) })
    const ws2 = WS_INSTANCES[1]
    act(() => { ws2.simulateOpen() })
    act(() => { ws2.simulateMessage(frame('session')) })

    expect(slotsDispatches()).toBe(2)
  })
})
