/** A frame that loaded off screen and was then moved on screen fires no new
 *  `load`, and that off-screen load can have been a 404: its deadline starts at
 *  mount. Any other frame waits for its own `load`. */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { FRAME_ALIVE_TIMEOUT_MS, useFrameAliveRecovery } from '../hooks/useFrameAliveRecovery'

function frame() {
  const el = document.createElement('iframe')
  Object.defineProperty(el, 'contentWindow', { configurable: true, value: { postMessage: () => {} } })
  return { current: el }
}

describe('useFrameAliveRecovery at mount', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('starts the deadline at once for a frame that already loaded', () => {
    vi.useFakeTimers()
    const recover = vi.fn()
    renderHook(() => useFrameAliveRecovery(frame(), 'https://localhost/doc', true, recover, true))
    act(() => { vi.advanceTimersByTime(FRAME_ALIVE_TIMEOUT_MS) })
    expect(recover).toHaveBeenCalledTimes(1)
  })

  it('waits for the load of a frame that has not loaded yet', () => {
    vi.useFakeTimers()
    const recover = vi.fn()
    renderHook(() => useFrameAliveRecovery(frame(), 'https://localhost/doc', true, recover))
    act(() => { vi.advanceTimersByTime(FRAME_ALIVE_TIMEOUT_MS * 2) })
    expect(recover).not.toHaveBeenCalled()
  })
})
