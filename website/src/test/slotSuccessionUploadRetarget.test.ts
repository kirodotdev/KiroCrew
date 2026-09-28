import { describe, expect, it, beforeEach } from 'vitest'

import { clearSlotSuccession, pinSlotSuccession, recordSlotSuccession, releaseSlotSuccession, resolveSlotSuccession } from '../utils/slotSuccession'
import { fileLandingSlot } from '../utils/uploadRouting'

describe('an upload in flight across a mode switch', () => {
  beforeEach(() => clearSlotSuccession())

  it('lands in the replacement slot, not the deleted one', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    expect(resolveSlotSuccession('slot-old')).toBe('slot-new')
  })

  it('routes the completion to the replacement draft bucket', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    // The user has since moved on to a third slot, so the file goes to a draft, not the composer.
    expect(fileLandingSlot(resolveSlotSuccession('slot-old'), 'slot-other')).toEqual({
      target: 'draft',
      slot: 'slot-new',
    })
  })

  it('routes into the live composer when the replacement is the slot on screen', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    expect(fileLandingSlot(resolveSlotSuccession('slot-old'), 'slot-new')).toEqual({ target: 'pending' })
  })

  it('follows a chain, because two switches in a row stale the first successor', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('b', 'c')
    expect(resolveSlotSuccession('a')).toBe('c')
  })

  it('terminates on a cycle instead of spinning', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('b', 'a')
    expect(['a', 'b']).toContain(resolveSlotSuccession('a'))
  })

  it('passes absence through so the router can still drop it', () => {
    expect(resolveSlotSuccession(null)).toBeNull()
    expect(resolveSlotSuccession(undefined)).toBeUndefined()
    expect(fileLandingSlot(resolveSlotSuccession(null), 'slot-a')).toEqual({ target: 'drop' })
  })

  it('leaves an unreplaced slot alone', () => {
    recordSlotSuccession('a', 'b')
    expect(resolveSlotSuccession('untouched')).toBe('untouched')
  })

  it('ignores a self-succession rather than recording a one-hop cycle', () => {
    recordSlotSuccession('a', 'a')
    expect(resolveSlotSuccession('a')).toBe('a')
  })

  it('bounds the table so a long-lived tab cannot accumulate slots', () => {
    for (let i = 0; i < 200; i++) recordSlotSuccession(`from-${i}`, `to-${i}`)
    expect(resolveSlotSuccession('from-199')).toBe('to-199')
    // The earliest entries were evicted, so the oldest key resolves to itself again.
    expect(resolveSlotSuccession('from-0')).toBe('from-0')
  })

  it('keeps a PINNED mapping past the eviction cap, and evicts it once released', () => {
    recordSlotSuccession('pending-upload', 'live-slot')
    pinSlotSuccession('pending-upload')
    for (let i = 0; i < 200; i++) recordSlotSuccession(`churn-${i}`, `churn-to-${i}`)
    expect(resolveSlotSuccession('pending-upload')).toBe('live-slot')
    releaseSlotSuccession('pending-upload')
    for (let i = 0; i < 200; i++) recordSlotSuccession(`later-${i}`, `later-to-${i}`)
    expect(resolveSlotSuccession('pending-upload')).toBe('pending-upload')
  })

  it('protects the whole CHAIN a pinned slot walks, not just its own edge', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('b', 'c')
    pinSlotSuccession('a')
    for (let i = 0; i < 200; i++) recordSlotSuccession(`churn-${i}`, `churn-to-${i}`)
    expect(resolveSlotSuccession('a')).toBe('c')
  })

  it('needs one release per pin, so overlapping uploads cannot free each other', () => {
    recordSlotSuccession('shared', 'live')
    pinSlotSuccession('shared')
    pinSlotSuccession('shared')
    releaseSlotSuccession('shared')
    for (let i = 0; i < 200; i++) recordSlotSuccession(`churn-${i}`, `churn-to-${i}`)
    expect(resolveSlotSuccession('shared')).toBe('live')
  })

  it('still evicts an UNPINNED mapping, so the bound is not simply removed', () => {
    recordSlotSuccession('unpinned', 'gone')
    for (let i = 0; i < 200; i++) recordSlotSuccession(`churn-${i}`, `churn-to-${i}`)
    expect(resolveSlotSuccession('unpinned')).toBe('unpinned')
  })
})
