/**
 * Test: useDashboardHealthProbe.
 * Covers the same-tab self-recovery hook for gateway-disconnect.
 *
 * Verifies:
 *   - When `connected=true` (steady state), no polling happens.
 *   - On INITIAL page load (connected=false, never been true), the probe is
 *     silent -- it only kicks in for genuine post-connect disconnects.
 *   - When the WS goes connected -> disconnected, /api/status is polled
 *     immediately + on interval, and a 200 response triggers forceReconnect
 *     exactly once per disconnect cycle.
 *   - When the probe rejects (e.g. /api/status 403 -> throws via the j
 *     wrapper, or network error), forceReconnect is NOT called.
 *   - When the connected flag flips back to true mid-poll-cycle, the
 *     interval is cleared.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { Provider } from 'react-redux'
import type { RootState } from '../store'
import { createTestStore } from './helpers'

const apiStatusMock = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  api: { status: apiStatusMock },
}))

import { useDashboardHealthProbe } from '../hooks/useDashboardHealthProbe'

function makeStore(connected: boolean) {
  return createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected,
      slots: [], approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0,
      unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as RootState['dashboard'],
    chat: {
      activeSlot: null, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as RootState['chat'],
  })
}

function wrapper(store: ReturnType<typeof makeStore>) {
  return ({ children }: { children: React.ReactNode }) => (
    <Provider store={store}>{children}</Provider>
  )
}

/**
 * Render the hook in a state that simulates "WS was connected, then dropped".
 * Real recovery scenarios always have this trajectory -- the dashboard mounts
 * with connected=false during the WS handshake, transitions to true on
 * successful connect, then drops to false again on gateway restart / network
 * blip / dev reload. The probe is intentionally only active for that final
 * drop, NOT for the initial false during handshake.
 *
 * Existing assertions (forceReconnect fires, polling continues, unmount
 * cleanup, rejection path) test the post-disconnect behaviour, so they all
 * need this trajectory established first.
 */
async function renderProbeAfterDisconnect(forceReconnect: () => void) {
  const store = makeStore(true)  // start connected so hasEverConnected becomes true
  const { rerender, unmount } = renderHook(
    () => useDashboardHealthProbe(forceReconnect),
    { wrapper: wrapper(store) },
  )
  // Drop to disconnected -- the recovery scenario the probe handles.
  store.dispatch({ type: 'dashboard/sseDisconnected' })
  rerender()
  return { store, rerender, unmount }
}

describe('useDashboardHealthProbe', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    apiStatusMock.mockReset()
  })

  afterEach(() => {
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
  })

  it('does NOT poll when connected=true', async () => {
    const forceReconnect = vi.fn()
    apiStatusMock.mockResolvedValue({ ok: true })
    const store = makeStore(true)
    renderHook(() => useDashboardHealthProbe(forceReconnect), { wrapper: wrapper(store) })
    // Advance 10s -- should still be 0 polls because we're connected.
    await vi.advanceTimersByTimeAsync(10_000)
    expect(apiStatusMock).not.toHaveBeenCalled()
    expect(forceReconnect).not.toHaveBeenCalled()
  })

  it('does NOT poll on initial page load when connected starts false (regression: pre-fix, this fired probe + tore down in-flight WS)', async () => {
    // dashboardSlice initial state is `connected: false`, which is also the
    // state during a fresh page load while useWebSocket's mount effect still
    // has its initial WS in CONNECTING. Without the hasEverConnected gate,
    // the probe would hit /api/status, get 200, and call forceReconnect()
    // -- tearing down the in-flight initial WS on every normal page load.
    // This pins the gate: no probe fires until we've actually been connected
    // at least once.
    const forceReconnect = vi.fn()
    apiStatusMock.mockResolvedValue({ ok: true })
    const store = makeStore(false)
    renderHook(() => useDashboardHealthProbe(forceReconnect), { wrapper: wrapper(store) })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(apiStatusMock).not.toHaveBeenCalled()
    expect(forceReconnect).not.toHaveBeenCalled()
  })

  it('starts polling only AFTER connected has been true at least once, then drops to false', async () => {
    // Drives the realistic timeline:
    //   1. Page load: connected=false  -> probe disabled (no first connect yet)
    //   2. WS opens:   connected=true   -> hasEverConnected=true; probe still off (healthy)
    //   3. WS drops:   connected=false  -> NOW the probe should fire to recover
    const forceReconnect = vi.fn()
    apiStatusMock.mockResolvedValue({ ok: true })
    const store = makeStore(false)
    const { rerender } = renderHook(() => useDashboardHealthProbe(forceReconnect), { wrapper: wrapper(store) })
    // Step 1: still disconnected on initial load -- probe must NOT fire
    await vi.advanceTimersByTimeAsync(0)
    expect(apiStatusMock).not.toHaveBeenCalled()
    // Step 2: simulate WS connect -> connected=true
    store.dispatch({ type: 'dashboard/sseConnected' })
    rerender()
    await vi.advanceTimersByTimeAsync(5_000)
    // Still no probe -- we're connected, no recovery needed
    expect(apiStatusMock).not.toHaveBeenCalled()
    // Step 3: simulate WS drop -> connected=false; probe SHOULD now fire
    store.dispatch({ type: 'dashboard/sseDisconnected' })
    rerender()
    await vi.advanceTimersByTimeAsync(0)
    await Promise.resolve()
    expect(apiStatusMock).toHaveBeenCalled()
    expect(forceReconnect).toHaveBeenCalledTimes(1)
  })

  it('polls /api/status immediately on a real disconnect (after the first successful connect)', async () => {
    const forceReconnect = vi.fn()
    apiStatusMock.mockResolvedValue({ ok: true })
    await renderProbeAfterDisconnect(forceReconnect)
    // First probe is immediate (no interval delay).
    await vi.advanceTimersByTimeAsync(0)
    expect(apiStatusMock).toHaveBeenCalledTimes(1)
  })

  it('calls forceReconnect when /api/status returns success', async () => {
    const forceReconnect = vi.fn()
    apiStatusMock.mockResolvedValue({ ok: true })
    await renderProbeAfterDisconnect(forceReconnect)
    // Let the immediate probe + microtask resolve.
    await vi.advanceTimersByTimeAsync(0)
    await Promise.resolve()
    expect(forceReconnect).toHaveBeenCalled()
  })

  it('only calls forceReconnect ONCE per disconnect cycle, even if WS handshake exceeds INTERVAL_MS', async () => {
    // Regression: without `cancelled = true` after the first success, the
    // 3s interval would tick again, hit api.status() success, and call
    // forceReconnect() a second time -- tearing down the in-progress WS
    // and starting a new one in a reconnection loop. This test pins that
    // forceReconnect fires AT MOST ONCE between disconnect and reconnect,
    // even when the probe stays successful for many intervals (simulating
    // a slow WS handshake e.g. over SSH tunnel).
    const forceReconnect = vi.fn()
    apiStatusMock.mockResolvedValue({ ok: true })
    await renderProbeAfterDisconnect(forceReconnect)
    // Immediate probe + 5 interval ticks (15s of "successful" probes).
    await vi.advanceTimersByTimeAsync(0)
    await Promise.resolve()
    for (let i = 0; i < 5; i++) {
      await vi.advanceTimersByTimeAsync(3000)
      await Promise.resolve()
    }
    expect(forceReconnect).toHaveBeenCalledTimes(1)
  })

  it('does NOT call forceReconnect when /api/status rejects', async () => {
    const forceReconnect = vi.fn()
    apiStatusMock.mockRejectedValue(new Error('HTTP 403'))
    await renderProbeAfterDisconnect(forceReconnect)
    await vi.advanceTimersByTimeAsync(0)
    await Promise.resolve()
    await Promise.resolve()
    expect(forceReconnect).not.toHaveBeenCalled()
  })

  it('continues polling on subsequent intervals while disconnected', async () => {
    const forceReconnect = vi.fn()
    apiStatusMock.mockRejectedValue(new Error('still down'))
    await renderProbeAfterDisconnect(forceReconnect)
    // Immediate probe + 2 interval ticks (3s each = 6s total).
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(3000)
    await vi.advanceTimersByTimeAsync(3000)
    expect(apiStatusMock.mock.calls.length).toBeGreaterThanOrEqual(3)
    expect(forceReconnect).not.toHaveBeenCalled()
  })

  it('stops polling when the hook unmounts', async () => {
    const forceReconnect = vi.fn()
    apiStatusMock.mockRejectedValue(new Error('still down'))
    const { unmount } = await renderProbeAfterDisconnect(forceReconnect)
    await vi.advanceTimersByTimeAsync(0)
    const callsBeforeUnmount = apiStatusMock.mock.calls.length
    unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(apiStatusMock.mock.calls.length).toBe(callsBeforeUnmount)
  })
})
