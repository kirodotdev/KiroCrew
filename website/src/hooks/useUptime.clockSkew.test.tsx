/**
 * useUptime under clock skew: the gateway's start_time comes from the gateway's
 * clock, the "now" it is subtracted from comes from the browser's. A remote
 * gateway whose clock runs ahead hands the browser a start_time in its future,
 * and the uptime must read zero until the browser's clock catches up rather
 * than a negative duration counting toward zero.
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { createTestStore } from '../test/helpers'
import { sseStatus } from '../store/dashboardSlice'
import type { StatusData } from '../types'
import { useUptime } from './useUptime'

const NOW_MS = 1_700_000_000_000

function harness(startTime: number) {
  const store = createTestStore()
  store.dispatch(sseStatus({ start_time: startTime } as unknown as StatusData))
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Provider store={store}>{children}</Provider>
  )
  return renderHook(() => useUptime(), { wrapper })
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useUptime clock skew', () => {
  it('reads 0m 0s, not a negative uptime, when the gateway start_time is ahead of the browser clock', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const { result } = harness(NOW_MS / 1000 + 5)
    expect(result.current).toBe('0m 0s')
  })

  it('holds at 0m 0s while the skew lasts, then counts up once the browser clock passes start_time', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const { result } = harness(NOW_MS / 1000 + 5)
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(result.current).toBe('0m 0s')
    act(() => {
      vi.advanceTimersByTime(4000)
    })
    expect(result.current).toBe('0m 2s')
  })

  it('control: a start time 38s in the past still reads 0m 38s', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const { result } = harness(NOW_MS / 1000 - 38)
    expect(result.current).toBe('0m 38s')
  })
})
