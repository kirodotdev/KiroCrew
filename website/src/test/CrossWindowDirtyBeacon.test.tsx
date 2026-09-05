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
    publishSlotDirty('composer-popout', SLOT, true)

    // This window has no composer for the slot and no persisted draft — the debounce
    // has not fired, so persistence alone still reads clean.
    expect(slotHasUnsentWork(SLOT)).toBe(true)
  })

  it('releases the slot when that window retracts its claim', () => {
    publishSlotDirty('composer-popout', SLOT, true)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)

    retractSlotDirty('composer-popout')

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    expect(slotHasUnsentWork(SLOT)).toBe(false)
  })

  it('moves the claim when a mounted host changes slot', () => {
    publishSlotDirty('composer-a', SLOT, true)
    // Same composer, now bound elsewhere: leaving the old claim would block that slot
    // forever, since no unmount ever comes to clear it.
    publishSlotDirty('composer-a', 'slot-other', true)

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
