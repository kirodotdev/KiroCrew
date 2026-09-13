import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest'
import {
  PINNED_SESSION_ORDER_MANUAL_KEY,
  PINNED_SESSION_ORDER_REVISION_KEY,
  markPinnedSessionOrderManual,
} from '../utils/pinnedSessionOrder'

/**
 * The marker and the revision are one statement. A marker standing on a revision that never persisted
 * disarms the sibling staleness guard, so a pre-ack zero-pin frame in another tab would clear it, and
 * the curated rank would stop applying. The statement therefore aborts instead of half-landing.
 */
describe('a reorder whose revision write is refused', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  beforeEach(() => {
    localStorage.clear()
  })

  it('aborts instead of leaving a marker on a revision that never persisted', () => {
    const real = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      if (key === PINNED_SESSION_ORDER_REVISION_KEY) throw new DOMException('quota', 'QuotaExceededError')
      return real.call(this, key, value)
    })

    expect(markPinnedSessionOrderManual()).toBe(false)
    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBeNull()
  })
})
