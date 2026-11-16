import { describe, it, expect, afterEach, vi } from 'vitest'
import { yieldToMain } from './yieldToMain'

/**
 * The promise must settle in a LATER task, never in the caller's own: a caller
 * that awaits it is splitting one long task in two, and a promise that settled
 * as a microtask would leave the two halves in the same task.
 */
describe('yieldToMain', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('uses scheduler.yield where the browser has it', async () => {
    const yielded = vi.fn(() => Promise.resolve())
    vi.stubGlobal('scheduler', { yield: yielded })
    await yieldToMain()
    expect(yielded).toHaveBeenCalledTimes(1)
  })

  it('falls back to a timer, which settles in a later task and not as a microtask', async () => {
    vi.stubGlobal('scheduler', undefined)
    vi.useFakeTimers()
    let settled = false
    void yieldToMain().then(() => { settled = true })
    // Every microtask of this task has run; the timer has not.
    await Promise.resolve()
    await Promise.resolve()
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(0)
    expect(settled).toBe(true)
  })

  it('falls back to the timer where scheduler exists without yield', async () => {
    vi.stubGlobal('scheduler', { postTask: () => Promise.resolve() })
    vi.useFakeTimers()
    let settled = false
    void yieldToMain().then(() => { settled = true })
    await vi.advanceTimersByTimeAsync(0)
    expect(settled).toBe(true)
  })
})
