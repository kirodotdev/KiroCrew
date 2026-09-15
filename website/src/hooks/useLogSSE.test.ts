import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useLogSSE } from './useLogSSE'

/**
 * Regression coverage for the reconnect-timer leak.
 *
 * On error, useLogSSE schedules setTimeout(start, 3000). If the component
 * unmounts (or stop() runs) during that 3s window, the pending reconnect must
 * be cancelled — otherwise it fires afterward, opens a NEW EventSource on the
 * now-orphaned ref that nothing can close, and can spin an unbounded reconnect
 * loop with no owner.
 */

class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  close() {
    this.closed = true
  }
}

afterEach(() => {
  MockEventSource.instances = []
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('useLogSSE reconnect-timer cleanup', () => {
  it('does NOT reconnect after unmount during the error backoff window', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource)

    const { unmount } = renderHook(() => useLogSSE(() => {}))
    expect(MockEventSource.instances).toHaveLength(1)

    // Stream errors -> schedules a reconnect in 3s.
    act(() => { MockEventSource.instances[0].onerror?.() })
    expect(MockEventSource.instances[0].closed).toBe(true)

    // Unmount during the 3s window must clear the pending reconnect.
    unmount()
    act(() => { vi.advanceTimersByTime(5000) })

    // No second EventSource was ever opened -> no leak, no orphaned loop.
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('reconnects after 3s while still mounted', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource)

    renderHook(() => useLogSSE(() => {}))
    expect(MockEventSource.instances).toHaveLength(1)

    act(() => { MockEventSource.instances[0].onerror?.() })
    act(() => { vi.advanceTimersByTime(3000) })

    // A fresh stream was opened by the scheduled reconnect.
    expect(MockEventSource.instances).toHaveLength(2)
  })
})
