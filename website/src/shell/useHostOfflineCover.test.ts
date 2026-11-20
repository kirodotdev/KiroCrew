import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { QUIET_RECONNECT_GRACE_MS } from '../utils/reconnectBackoff'
import { clearGatewayRestart, noteGatewayRestart } from '../lib/gatewayRestart'
import type { OfflineCover } from '../utils/offlineReason'
import { useHostOfflineCover } from './useHostOfflineCover'

describe('useHostOfflineCover — the host view waits out a short drop and a restart', () => {
  beforeEach(() => { vi.useFakeTimers(); clearGatewayRestart() })
  afterEach(() => { vi.useRealTimers(); clearGatewayRestart() })

  const mount = () => renderHook(
    ({ cover, connected }: { cover: OfflineCover; connected: boolean }) => useHostOfflineCover(cover, connected),
    { initialProps: { cover: null as OfflineCover, connected: true } },
  )

  it('shows no cover for a drop that reconnects inside the grace', () => {
    const { result, rerender } = mount()
    rerender({ cover: 'host', connected: false })
    expect(result.current).toBeNull()
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS - 1) })
    rerender({ cover: null, connected: true })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS) })
    expect(result.current).toBeNull()
  })

  it('shows the host view once the drop outlasts the grace', () => {
    const { result, rerender } = mount()
    rerender({ cover: 'host', connected: false })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS) })
    expect(result.current).toBe('host')
    rerender({ cover: null, connected: true })
    expect(result.current).toBeNull()
  })

  it('holds the host view off while a restart this tab asked for is pending', () => {
    const { result, rerender } = mount()
    act(() => { noteGatewayRestart() })
    rerender({ cover: 'host', connected: false })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS * 10) })
    expect(result.current).toBeNull()
    // The restarted gateway answers: the request is spent, so a later drop counts again.
    rerender({ cover: null, connected: true })
    rerender({ cover: 'host', connected: false })
    act(() => { vi.advanceTimersByTime(QUIET_RECONNECT_GRACE_MS) })
    expect(result.current).toBe('host')
  })

  it('passes the relay window cover through at once', () => {
    const { result, rerender } = mount()
    rerender({ cover: 'relay', connected: false })
    expect(result.current).toBe('relay')
  })
})
