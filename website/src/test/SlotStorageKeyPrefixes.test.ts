// Design review at 90103c2e9: the two modules each carry their own probe, memo and
// stale-time logic, and the prefixes must stay disjoint or one prefix "holds every slot
// shut". `anyWindowSlotDirty` fails closed on any key under its own prefix whose value is
// not a parseable claim, and a presence stamp is a bare number, so a rename that nested
// one prefix inside the other would pin every slot dirty forever rather than erroring.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  SLOT_DIRTY_KEY_PREFIX,
  __resetSlotDirtyForTests,
  anyWindowSlotDirty,
  claimFailureKey,
  publishSlotDirty,
} from '../utils/slotDirtyBeacon'
import {
  CLOSING_KEY_PREFIX,
  __resetClosingIntentForTests,
  anotherWindowHoldsComposer,
  publishClosingIntent,
} from '../utils/slotClosingIntent'

const PINNED = new Date('2026-01-01T00:00:00.000Z')
const PINNED_MS = PINNED.getTime()

describe('the two key namespaces cannot read each other', () => {
  beforeEach(() => {
    // Only `Date` is faked: every case here is synchronous, and a stamp written at the
    // pinned instant must read as fresh against both modules' stale windows.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(PINNED)
    localStorage.clear()
    __resetSlotDirtyForTests()
    __resetClosingIntentForTests()
  })
  afterEach(() => {
    localStorage.clear()
    __resetSlotDirtyForTests()
    __resetClosingIntentForTests()
    vi.useRealTimers()
  })

  it('pins the two prefixes as disjoint in BOTH directions', () => {
    expect(CLOSING_KEY_PREFIX.startsWith(SLOT_DIRTY_KEY_PREFIX)).toBe(false)
    expect(SLOT_DIRTY_KEY_PREFIX.startsWith(CLOSING_KEY_PREFIX)).toBe(false)
    expect(CLOSING_KEY_PREFIX).not.toBe(SLOT_DIRTY_KEY_PREFIX)
  })

  it('reads no slot as dirty from another window\u2019s handshake keys', () => {
    // Written raw, as a different window would leave them: a presence stamp is a bare
    // number and a veto-fail record has no slot field, so both are exactly the shapes
    // the dirty scan treats as unreadable-and-therefore-dirty.
    localStorage.setItem(`${CLOSING_KEY_PREFIX}present:composer-abc-1`, String(PINNED_MS))
    localStorage.setItem(`${CLOSING_KEY_PREFIX}veto-fail:nonce-1`, JSON.stringify({ f: PINNED_MS }))
    publishClosingIntent('slot-a')

    expect(anyWindowSlotDirty('slot-a')).toBe(false)
    expect(anyWindowSlotDirty('slot-b')).toBe(false)
  })

  it('counts no composer presence from a dirty claim or its failure record', () => {
    publishSlotDirty('composer-abc-1', 'slot-a', true)
    localStorage.setItem(claimFailureKey('composer-def-2'), JSON.stringify({ f: PINNED_MS }))

    expect(anotherWindowHoldsComposer()).toBe(false)
  })

  it('still reports a real claim as dirty while handshake keys sit beside it', () => {
    // The positive control: without it the two assertions above would also pass if the
    // dirty scan were broken outright and could never report anything.
    localStorage.setItem(`${CLOSING_KEY_PREFIX}present:composer-abc-1`, String(PINNED_MS))
    publishSlotDirty('composer-ghi-3', 'slot-a', true)

    expect(anyWindowSlotDirty('slot-a')).toBe(true)
  })
})
