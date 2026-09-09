/**
 * The pin-reconciliation channel settles the arrangement too.
 *
 * With the WebSocket down, an accepted last-pin unpin reconciles through React Query and
 * `updateSlotPin` -- it dispatches neither `sseSlots` nor `fetchSlots.fulfilled`, so the slot-frame
 * listener never sees it. Without this the stale marker survived and governed the next pin set.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
  commitPinnedSessionSnapshot,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

describe('pin reconciliation settles the arrangement without any slot frame', () => {
  beforeEach(() => localStorage.clear())

  it('forgets the arrangement when the reconciled snapshot has no pins left', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['solo']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')

    commitPinnedSessionSnapshot([])

    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBeNull()
  })

  it('keeps the arrangement while the reconciled snapshot still has a pin', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    localStorage.setItem(PINNED_SESSION_ORDER_MANUAL_KEY, '1')

    commitPinnedSessionSnapshot(['a'])

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })
})
