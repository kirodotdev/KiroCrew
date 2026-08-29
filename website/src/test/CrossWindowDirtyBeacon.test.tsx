import { anyWindowClaimLapsed, UNRECOVERABLE_CLAIM_TTL_MS, LAPSED_CLAIM_GRACE_MS, SLOT_DIRTY_REFRESH_MS } from '../utils/slotDirtyBeacon'
/**
 * A popout's unsent work must block a close fired in ANOTHER window.
 *
 * `slotComposerRegistry` is a module Map, so it knows only the composers mounted in its
 * own window, and the persisted drafts lag by a debounce. Between a keystroke in the
 * popout and that flush, another window read clean state and deleted the slot.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'

import { slotHasUnsentWork, registerSlotComposer, nextComposerId } from '../utils/slotComposerRegistry'
import {
  publishSlotDirty,
  retractSlotDirty,
  anyWindowSlotDirty,
  __resetSlotDirtyForTests,
} from '../utils/slotDirtyBeacon'
import { __resetForTests as resetDrafts } from '../utils/chatDrafts'

const SLOT = 'slot-cross-window'

describe('cross-window unsent work reaches the close gate', () => {
  beforeEach(() => {
    __resetSlotDirtyForTests()
    resetDrafts()
    localStorage.clear()
  })

  afterEach(() => {
    __resetSlotDirtyForTests()
    resetDrafts()
    localStorage.clear()
  })

  it('blocks while another window claims the slot dirty, with NOTHING persisted yet', () => {
    // Positive control: the gate must read clean first, or the assertion below could
    // pass on residue from a sibling test.
    expect(slotHasUnsentWork(SLOT)).toBe(false)

    // The popout's window: a claim published on its first keystroke, before any flush.
    publishSlotDirty('composer-popout', SLOT, true, true)

    // This window has no composer for the slot and no persisted draft — the debounce
    // has not fired, so persistence alone still reads clean.
    expect(slotHasUnsentWork(SLOT)).toBe(true)
  })

  it('releases the slot when that window retracts its claim', () => {
    publishSlotDirty('composer-popout', SLOT, true, true)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)

    retractSlotDirty('composer-popout')

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    expect(slotHasUnsentWork(SLOT)).toBe(false)
  })

  it('moves the claim when a mounted host changes slot', () => {
    publishSlotDirty('composer-a', SLOT, true, true)
    // Same composer, now bound elsewhere: leaving the old claim would block that slot
    // forever, since no unmount ever comes to clear it.
    publishSlotDirty('composer-a', 'slot-other', true, true)

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    expect(anyWindowSlotDirty('slot-other')).toBe(true)
  })

  it('still answers from a mounted composer when the beacon is empty', () => {
    const release = registerSlotComposer(nextComposerId(), {
      getSlot: () => SLOT,
      hasWork: () => true,
    })
    try {
      expect(anyWindowSlotDirty(SLOT)).toBe(false)
      expect(slotHasUnsentWork(SLOT)).toBe(true)
    } finally {
      release()
    }
  })
})

describe('a lapsed unrecoverable claim is bounded, not permanent', () => {
  it('reads unverifiable just past the TTL, so a frozen owner is still protected', () => {
    const age = Date.now() - (UNRECOVERABLE_CLAIM_TTL_MS + 5_000)
    localStorage.setItem('mc-slot-dirty:c1', JSON.stringify({ s: 'slot-x', t: age, u: 1 }))
    expect(anyWindowClaimLapsed('slot-x')).toBe(true)
  })

  it('stops answering once the grace is spent, and RECLAIMS the key', () => {
    const age = Date.now() - (UNRECOVERABLE_CLAIM_TTL_MS + LAPSED_CLAIM_GRACE_MS + 5_000)
    localStorage.setItem('mc-slot-dirty:c2', JSON.stringify({ s: 'slot-y', t: age, u: 1 }))
    expect(anyWindowClaimLapsed('slot-y')).toBe(false)
    // Left in place it would be re-scanned forever and nothing else can clear it.
    expect(localStorage.getItem('mc-slot-dirty:c2')).toBeNull()
  })

  it('leaves a RECOVERABLE claim alone, since the short TTL already answers for it', () => {
    const age = Date.now() - (UNRECOVERABLE_CLAIM_TTL_MS + LAPSED_CLAIM_GRACE_MS + 5_000)
    localStorage.setItem('mc-slot-dirty:c3', JSON.stringify({ s: 'slot-z', t: age }))
    expect(anyWindowClaimLapsed('slot-z')).toBe(false)
    expect(localStorage.getItem('mc-slot-dirty:c3')).not.toBeNull()
  })

  it('keeps the lapsed grace REFRESH-scale, so the bound is one tier not two', () => {
    // A TTL-scale grace made the real worst case 24h while the docs said 12h.
    expect(LAPSED_CLAIM_GRACE_MS).toBeLessThan(UNRECOVERABLE_CLAIM_TTL_MS / 100)
    const total = UNRECOVERABLE_CLAIM_TTL_MS + LAPSED_CLAIM_GRACE_MS
    expect(total).toBeLessThan(UNRECOVERABLE_CLAIM_TTL_MS * 1.01)
  })

  it('still outlasts several missed refresh beats, so a slow window is not evicted', () => {
    expect(LAPSED_CLAIM_GRACE_MS).toBeGreaterThan(SLOT_DIRTY_REFRESH_MS * 2)
  })
})
