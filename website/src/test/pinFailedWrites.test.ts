/**
 * Two places a failed storage write silently cost more than the write.
 *
 * The adoption has exactly one chance per browser, so consuming its flag before the latch had taken
 * threw that chance away when the latch failed at quota. And a tab knows its own pending mutations
 * whether or not it managed to publish them, so reading them back only from storage let a failed
 * write hide live mutations from the guard that exists to see them.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  PINNED_SESSION_ORDER_ADOPTED_KEY,
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
  adoptStoredPinnedOrderAsManual,
} from '../utils/pinnedSessionOrder'


/** Fails writes for keys matching, as a full quota does. */
function refuseWritesTo(match: (key: string) => boolean): void {
  const real = Storage.prototype.setItem
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
    if (match(key)) throw new DOMException('quota', 'QuotaExceededError')
    return real.call(this, key, value)
  })
}

describe('a failed write must not consume the adoption', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('leaves the question open when the latch could not be written', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    refuseWritesTo(key => key === PINNED_SESSION_ORDER_MANUAL_KEY)

    expect(adoptStoredPinnedOrderAsManual()).toBe(false)

    // Unconsumed, so the next load can still adopt the arrangement.
    expect(localStorage.getItem(PINNED_SESSION_ORDER_ADOPTED_KEY)).toBeNull()
  })

  it('still settles the question when there was nothing to adopt', () => {
    expect(adoptStoredPinnedOrderAsManual()).toBe(false)
    expect(localStorage.getItem(PINNED_SESSION_ORDER_ADOPTED_KEY)).toBe('1')
  })
})

describe('an adoption whose receipt is refused', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  beforeEach(() => {
    localStorage.clear()
  })

  it('leaves no marker behind, so a later opt-out is not undone on reload', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    refuseWritesTo(key => key === PINNED_SESSION_ORDER_ADOPTED_KEY)

    expect(adoptStoredPinnedOrderAsManual()).toBe(false)
    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBeNull()
  })
})
