/**
 * The in-flight record must close, and two toggles sharing one batch is where it did not.
 *
 * A counter incremented per mutation but cleared per BATCH drifts the moment a second toggle joins
 * an in-flight batch: one clear, two increments, and the residue silences every later zero-pin
 * settle for the tab's lifetime -- so a stale marker survives emptying the pinned set and the next
 * pin set freezes in pin-toggle order, which is the defect this change exists to remove. Deriving
 * the record from the batch itself removes the drift by construction, so these cases pin the
 * DERIVED property: whatever the batch holds is what the store-side reader sees, and an ended
 * batch reads empty.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  pinMutationsAreInFlight,
  publishPinMutationKeysInFlight,
  readPinMutationKeysInFlight,
} from '../utils/pinMutationsInFlight'

describe('the in-flight pin record', () => {
  beforeEach(() => publishPinMutationKeysInFlight([]))

  it('closes once, however many toggles shared the batch', () => {
    // Two overlapping toggles join one batch, so the batch reports both keys.
    publishPinMutationKeysInFlight(['a'])
    publishPinMutationKeysInFlight(['a', 'b'])
    expect(readPinMutationKeysInFlight()).toEqual(['a', 'b'])

    // The batch settles ONCE. A per-mutation tally would still be holding a residue here.
    publishPinMutationKeysInFlight([])

    expect(pinMutationsAreInFlight()).toBe(false)
    expect(readPinMutationKeysInFlight()).toEqual([])
  })

  it('reports exactly what the batch holds, never a count of its own', () => {
    publishPinMutationKeysInFlight(['only'])
    expect(pinMutationsAreInFlight()).toBe(true)
    expect(readPinMutationKeysInFlight()).toEqual(['only'])
  })

  it('hands back a copy, so a caller cannot mutate the record', () => {
    publishPinMutationKeysInFlight(['a'])
    readPinMutationKeysInFlight().push('forged')
    expect(readPinMutationKeysInFlight()).toEqual(['a'])
  })
})

