/**
 * A dashboard window left open across a gateway restart keeps every session's
 * unread dot.
 *
 * While a restart is still restoring sessions the gateway labels the `slots`
 * frames it pushes with `slotsPartial: true`. A session not yet restored is
 * absent from such a list without having been deleted, so the unread drain that
 * clears a remotely deleted slot's badge must not run against it. The hook reads
 * the label off every frame and the reducer skips only the drain while it is
 * set; the list itself renders as usual. A frame without the label is an older
 * gateway, which means complete and drains like before.
 *
 * Deliberately the SINGLETON store, for the same reason as
 * useWebSocket.slotsFrameDedupe.test.ts: the hook reads slots off the imported
 * singleton, so a separate Provider store would let reads and writes diverge.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { store as globalStore } from '../store'
import { sseSlots, sseSlotsPartial, markSlotUnread, markSlotRead } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { useWebSocket } from '../hooks/useWebSocket'
import { sharedItem } from './unreadStorageTestUtils'

vi.mock('../api/client', () => ({
  api: {
    // The connect-time refetch stays in flight for the whole test: its reply
    // is covered by the reducer-level test in dashboardSlice.test.ts.
    chatSlots: vi.fn().mockReturnValue(new Promise(() => {})),
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

const rowA: ChatSlot = { key: 'slot-a', title: 'A', agent: 'kirocrew' } as ChatSlot
const rowB: ChatSlot = { key: 'slot-b', title: 'B', agent: 'kirocrew' } as ChatSlot
const TS = '2026-01-01T00:00:00Z'

const unread = () => globalStore.getState().dashboard.unreadSlots
const unreadSince = (key: string) => globalStore.getState().dashboard.unreadSince[key]
const persisted = () => Object.keys(JSON.parse(sharedItem('mc-unread-shared') ?? '{}'))
const listed = () => globalStore.getState().dashboard.slots.map(s => s.key)

describe('useWebSocket slotsPartial label', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    localStorage.clear()
    // The window saw both sessions before the restart, with B unread.
    globalStore.dispatch(sseSlotsPartial(false))
    globalStore.dispatch(sseSlots([rowA, rowB]))
    globalStore.dispatch(markSlotUnread({ slot: 'slot-b', ts: TS }))
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    globalStore.dispatch(markSlotRead('slot-b'))
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: globalStore },
      createElement(QueryClientProvider, { client: qc }, children),
    )
  }

  /** Mount and open the socket: the reconnect of a window the restart cut off. */
  function mountOpened() {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return ws
  }

  it('keeps an absent key\'s unread entry and persisted copy across partial frames, then drains on the complete one', () => {
    const ws = mountOpened()
    expect(globalStore.getState().dashboard.unreadDrainHeld).toBe(true)

    // Mid-restore: only A is back so far. The list renders; the badge stays.
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA], slotsPartial: true }) })
    expect(listed()).toEqual(['slot-a'])
    expect(unread()).toEqual(['slot-b'])
    expect(unreadSince('slot-b')).toBe(TS)
    expect(persisted()).toEqual(['slot-b'])

    // A second identical partial frame (a slow restore pushes many) still drains nothing.
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA], slotsPartial: true }) })
    expect(unread()).toEqual(['slot-b'])
    expect(persisted()).toEqual(['slot-b'])

    // The restore finished and B really is gone: the complete frame drains it.
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA], slotsPartial: false }) })
    expect(globalStore.getState().dashboard.unreadDrainHeld).toBe(false)
    expect(unread()).toEqual([])
    expect(unreadSince('slot-b')).toBeUndefined()
    expect(persisted()).toEqual([])
  })

  it('keeps a held badge on a complete frame while draining an unheld badge', () => {
    const ws = mountOpened()
    act(() => { globalStore.dispatch(markSlotUnread({ slot: 'slot-c', ts: TS })) })

    act(() => { ws.simulateMessage({
      type: 'slots',
      data: [rowA],
      slotsPartial: false,
      slotsHeldKeys: ['slot-b'],
      slotsHoldChannels: false,
    }) })

    expect(unread()).toEqual(['slot-b'])
    expect(unreadSince('slot-b')).toBe(TS)
    expect(unreadSince('slot-c')).toBeUndefined()
  })

  it('keeps the badge when the restored session reappears on the complete frame', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA], slotsPartial: true }) })
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA, rowB], slotsPartial: false }) })
    expect(listed()).toEqual(['slot-a', 'slot-b'])
    expect(unread()).toEqual(['slot-b'])
    expect(persisted()).toEqual(['slot-b'])
  })

  it('drains an explicitly removed key while the reconnect hold is active', () => {
    const ws = mountOpened()
    expect(globalStore.getState().dashboard.unreadDrainHeld).toBe(true)

    act(() => { ws.simulateMessage({ type: 'slot_patch', data: { slots: [], removed: ['slot-b'] } }) })

    expect(unread()).toEqual([])
    expect(unreadSince('slot-b')).toBeUndefined()
    expect(persisted()).toEqual([])
  })

  it('drains on a frame without the label, as an older gateway sends', () => {
    const ws = mountOpened()
    act(() => { ws.simulateMessage({ type: 'slots', data: [rowA] }) })
    expect(globalStore.getState().dashboard.unreadDrainHeld).toBe(false)
    expect(unread()).toEqual([])
    expect(persisted()).toEqual([])
  })
})
