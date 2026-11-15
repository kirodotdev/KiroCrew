import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { pollWhileVisible } from './pollWhileVisible'

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('pollWhileVisible', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setHidden(false)
  })
  afterEach(() => {
    vi.useRealTimers()
    setHidden(false)
  })

  it('repeats on the cadence while the page is on screen, and leaves the first tick to the caller', () => {
    const tick = vi.fn()
    const stop = pollWhileVisible(tick, 3000)
    expect(tick).not.toHaveBeenCalled()
    vi.advanceTimersByTime(9000)
    expect(tick).toHaveBeenCalledTimes(3)
    stop()
  })

  it('does not tick while the page is hidden', () => {
    const tick = vi.fn()
    const stop = pollWhileVisible(tick, 3000)
    vi.advanceTimersByTime(3000)
    expect(tick).toHaveBeenCalledTimes(1)
    setHidden(true)
    vi.advanceTimersByTime(60_000)
    expect(tick).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
    stop()
  })

  it('ticks once when the page comes back, then keeps the cadence from there', () => {
    const tick = vi.fn()
    const stop = pollWhileVisible(tick, 3000)
    setHidden(true)
    vi.advanceTimersByTime(60_000)
    expect(tick).not.toHaveBeenCalled()
    setHidden(false)
    expect(tick).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(2999)
    expect(tick).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1)
    expect(tick).toHaveBeenCalledTimes(2)
    stop()
  })

  it('starts no timer for a page that opens hidden, and starts when it is first shown', () => {
    setHidden(true)
    const tick = vi.fn()
    const stop = pollWhileVisible(tick, 3000)
    expect(vi.getTimerCount()).toBe(0)
    setHidden(false)
    expect(tick).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(1)
    stop()
  })

  it('a second visibility event while visible neither ticks nor adds a timer', () => {
    const tick = vi.fn()
    const stop = pollWhileVisible(tick, 3000)
    setHidden(false)
    expect(tick).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(1)
    stop()
  })

  it('stop clears the timer and stops listening', () => {
    const tick = vi.fn()
    const stop = pollWhileVisible(tick, 3000)
    stop()
    expect(vi.getTimerCount()).toBe(0)
    setHidden(true)
    setHidden(false)
    vi.advanceTimersByTime(60_000)
    expect(tick).not.toHaveBeenCalled()
  })
})
