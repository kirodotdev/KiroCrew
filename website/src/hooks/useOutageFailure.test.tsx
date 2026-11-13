/**
 * A status read that failed while the gateway was down (a restart) is the
 * offline state's to report, not a per-feature alert's (#32966).
 */
import { afterEach, describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import { __resetGatewayConnectedForTests, setGatewayConnected } from '../utils/errorReport'
import { useOutageFailure } from './useOutageFailure'

type Read = { isError: boolean; errorUpdatedAt: number }

function renderRead(initial: Read) {
  const refetch = vi.fn()
  const hook = renderHook(
    ({ isError, errorUpdatedAt }: Read) => useOutageFailure(isError, errorUpdatedAt, refetch),
    { initialProps: initial },
  )
  return { ...hook, refetch }
}

afterEach(() => { __resetGatewayConnectedForTests() })

describe('useOutageFailure', () => {
  it('is false for a failure while connected: that one is reported', () => {
    const { result, refetch } = renderRead({ isError: true, errorUpdatedAt: 1 })
    expect(result.current).toBe(false)
    expect(refetch).not.toHaveBeenCalled()
  })

  it('holds a failure from the outage back, re-reads it once on reconnect, and reports the next failure', () => {
    const { result, rerender, refetch } = renderRead({ isError: false, errorUpdatedAt: 0 })

    act(() => { setGatewayConnected(false) })
    rerender({ isError: true, errorUpdatedAt: 5 })
    expect(result.current).toBe(true)
    expect(refetch).not.toHaveBeenCalled()

    // Back online with the outage's failure still cached: still the outage's,
    // and read again so a reader that does not poll is not left on it.
    act(() => { setGatewayConnected(true) })
    expect(result.current).toBe(true)
    expect(refetch).toHaveBeenCalledTimes(1)
    rerender({ isError: true, errorUpdatedAt: 5 })
    expect(refetch).toHaveBeenCalledTimes(1)

    // That re-read failed with the gateway reachable: a real failure.
    rerender({ isError: true, errorUpdatedAt: 9 })
    expect(result.current).toBe(false)
    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('is false once a read succeeds', () => {
    const { result, rerender } = renderRead({ isError: true, errorUpdatedAt: 3 })
    act(() => { setGatewayConnected(false) })
    expect(result.current).toBe(true)
    act(() => { setGatewayConnected(true) })
    rerender({ isError: false, errorUpdatedAt: 3 })
    expect(result.current).toBe(false)
  })
})
