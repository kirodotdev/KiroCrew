/**
 * useWebSocket `notification` and `approval` frames -> MC_LIVE_NOTIFICATION_EVENT
 * relay.
 *
 * The in-app banner listens to this event, not to the store, so the socket
 * layer must (a) fire it for a frame received on a live connection and
 * (b) NOT fire it during a reconnect catch-up replay, where the frames are
 * history the bell already holds. The store still receives every frame
 * either way — suppression is about the banner, never about the inbox.
 *
 * An approval is a blocking event: while the window is focused the banner is
 * its visible interrupt (the OS toast stays quiet for a focused window), so
 * the `approval` frame relays its feed note the same way.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { MC_LIVE_NOTIFICATION_EVENT, type McLiveNotificationDetail } from '../hooks/notificationEvent'
import { shouldBannerNote } from '../hooks/notificationBanner'
import type { Notification } from '../types'

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

  simulateClose() {
    this.readyState = 3
    this.onclose?.(new CloseEvent('close'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

describe('useWebSocket notification -> MC_LIVE_NOTIFICATION_EVENT', () => {
  let queryClient: QueryClient
  let store: ReturnType<typeof createTestStore>
  const seen: string[] = []
  const notes: Notification[] = []
  const listener = (e: Event) => { const n = (e as CustomEvent<McLiveNotificationDetail>).detail.note; seen.push(n.ts); notes.push(n) }

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    seen.length = 0
    notes.length = 0
    store = createTestStore()
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    window.addEventListener(MC_LIVE_NOTIFICATION_EVENT, listener)
  })

  afterEach(() => {
    window.removeEventListener(MC_LIVE_NOTIFICATION_EVENT, listener)
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store },
      createElement(QueryClientProvider, { client: queryClient }, children))
  }

  it('fires for a frame on a live connection, carrying the whole note', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => {
      ws.simulateMessage({ type: 'notification', data: { kind: 'cron', ts: 'live-1', title: 'done', body: '' } })
    })
    expect(seen).toEqual(['live-1'])
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual(['live-1'])
  })

  it('stays silent for frames replayed during a reconnect catch-up, while the store still receives them', () => {
    vi.useFakeTimers()
    renderHook(() => useWebSocket(), { wrapper })
    const first = WS_INSTANCES[0]
    act(() => { first.simulateOpen() })
    act(() => { first.simulateClose() })
    // The reconnect backoff opens a second socket; its open handler marks the
    // catch-up window, which stays open until fetchSlots settles. Delivering
    // the replayed frame inside the same act keeps it in that window.
    act(() => { vi.runOnlyPendingTimers() })
    const second = WS_INSTANCES[1]
    expect(second).toBeTruthy()
    act(() => {
      second.simulateOpen()
      second.simulateMessage({ type: 'notification', data: { kind: 'cron', ts: 'replayed-1', title: 'old', body: '' } })
    })
    expect(seen).toEqual([])
    expect(store.getState().notifications.items.map(n => n.ts)).toContain('replayed-1')
  })

  it('relays an approval frame on a live connection, carrying its owning slot', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-live-1', slot: 'slot-a', source: 'agent', tool: 'Bash', tool_input: '{}', ts: 7 },
      })
    })
    expect(seen).toEqual(['7'])
    expect(notes[0].kind).toBe('approval')
    expect(notes[0].approval_id).toBe('ap-live-1')
    expect(notes[0].slot).toBe('slot-a')
    // The relayed note IS the feed entry, not a second object.
    expect(store.getState().notifications.items.find(n => n.approval_id === 'ap-live-1')?.slot).toBe('slot-a')
  })

  it('relays an unowned approval with no slot, so the banner cannot mistake it for the active chat', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    act(() => {
      ws.simulateMessage({
        type: 'approval',
        data: { id: 'ap-live-2', source: 'cron', tool: 'Bash', tool_input: '{}', ts: 8 },
      })
    })
    expect(seen).toEqual(['8'])
    expect('slot' in notes[0]).toBe(false)
  })

  it('the banner gate shows the approval on another surface and skips it over its own open chat', () => {
    const note = { kind: 'approval', title: 'Tool approval: Bash', body: '', ts: '9', approval_id: 'ap-gate', slot: 'slot-a' } as Notification
    const focused = { enabled: true, popoverOpen: false, windowFocused: true }
    // Settings page, or a chat showing a different slot: the banner is the interrupt.
    expect(shouldBannerNote(note, { ...focused, pathname: '/settings', activeSlot: 'slot-a' })).toBe(true)
    expect(shouldBannerNote(note, { ...focused, pathname: '/chat', activeSlot: 'slot-b' })).toBe(true)
    // The owning chat is on screen: the inline permission card already shows it.
    expect(shouldBannerNote(note, { ...focused, pathname: '/chat', activeSlot: 'slot-a' })).toBe(false)
  })

  it('stays silent for an approval replayed during a reconnect catch-up', () => {
    vi.useFakeTimers()
    renderHook(() => useWebSocket(), { wrapper })
    const first = WS_INSTANCES[0]
    act(() => { first.simulateOpen() })
    act(() => { first.simulateClose() })
    act(() => { vi.runOnlyPendingTimers() })
    const second = WS_INSTANCES[1]
    expect(second).toBeTruthy()
    act(() => {
      second.simulateOpen()
      second.simulateMessage({
        type: 'approval',
        data: { id: 'ap-replay-1', slot: 'slot-a', source: 'agent', tool: 'Bash', tool_input: '{}', ts: 10 },
      })
    })
    expect(seen).toEqual([])
    expect(store.getState().notifications.items.find(n => n.approval_id === 'ap-replay-1')).toBeDefined()
  })
})
