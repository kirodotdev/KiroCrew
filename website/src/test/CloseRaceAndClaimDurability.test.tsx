/**
 * How long a cross-window dirty claim lives, and why that depends on the work.
 *
 * A claim ages out so a CRASHED window stops blocking closes forever. Where the work is
 * text or files that answer works because a persisted store still answers afterwards —
 * but a pending knowledge selection, an in-flight upload and a live voice capture are
 * written down nowhere, so for those the claim IS the only record.
 *
 * A FROZEN window is not a dead one: a browser stops its timers, so it misses the 25s
 * re-stamp while still holding the work on screen. Expiring on that scale therefore
 * handed another window a clean slot and destroyed the work permanently. Those claims get
 * a long bound instead — bounded, not exempt, because a window that died mid-upload must
 * eventually let the slot go.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  anyWindowSlotDirty,
  CLAIM_TTL_MS,
  publishSlotDirty,
  SLOT_DIRTY_KEY_PREFIX,
  UNRECOVERABLE_CLAIM_TTL_MS,
  __resetSlotDirtyForTests,
} from '../utils/slotDirtyBeacon'
import { slotHasUnsentWork } from '../utils/slotComposerRegistry'

const SLOT = 'chat-1'

describe('a dirty claim ages out, and only the owning window clears it', () => {
  beforeEach(() => {
    __resetSlotDirtyForTests()
    vi.useRealTimers()
  })

  it('ages out a claim, so a crashed window stops forcing confirms forever', () => {
    const now = Date.now()
    publishSlotDirty('composer-a', SLOT, true, true)
    vi.setSystemTime(new Date(now + CLAIM_TTL_MS + 1_000))
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    vi.useRealTimers()
  })

  it('keeps a claim inside the TTL, so a live window is not expired mid-draft', () => {
    // Paired with the case above: without this, a beacon that expired everything
    // instantly would pass that one while protecting nobody.
    const now = Date.now()
    publishSlotDirty('composer-f', SLOT, true, true)
    vi.setSystemTime(new Date(now + CLAIM_TTL_MS - 1_000))
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
    vi.useRealTimers()
  })

  it('honours a claim another window wrote', () => {
    const claim = JSON.stringify({ s: SLOT, t: Date.now() })
    localStorage.setItem(`${SLOT_DIRTY_KEY_PREFIX}composer-c`, claim)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('ignores a claim naming a DIFFERENT slot', () => {
    publishSlotDirty('composer-g', 'chat-other', true, true)
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('KEEPS a claim whose work no store can answer for, past the refresh TTL', () => {
    // The cross-window loss, reproduced: before this, the lapsed claim reported a clean
    // slot and a close from another window destroyed the work permanently.
    const now = Date.now()
    publishSlotDirty('composer-frozen', SLOT, true, false, true)
    vi.setSystemTime(new Date(now + CLAIM_TTL_MS * 4))

    expect(anyWindowSlotDirty(SLOT)).toBe(true)
    expect(slotHasUnsentWork(SLOT)).toBe(true)
    vi.useRealTimers()
  })

  it('RELEASES that claim past its own outer bound, so a dead window lets go', () => {
    // Paired with the case above: protection cannot be unbounded, or a window that died
    // mid-upload holds the slot shut forever with nothing on screen to explain why.
    const now = Date.now()
    publishSlotDirty('composer-abandoned', SLOT, true, false, true)
    vi.setSystemTime(new Date(now + UNRECOVERABLE_CLAIM_TTL_MS + 1_000))

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    vi.useRealTimers()
  })

  it('a RECOVERABLE claim still ages out on the short TTL', () => {
    // Discriminates the two horizons: holding a text claim for hours would confirm over a
    // draft the user can already see and clear.
    const now = Date.now()
    publishSlotDirty('composer-text', SLOT, true, true, true)
    vi.setSystemTime(new Date(now + CLAIM_TTL_MS + 1_000))

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    vi.useRealTimers()
  })

  it('a claim another window wrote with no recoverability field reads as recoverable', () => {
    // Forward compatibility in the safe direction for the COMMON case: an older build's
    // claim must age out normally rather than pin the slot for hours.
    const now = Date.now()
    localStorage.setItem(
      `${SLOT_DIRTY_KEY_PREFIX}composer-oldbuild`,
      JSON.stringify({ s: SLOT, t: now }),
    )
    vi.setSystemTime(new Date(now + CLAIM_TTL_MS + 1_000))
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    vi.useRealTimers()
  })

  it('retracting clears the claim, so it is not a permanent lock', () => {
    publishSlotDirty('composer-d', SLOT, true, true)
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
    publishSlotDirty('composer-d', null, false, true)
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })
})
