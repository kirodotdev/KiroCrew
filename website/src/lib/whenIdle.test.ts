import { afterEach, describe, expect, it, vi } from 'vitest'
import { whenIdle } from './whenIdle'

describe('whenIdle', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('uses requestIdleCallback with the timeout, and cancels through cancelIdleCallback', () => {
    const ric = vi.fn((_cb: () => void, _o?: { timeout: number }) => 7)
    const cic = vi.fn()
    vi.stubGlobal('requestIdleCallback', ric)
    vi.stubGlobal('cancelIdleCallback', cic)
    const fn = vi.fn()
    const cancel = whenIdle(fn, 1234)
    expect(ric).toHaveBeenCalledWith(fn, { timeout: 1234 })
    cancel()
    expect(cic).toHaveBeenCalledWith(7)
  })

  it('falls back to a timer of the same length where there is no requestIdleCallback', () => {
    vi.stubGlobal('requestIdleCallback', undefined)
    vi.useFakeTimers()
    const fn = vi.fn()
    whenIdle(fn, 500)
    vi.advanceTimersByTime(499)
    expect(fn).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(fn).toHaveBeenCalledOnce()
    const later = vi.fn()
    whenIdle(later, 500)()
    vi.advanceTimersByTime(500)
    expect(later).not.toHaveBeenCalled()
  })
})
