/**
 * #765 — the duplicated boot fetchNotifications round-trip.
 *
 * App's mount effect and the WebSocket's FIRST-connect handler used to each
 * dispatch `fetchNotifications` — two identical round-trips on every boot.
 * The first-connect copy is the one that must survive: its HTTP snapshot is
 * taken AFTER the socket is registered, so a notification created after the
 * snapshot is guaranteed to arrive as a WS push. A mount-time snapshot has no
 * such guarantee — a notification created between it and socket registration
 * is pushed to nobody and would be invisible until a reconnect. So the mount
 * effect no longer fetches; it arms a timed fallback that fires only when no
 * boot fetch happened (a socket that never connects), and first-connect marks
 * the boot fetch done before dispatching.
 *
 * Pinned here:
 *  1. first connect dispatches exactly ONE notifications fetch, with
 *     syncPendingApprovals gated on it settling (the ordering guarantee);
 *  2. the armed fallback does NOT fire once first-connect marked the boot
 *     fetch (no double-fire);
 *  3. the fallback DOES fire when nothing marked it (no-WS boot still
 *     populates the inbox over plain HTTP);
 *  4. disarming (App unmount) cancels a pending fallback;
 *  5. a first connect that lands AFTER the fallback fired serializes its own
 *     fetch behind the fallback's in-flight one, so the older (pre-
 *     registration) snapshot can never replace the newer one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import {
  armBootNotificationsFallback,
  markBootNotificationsFetched,
  resetBootNotificationsForTest,
  BOOT_NOTIFICATIONS_FALLBACK_MS,
} from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

import { api } from '../api/client'

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
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

describe('useWebSocket boot notifications dedupe (#765)', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    resetBootNotificationsForTest()
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    resetBootNotificationsForTest()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  const renderWs = () => {
    const store = createTestStore()
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(Provider, { store },
        createElement(QueryClientProvider, { client: queryClient }, children))
    renderHook(() => useWebSocket(), { wrapper })
    return store
  }

  it('first connect dispatches exactly one notifications fetch, approvals gated on it settling', async () => {
    // Hold the notifications response open so the ordering is observable.
    let settleNotifications!: (v: { notifications: never[]; unread: number }) => void
    vi.mocked(api.notifications).mockReturnValueOnce(
      new Promise(resolve => { settleNotifications = resolve }),
    )

    renderWs()
    act(() => { WS_INSTANCES[0].simulateOpen() })
    await flush()

    // One boot fetch — the first-connect copy — and nothing else.
    expect(api.notifications).toHaveBeenCalledTimes(1)
    // The ordering guarantee: approvals sync waits for the fetch to settle.
    expect(api.approvals).not.toHaveBeenCalled()

    settleNotifications({ notifications: [], unread: 0 })
    await flush()
    expect(api.approvals).toHaveBeenCalledTimes(1)
    expect(api.notifications).toHaveBeenCalledTimes(1)
  })

  it('the armed fallback never double-fires after first connect marked the boot fetch', async () => {
    vi.useFakeTimers()
    const fallback = vi.fn()
    const disarm = armBootNotificationsFallback(fallback)

    renderWs()
    act(() => { WS_INSTANCES[0].simulateOpen() })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(api.notifications).toHaveBeenCalledTimes(1)

    // Let the fallback window lapse — first-connect marked the fetch, so the
    // timer body must be a no-op.
    await act(async () => { await vi.advanceTimersByTimeAsync(BOOT_NOTIFICATIONS_FALLBACK_MS + 1000) })
    expect(fallback).not.toHaveBeenCalled()
    expect(api.notifications).toHaveBeenCalledTimes(1)
    disarm()
  })

  it('the fallback fires when the socket never connects', () => {
    vi.useFakeTimers()
    const fallback = vi.fn()
    armBootNotificationsFallback(fallback)

    vi.advanceTimersByTime(BOOT_NOTIFICATIONS_FALLBACK_MS + 1)
    expect(fallback).toHaveBeenCalledTimes(1)

    // And having fired, it marked the boot fetch itself: a late mark or a
    // second armed window must not run again.
    const second = vi.fn()
    armBootNotificationsFallback(second)
    vi.advanceTimersByTime(BOOT_NOTIFICATIONS_FALLBACK_MS + 1)
    expect(second).not.toHaveBeenCalled()
  })

  it('disarming cancels a pending fallback (App unmount cleanup)', () => {
    vi.useFakeTimers()
    const fallback = vi.fn()
    const disarm = armBootNotificationsFallback(fallback)
    disarm()
    vi.advanceTimersByTime(BOOT_NOTIFICATIONS_FALLBACK_MS + 1)
    expect(fallback).not.toHaveBeenCalled()
  })

  it('a late mark after the fallback window changes nothing retroactively', () => {
    vi.useFakeTimers()
    const fallback = vi.fn()
    armBootNotificationsFallback(fallback)
    vi.advanceTimersByTime(BOOT_NOTIFICATIONS_FALLBACK_MS + 1)
    expect(fallback).toHaveBeenCalledTimes(1)
    // Reconnect-era mark must not throw or re-trigger anything.
    markBootNotificationsFetched()
    expect(fallback).toHaveBeenCalledTimes(1)
  })

  it('a late first connect serializes its fetch behind a fired fallback (newest snapshot lands last)', async () => {
    vi.useFakeTimers()
    // The fallback's request is slow: it is still in flight when the socket
    // finally connects. The bug this pins: without serialization the connect
    // fetch resolves first and the older fallback payload then replaces
    // membership -- notifications gone until a reconnect.
    let settleFallbackFetch!: (v: unknown) => void
    const fallbackFetch = new Promise(resolve => { settleFallbackFetch = resolve })
    armBootNotificationsFallback(() => fallbackFetch)
    vi.advanceTimersByTime(BOOT_NOTIFICATIONS_FALLBACK_MS + 1)

    renderWs()
    act(() => { WS_INSTANCES[0].simulateOpen() })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    // The connect fetch must NOT have been dispatched yet -- it waits for the
    // fallback's in-flight request to settle.
    expect(api.notifications).not.toHaveBeenCalled()

    settleFallbackFetch({ notifications: [], unread: 0 })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    // Now the post-registration snapshot goes out -- strictly after the older
    // one settled -- and approvals sync still trails the whole chain.
    expect(api.notifications).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(api.approvals).toHaveBeenCalledTimes(1)
  })
})
