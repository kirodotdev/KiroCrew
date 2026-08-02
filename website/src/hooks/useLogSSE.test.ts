import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useLogSSE } from './useLogSSE'

class MockEventSource {
  static instances: MockEventSource[] = []
  readyState = 0
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  url: string

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
}

describe('useLogSSE', () => {
  beforeEach(() => {
    MockEventSource.instances = []
    vi.stubGlobal('EventSource', MockEventSource)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('cleans up EventSource on unmount', () => {
    const onMessage = vi.fn()
    const { unmount } = renderHook(() => useLogSSE(onMessage))

    expect(MockEventSource.instances).toHaveLength(1)
    const sse = MockEventSource.instances[0]

    unmount()
    expect(sse.close).toHaveBeenCalled()
  })

  it('cancels pending reconnect timer on unmount during reconnect window', () => {
    const onMessage = vi.fn()
    const { unmount } = renderHook(() => useLogSSE(onMessage))

    expect(MockEventSource.instances).toHaveLength(1)
    const sse = MockEventSource.instances[0]

    // Trigger an error - this schedules a reconnect in 3s
    act(() => {
      sse.onerror!()
    })

    // Unmount during the 3s reconnect window
    unmount()

    // Advance timers past the reconnect delay
    act(() => {
      vi.advanceTimersByTime(5000)
    })

    // No new EventSource should have been created after the first one
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('does not run message handler after unmount', () => {
    const onMessage = vi.fn()
    const { unmount } = renderHook(() => useLogSSE(onMessage))

    const sse = MockEventSource.instances[0]
    unmount()

    // Simulate a message arriving after unmount
    const event = { data: JSON.stringify({ level: 'info', msg: 'test' }) } as MessageEvent
    sse.onmessage?.(event)

    expect(onMessage).not.toHaveBeenCalled()
  })

  it('does not schedule reconnect after unmount when error fires post-close', () => {
    const onMessage = vi.fn()
    const { unmount } = renderHook(() => useLogSSE(onMessage))

    const sse = MockEventSource.instances[0]
    unmount()

    // Simulate error firing after unmount (race condition)
    act(() => {
      sse.onerror!()
    })

    act(() => {
      vi.advanceTimersByTime(5000)
    })

    // Still only 1 instance - no reconnect happened
    expect(MockEventSource.instances).toHaveLength(1)
  })
})
