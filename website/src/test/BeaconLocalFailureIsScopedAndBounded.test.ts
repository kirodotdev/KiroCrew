/**
 * The LOCAL claim-write failure tier is per composer and ages out.
 *
 * It was one window-wide flag, which broke in both directions. A sibling composer's
 * successful publish cleared it, erasing the only evidence of a composer whose claim AND
 * whose shared failure record had both failed to write — a fail-open that loses the
 * draft. And nothing cleared it after the failing composer retracted, because the clear
 * was only ever reachable from a successful write by the one composer that would never
 * publish again — so every slot in the window stayed shut for the page's life.
 *
 * Both halves are covered here with a healthy-storage control, because the read falls
 * back to a live probe write: with storage still broken the guard answers dirty for a
 * reason that has nothing to do with this tier, and the tests would pass vacuously.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  anyWindowSlotDirty,
  claimFailureKey,
  CLAIM_TTL_MS,
  publishSlotDirty,
  retractSlotDirty,
  __resetSlotDirtyForTests,
} from '../utils/slotDirtyBeacon'

const SLOT = 'chat-local-failure'
const OTHER_SLOT = 'chat-sibling'
const PINNED = new Date('2026-01-01T00:00:00.000Z')

describe('the local claim-write failure tier is scoped per composer and bounded', () => {
  let setItem: typeof localStorage.setItem

  beforeEach(() => {
    __resetSlotDirtyForTests()
    localStorage.clear()
    setItem = localStorage.setItem.bind(localStorage)
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(PINNED)
  })

  afterEach(() => {
    localStorage.setItem = setItem
    vi.useRealTimers()
    __resetSlotDirtyForTests()
  })

  it('control: healthy storage with no claim and no failure reports CLEAN', () => {
    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('keeps a failing composer dirty after a SIBLING publishes successfully', () => {
    // Both of the failing composer's writes are refused, so after storage recovers the
    // shared record carries nothing and this tier is the only evidence that remains.
    localStorage.setItem = () => {
      throw new Error('quota')
    }
    expect(publishSlotDirty('composer-a', SLOT, true)).toBe(false)

    localStorage.setItem = setItem
    expect(localStorage.getItem(claimFailureKey('composer-a'))).toBeNull()

    // The sibling is healthy and on a different slot, so its claim cannot answer for
    // this one. Its success is not evidence that composer-a recovered.
    expect(publishSlotDirty('composer-b', OTHER_SLOT, true)).toBe(true)

    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('clears only the composer that recovered, so a sibling failure still holds', () => {
    localStorage.setItem = () => {
      throw new Error('quota')
    }
    publishSlotDirty('composer-a', SLOT, true)
    publishSlotDirty('composer-b', OTHER_SLOT, true)

    localStorage.setItem = setItem
    expect(publishSlotDirty('composer-a', SLOT, true)).toBe(true)
    retractSlotDirty('composer-a')

    // composer-b never recovered, so the window still fails closed.
    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('still reports dirty immediately after a failing composer retracts', () => {
    // The existing contract: a removal proves nothing about whether writes land, so a
    // retraction must not be read as recovery.
    localStorage.setItem = () => {
      throw new Error('quota')
    }
    publishSlotDirty('composer-c', SLOT, true)

    localStorage.setItem = setItem
    retractSlotDirty('composer-c')

    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })

  it('releases a retracted composer\u2019s failure once it ages out', () => {
    // The composer is gone, so no future write of its own can ever clear it. Without a
    // bound the window pins every slot until the page reloads.
    localStorage.setItem = () => {
      throw new Error('quota')
    }
    publishSlotDirty('composer-c', SLOT, true)

    localStorage.setItem = setItem
    retractSlotDirty('composer-c')
    vi.setSystemTime(new Date(PINNED.getTime() + CLAIM_TTL_MS + 1_000))

    expect(anyWindowSlotDirty(SLOT)).toBe(false)
  })

  it('keeps a live failure dirty right up to the bound', () => {
    // Guards the comparison itself: an off-by-one that released early would drop
    // protection while the composer still holds the text.
    localStorage.setItem = () => {
      throw new Error('quota')
    }
    publishSlotDirty('composer-d', SLOT, true)

    localStorage.setItem = setItem
    vi.setSystemTime(new Date(PINNED.getTime() + CLAIM_TTL_MS - 1))

    expect(anyWindowSlotDirty(SLOT)).toBe(true)
  })
})
