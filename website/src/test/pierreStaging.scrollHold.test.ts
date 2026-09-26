// #11244: the mid-scroll hold must not defer the drain forever under
// continuous scrolling.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  EAGER_ROWS,
  STAGE_MAX_DEFER_MS,
  __resetStagingForTests,
  requestStage,
} from '../components/pierreStaging'

describe('pierreStaging: scroll hold has a deferral ceiling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    __resetStagingForTests()
  })
  afterEach(() => {
    __resetStagingForTests()
    vi.useRealTimers()
  })

  it('drains a queued registrant even while scrolling never stops', () => {
    for (let i = 0; i < EAGER_ROWS; i++) requestStage(() => {})
    const released = vi.fn()
    requestStage(released)
    // A scroll every 100ms keeps every drain attempt inside the hold window.
    for (let t = 0; t < STAGE_MAX_DEFER_MS * 3; t += 100) {
      document.dispatchEvent(new Event('scroll'))
      vi.advanceTimersByTime(100)
    }
    expect(released).toHaveBeenCalledTimes(1)
  })

  it('still holds the drain for a short scroll', () => {
    for (let i = 0; i < EAGER_ROWS; i++) requestStage(() => {})
    const released = vi.fn()
    requestStage(released)
    document.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(100)
    expect(released).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)
    expect(released).toHaveBeenCalledTimes(1)
  })
})
