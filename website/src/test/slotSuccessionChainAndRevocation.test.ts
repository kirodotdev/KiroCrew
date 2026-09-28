import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'

import {
  clearSlotSuccession,
  forgetSlotSuccession,
  recordSlotSuccession,
  resolveSlotSuccession,
} from '../utils/slotSuccession'

const SUCCESSION = readFileSync(join(__dirname, '..', 'utils', 'slotSuccession.ts'), 'utf-8')

describe('a reactivated slot is not redirected into its deleted successor', () => {
  beforeEach(() => { clearSlotSuccession() })

  it('resolves to itself once the revoke has run, not to the gone successor', () => {
    recordSlotSuccession('slot-A', 'slot-B')
    // Pre-revoke: this is the misroute — new work in A lands in the deleted B.
    expect(resolveSlotSuccession('slot-A')).toBe('slot-B')
    // What activation does. B is gone; A is on screen and owns its own uploads again.
    forgetSlotSuccession('slot-A')
    expect(resolveSlotSuccession('slot-A')).toBe('slot-A')
  })

  it('still retargets an in-flight completion across a switch it did not activate', () => {
    recordSlotSuccession('slot-A', 'slot-B')
    forgetSlotSuccession('slot-B')
    expect(resolveSlotSuccession('slot-A')).toBe('slot-B')
  })
})

describe('the resolver walks a chain of replacements to the slot still alive', () => {
  beforeEach(() => { clearSlotSuccession() })

  it('resolves a LONG chain instead of dropping the completion', () => {
    for (let i = 0; i < 20; i++) recordSlotSuccession(`hop-${i}`, `hop-${i + 1}`)
    // 20 mode switches inside one upload's completion window used to exceed the walk bound,
    // so the resolver refused and the attachment was uploaded, charged and unreachable.
    expect(resolveSlotSuccession('hop-0')).toBe('hop-20')
  })

  it('still terminates on a CYCLE rather than spinning', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('b', 'c')
    recordSlotSuccession('c', 'a')
    // Raising the bound without the `seen` guard would turn a dropped completion into a hang.
    expect(['a', 'b', 'c']).toContain(resolveSlotSuccession('a'))
  })

  it('walks the whole LIVE table, since a fully-referenced one may exceed the cap', () => {
    // A fixed hop limit cannot be caught behaviourally: any chain short enough to keep
    // every edge live is also short enough to pass under a generous constant.
    expect(SUCCESSION).toContain('hop < successors.size')
    expect(SUCCESSION).not.toContain('MAX_CHAIN')
    expect(SUCCESSION).toContain('seen.has(next)')
  })
})

describe('a succession is revoked when the deletion it anticipated fails', () => {
  beforeEach(() => clearSlotSuccession())

  it('stops standing in for a slot that survived', () => {
    recordSlotSuccession('slot-old', 'slot-new')
    expect(resolveSlotSuccession('slot-old')).toBe('slot-new')
    forgetSlotSuccession('slot-old')
    // The old slot is alive again, so its own uploads must land on it.
    expect(resolveSlotSuccession('slot-old')).toBe('slot-old')
  })

  it('leaves an unrelated succession alone', () => {
    recordSlotSuccession('a', 'b')
    recordSlotSuccession('c', 'd')
    forgetSlotSuccession('a')
    expect(resolveSlotSuccession('c')).toBe('d')
  })

  it('ignores an empty slot key', () => {
    recordSlotSuccession('a', 'b')
    forgetSlotSuccession('')
    expect(resolveSlotSuccession('a')).toBe('b')
  })
})
