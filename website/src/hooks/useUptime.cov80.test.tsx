/**
 * useUptime — the live uptime string in the status bar.
 *
 * Three things to pin: the em-dash placeholder while the gateway start time is
 * unknown, the sub-hour vs over-hour formats (hours are omitted below an hour,
 * seconds always shown), and the one-second tick — including that the interval
 * is cleared on unmount so a torn-down status bar stops updating.
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

function harness(startTime?: number) {
  const store = createTestStore()
  if (startTime !== undefined) {
    store.dispatch(sseStatus({ start_time: startTime } as unknown as StatusData))
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Provider store={store}>{children}</Provider>
  )
  return renderHook(() => useUptime(), { wrapper })
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useUptime', () => {
  it('renders the placeholder while no start time is known', () => {
    const { result } = harness()
    expect(result.current).toBe('—')
  })

  it('omits hours below an hour and always shows seconds', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const { result } = harness(NOW_MS / 1000 - 38)
    expect(result.current).toBe('0m 38s')
  })

  it('includes hours once uptime passes an hour', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const { result } = harness(NOW_MS / 1000 - (2 * 3600 + 5 * 60 + 9))
    expect(result.current).toBe('2h 5m 9s')
  })

  it('ticks every second', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const { result } = harness(NOW_MS / 1000 - 10)
    expect(result.current).toBe('0m 10s')
    // advanceTimersByTime moves the mocked clock too, so the tick recomputes
    // against a Date.now() two seconds later.
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    expect(result.current).toBe('0m 12s')
  })

  it('clears its interval on unmount', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')
    const { unmount } = harness(NOW_MS / 1000 - 1)
    unmount()
    expect(clearSpy).toHaveBeenCalled()
    clearSpy.mockRestore()
  })
})
