/**
 * A claim that FAILED in another window must not read as an empty composer.
 *
 * `claimWriteFailed` is module-local, so it only ever fails closed for the window that
 * hit the failure. The destructive case is the opposite one: window B could not persist
 * its claim while window A's storage is healthy, so A's flag is false, A's probe
 * succeeds, and A reports a clean slot over B's live draft.
 *
 * Two shapes of the same class are covered: a failure another window recorded, and a
 * claim that is PRESENT but unreadable — a truncated write is how a quota failure
 * actually manifests, and it cannot even be attributed to a slot.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  anyWindowSlotDirty,
  claimFailureKey,
  CLAIM_TTL_MS,
  publishSlotDirty,
  retractSlotDirty,
  SLOT_DIRTY_KEY_PREFIX,
  __resetSlotDirtyForTests,
} from '../utils/slotDirtyBeacon'

const SLOT = 'chat-99'

describe('a failed claim in another window is not evidence of an empty composer', () => {
  beforeEach(() => {
    __resetSlotDirtyForTests()
    localStorage.clear()
    vi.useRealTimers()
  })

  it('reports dirty when ANOTHER window recorded a claim-write failure', () => {
    // The blocking case. This window is healthy — no local flag, working storage — so
    // every local signal says clean while the other window's draft is live.
    localStorage.setItem(claimFailureKey('composer-b'), JSON.stringify({ f: Date.now() }))
    __resetSlotDirtyForTests_keepFailureKey()

    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('reports dirty for a claim that is PRESENT but unreadable', () => {
    // A truncated write cannot be attributed to a slot, so it cannot clear any slot.
    localStorage.setItem(`${SLOT_DIRTY_KEY_PREFIX}composer-truncated`, '{"s":"chat-9')

    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('does NOT let one composer\u2019s successful claim clear another\u2019s failure', () => {
    // GPT's chain: window B cannot persist, then window A publishes successfully, and a
    // single shared record let A's success erase the only evidence of B's unsent work.
    localStorage.setItem(claimFailureKey('composer-b'), JSON.stringify({ f: Date.now() }))
    publishSlotDirty('composer-a', 'chat-other', true, true)

    expect(localStorage.getItem(claimFailureKey('composer-b'))).not.toBeNull()
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('clears ONLY the recovering composer\u2019s own failure', () => {
    // The other half: recovery must still be expressible, or the record never clears and
    // every slot fails closed forever.
    localStorage.setItem(claimFailureKey('composer-a'), JSON.stringify({ f: Date.now() }))
    publishSlotDirty('composer-a', 'chat-other', true, true)

    expect(localStorage.getItem(claimFailureKey('composer-a'))).toBeNull()
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('drops a composer\u2019s failure record when it retracts', () => {
    // An unmounted composer holds no work, so its failure must not outlive it.
    localStorage.setItem(claimFailureKey('composer-b'), JSON.stringify({ f: Date.now() }))
    retractSlotDirty('composer-b')

    expect(localStorage.getItem(claimFailureKey('composer-b'))).toBeNull()
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('releases a recorded failure once it ages out', () => {
    // Bounded, or a window that died mid-failure fails closed forever.
    const now = Date.now()
    localStorage.setItem(claimFailureKey('composer-b'), JSON.stringify({ f: now }))
    vi.useFakeTimers()
    vi.setSystemTime(new Date(now + CLAIM_TTL_MS + 1_000))

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
    vi.useRealTimers()
  })

  it('clears the recorded failure once a claim write succeeds again', () => {
    localStorage.setItem(claimFailureKey('composer-recovered'), JSON.stringify({ f: Date.now() }))
    publishSlotDirty('composer-recovered', 'chat-other', true, true)

    expect(localStorage.getItem(claimFailureKey('composer-recovered'))).toBeNull()
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('does NOT fail closed on the probe key the writability check leaves behind', () => {
    // If the probe's removal ever fails, the leftover must stay inert: reading it as an
    // unattributable claim would pin every slot, turning fail-closed into a deadlock.
    localStorage.setItem(SLOT_DIRTY_KEY_PREFIX, SLOT_DIRTY_KEY_PREFIX)

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('still reports clean with healthy storage and no claims at all', () => {
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })
})

/** Reset the module's own state WITHOUT clearing keys another window wrote. */
function __resetSlotDirtyForTests_keepFailureKey(): void {
  const kept = localStorage.getItem(claimFailureKey('composer-b'))
  __resetSlotDirtyForTests()
  if (kept !== null) localStorage.setItem(claimFailureKey('composer-b'), kept)
}
