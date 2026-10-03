/**
 * The cross-window guard FAILS CLOSED when storage will not hold a claim.
 *
 * A claim that never reached storage is invisible to every other window, so a close
 * fired elsewhere read a clean slot and destroyed the draft — the guard's own answer
 * turned a storage failure into data loss. Reporting "dirty" instead costs a confirm
 * the user can dismiss; the alternative costs the only copy of their text.
 *
 * Quota is per-origin, which is what makes a LOCAL write failure usable evidence about
 * other windows: a write this window cannot make is one no window could.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'

import {
  anyWindowSlotDirty,
  publishSlotDirty,
  __resetSlotDirtyForTests,
} from '../utils/slotDirtyBeacon'

const SLOT = 'slot-fail-closed'

describe('the dirty guard fails closed on a storage failure', () => {
  let setItem: typeof localStorage.setItem

  beforeEach(() => {
    __resetSlotDirtyForTests()
    setItem = localStorage.setItem.bind(localStorage)
  })

  afterEach(() => {
    localStorage.setItem = setItem
    __resetSlotDirtyForTests()
  })

  it('control: healthy storage with no claim reports CLEAN', () => {
    // Without this the suite would pass against a guard hard-wired to true, which would
    // block every close instead of guarding one.
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('reports DIRTY once this window could not persist its own claim', () => {
    localStorage.setItem = () => { throw new Error('quota') }
    expect(publishSlotDirty('composer-a', SLOT, true)).toBe(false)

    // Storage healthy again, and no claim exists for this slot: the only thing that can
    // carry the answer is the recorded write failure.
    localStorage.setItem = setItem
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('reports DIRTY when storage refuses writes at guard time', () => {
    // No publish at all — this is the window that did NOT type, asking about a slot whose
    // other window may hold a draft it was unable to announce.
    localStorage.setItem = () => { throw new Error('quota') }
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('reports DIRTY when the claim keys cannot be enumerated', () => {
    // Seeded on ANOTHER slot so a healthy read would answer false: without it the store
    // is empty, the enumeration loop never runs, and the case could not fail.
    publishSlotDirty('composer-elsewhere', 'slot-other', true)
    expect(anyWindowSlotDirty(SLOT)).toBe(false)

    const key = localStorage.key.bind(localStorage)
    try {
      localStorage.key = () => { throw new Error('storage disabled') }
      expect(anyWindowSlotDirty(SLOT)).toBe(true)
    } finally {
      localStorage.key = key
    }
  })

  it('returns to CLEAN once a claim write succeeds again', () => {
    // The flag must not latch: a transient quota hit that later clears would otherwise
    // leave every close on this origin confirming forever.
    localStorage.setItem = () => { throw new Error('quota') }
    publishSlotDirty('composer-b', SLOT, true)
    localStorage.setItem = setItem
    expect(anyWindowSlotDirty(SLOT)).toBe(true)

    expect(publishSlotDirty('composer-b', SLOT, true)).toBe(true)
    publishSlotDirty('composer-b', null, false)
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('a retraction does not clear the failure, because a removal proves no write', () => {
    localStorage.setItem = () => { throw new Error('quota') }
    publishSlotDirty('composer-c', SLOT, true)
    localStorage.setItem = setItem
    publishSlotDirty('composer-c', null, false)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })
})
