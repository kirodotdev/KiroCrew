/**
 * Ghost-queue reconcile over the socket: a `slots` push carries the queued
 * depth the gateway last published for each row (`subagents_queued`), and the
 * hook applies it to `chat.subagentQueued`. A client that kept "1 queued" after
 * missing the 0 frame is corrected by the next push, for a session nothing on
 * screen is showing.
 *
 * Deliberately the SINGLETON store, for the reason useWebSocket.slotsFrameDedupe
 * gives: the hook dispatches through useAppDispatch() but reads slots off the
 * imported singleton.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store as globalStore } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { sseSubagentQueued } from '../store/chatSlice'
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

const BG = 'chat-background'

const slotsFrame = (rows: Record<string, unknown>[]) => ({
  type: 'slots',
  data: rows.map(r => ({ title: 'S', agent: 'kirocrew', ...r })),
})

const queuedOf = (slot: string) => globalStore.getState().chat.subagentQueued?.[slot]

describe('useWebSocket queued-depth reconcile from slots pushes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    globalStore.dispatch(sseSlots([]))
    globalStore.dispatch(sseSubagentQueued({ slot: BG, queued: 0 }))
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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

  it('clears a stale count on a background session when the push says 0', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'subagent_queued', data: { slot: BG, queued: 1 } }) })
    expect(queuedOf(BG)).toBe(1)
    expect(globalStore.getState().chat.activeSlot).not.toBe(BG)

    act(() => { ws.simulateMessage(slotsFrame([{ key: BG, subagents_queued: 0 }])) })

    expect(queuedOf(BG)).toBeUndefined()
  })

  it('a push identical to the previous one still clears a count moved in between', () => {
    // 1 -> 0 inside one debounce window: the client missed the 0 frame, and the
    // one push that follows serializes the same rows as the push before it.
    const ws = mountOpened()
    const frame = slotsFrame([{ key: BG, subagents_queued: 0 }])
    act(() => { ws.simulateMessage(frame) })
    act(() => { ws.simulateMessage({ type: 'subagent_queued', data: { slot: BG, queued: 1 } }) })
    expect(queuedOf(BG)).toBe(1)

    act(() => { ws.simulateMessage(frame) })

    expect(queuedOf(BG)).toBeUndefined()
  })

  it('leaves the count alone for a row that carries no depth', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'subagent_queued', data: { slot: BG, queued: 2 } }) })
    act(() => { ws.simulateMessage(slotsFrame([{ key: BG }])) })
    expect(queuedOf(BG)).toBe(2)
  })

  it('a newer frame after the push wins: the push never reorders the stream', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage(slotsFrame([{ key: BG, subagents_queued: 0 }])) })
    act(() => { ws.simulateMessage({ type: 'subagent_queued', data: { slot: BG, queued: 3 } }) })
    expect(queuedOf(BG)).toBe(3)
  })
})
