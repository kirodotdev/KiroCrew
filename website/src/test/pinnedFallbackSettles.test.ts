/**
 * The fallback commit path settles the arrangement too.
 *
 * When the reconciliation GET fails, `commitPinnedSessionOperations` is the ONLY membership
 * statement that reaches storage before the user re-pins -- no authoritative frame follows a
 * failed fetch. Without settling here, a marker left at `'1'` refroze the next pin set in
 * pin-toggle order, which is the defect this change exists to remove.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  PINNED_SESSION_ORDER_KEY,
  PINNED_SESSION_ORDER_MANUAL_KEY,
  commitPinnedSessionOperations,
  markPinnedSessionOrderManual,
  readPinnedSessionOrderIsManual,
} from '../utils/pinnedSessionOrder'

describe('the failed-reconciliation fallback settles the arrangement', () => {
  beforeEach(() => localStorage.clear())

  it('clears the marker when unpinning the last pinned session', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['solo']))
    markPinnedSessionOrderManual()

    // The shape the catch path commits: the GET never returned, so this is all we know.
    const next = commitPinnedSessionOperations([{ key: 'solo', pinned: false }])

    expect(next).toEqual([])
    expect(localStorage.getItem(PINNED_SESSION_ORDER_MANUAL_KEY)).toBeNull()
  })

  it('keeps the marker while the fallback still leaves a pin', () => {
    localStorage.setItem(PINNED_SESSION_ORDER_KEY, JSON.stringify(['a', 'b']))
    markPinnedSessionOrderManual()

    commitPinnedSessionOperations([{ key: 'b', pinned: false }])

    expect(readPinnedSessionOrderIsManual()).toBe(true)
  })
})
