import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useFileWatch } from './useFileWatch'

/**
 * Regression coverage for the file-watch reconnect-loop guard.
 *
 * A directory or missing path makes the backend return 404 (api_file_watch's
 * os.path.isfile guard). EventSource auto-reconnects on error by default, so a
 * permanently-bad path (e.g. a directory opened via a clickable path chip)
 * would hammer /api/file-watch forever. useFileWatch must instead CLOSE the
 * stream on error and settle into a stable 'error' state.
 */

// Minimal controllable EventSource mock: capture instances and let tests fire
// lifecycle events synchronously.
class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  readyState = 0
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }
  close() {
    this.closed = true
    this.readyState = 2
  }
}

afterEach(() => {
  MockEventSource.instances = []
  vi.unstubAllGlobals()
})

describe('useFileWatch reconnect guard', () => {
  it('closes the EventSource and reports error (no reconnect) when the stream errors', () => {
    vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource)
    const { result } = renderHook(() => useFileWatch('/some/dir', () => {}))

    // One stream opened, status connecting.
    expect(MockEventSource.instances).toHaveLength(1)
    expect(result.current.status).toBe('connecting')

    // Backend 404 (directory) → onerror fires.
    act(() => { MockEventSource.instances[0].onerror?.() })

    // The stream is closed (so the browser cannot auto-reconnect) and the hook
    // settles into a stable 'error' state — it does NOT open a second stream.
    expect(MockEventSource.instances[0].closed).toBe(true)
    expect(result.current.status).toBe('error')
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('delivers content on message and is idle with no path', () => {
    vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource)
    const onContent = vi.fn()
    const { result, rerender } = renderHook(
      ({ p }: { p: string | null }) => useFileWatch(p, onContent),
      { initialProps: { p: '/a/file.md' as string | null } },
    )
    act(() => {
      MockEventSource.instances[0].onopen?.()
      MockEventSource.instances[0].onmessage?.({ data: JSON.stringify({ content: 'hi' }) })
    })
    expect(result.current.status).toBe('open')
    expect(onContent).toHaveBeenCalledWith('hi')

    // Clearing the path tears down and returns to idle.
    rerender({ p: null })
    expect(result.current.status).toBe('idle')
    expect(MockEventSource.instances[0].closed).toBe(true)
  })
})
