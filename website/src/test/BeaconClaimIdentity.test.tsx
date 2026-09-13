/**
 * The cross-window beacon must survive a second window and a crashed one.
 *
 * Three distinct failures, all in the direction of LOSING a draft or blocking forever:
 *  - ids were only process-unique, so two windows both minted `composer-1` and one
 *    window's clean publish retracted the other's live claim;
 *  - a claim was published on the dirty transition alone, so a composer that changed
 *    slots while staying dirty left the old slot claimed and the new one reading clean;
 *  - a claim never expired, so a crashed window blocked its slot's close permanently.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

import { useSlotComposerRegistration } from '../hooks/useSlotComposerRegistration'
import { nextComposerId } from '../utils/slotComposerRegistry'
import {
  publishSlotDirty,
  anyWindowSlotDirty,
  __resetSlotDirtyForTests,
  CLAIM_TTL_MS,
} from '../utils/slotDirtyBeacon'

describe('beacon claims are window-scoped and expiring', () => {
  beforeEach(() => {
    __resetSlotDirtyForTests()
  })

  afterEach(() => {
    vi.useRealTimers()
    __resetSlotDirtyForTests()
  })

  it('ids from two windows do NOT collide', () => {
    // One module instance cannot mint another window's id, so the window half of the id
    // is what has to differ; two ids from here share it and must still be distinct.
    const a = nextComposerId()
    const b = nextComposerId()
    expect(a).not.toBe(b)
    // The window tag must be present, or a sibling window's counter reproduces this id.
    expect(a).toMatch(/^composer-.+-\d+$/)
    const tag = a.slice('composer-'.length, a.lastIndexOf('-'))
    expect(tag.length).toBeGreaterThan(8)
  })

  it("a second window's clean publish does not retract the first's live claim", () => {
    publishSlotDirty('composer-winA-1', 'slot-x', true, true)
    // Window B mints its own id and publishes CLEAN. Under the old process-unique ids
    // both were `composer-1`, so this call erased window A's claim.
    publishSlotDirty('composer-winB-1', 'slot-y', false, true)

    expect(anyWindowSlotDirty('slot-x')).toBe(true)
  })

  it('a claim FOLLOWS its composer to a new slot', () => {
    publishSlotDirty('composer-winA-1', 'slot-old', true, true)
    publishSlotDirty('composer-winA-1', 'slot-new', true, true)

    expect(anyWindowSlotDirty('slot-new')).toBe(true)
    // And the old slot is released, or its close is blocked by a draft that moved away.
    expect(anyWindowSlotDirty('slot-old')).toBe(false)
  })

  it('the HOOK republishes when a mounted dirty composer changes slots', () => {
    // The blocker lives in the effect's dependencies, not in publishSlotDirty: keyed on
    // `[hasUnsentWork]` alone the effect never re-fired, so the new slot read clean.
    const { rerender } = renderHook(
      ({ slot }: { slot: string }) => useSlotComposerRegistration(() => slot, true),
      { initialProps: { slot: 'slot-first' } },
    )
    expect(anyWindowSlotDirty('slot-first')).toBe(true)

    rerender({ slot: 'slot-second' })

    expect(anyWindowSlotDirty('slot-second')).toBe(true)
    expect(anyWindowSlotDirty('slot-first')).toBe(false)
  })

  it('a crashed window stops blocking once its claim goes stale', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    publishSlotDirty('composer-crashed-1', 'slot-z', true, true)
    expect(anyWindowSlotDirty('slot-z')).toBe(true)

    vi.setSystemTime(new Date(Date.now() + CLAIM_TTL_MS + 1_000))
    expect(anyWindowSlotDirty('slot-z')).toBe(false)
  })

  it('a claim REFRESHED before the bound keeps protecting its slot', () => {
    // The negative control for the test above: expiry must not reap a live window, which
    // is what a bare TTL with no refresh would do to a draft left sitting.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    publishSlotDirty('composer-live-1', 'slot-w', true, true)

    vi.setSystemTime(new Date(Date.now() + CLAIM_TTL_MS - 5_000))
    publishSlotDirty('composer-live-1', 'slot-w', true, true)
    vi.setSystemTime(new Date(Date.now() + CLAIM_TTL_MS - 5_000))

    expect(anyWindowSlotDirty('slot-w')).toBe(true)
  })
})
