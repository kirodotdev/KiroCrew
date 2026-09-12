/**
 * View-state store — the pure surface.
 *
 * One export left to test here. The key derivation, the record decisions and the write
 * policy are all internal to the module now (none had a non-test consumer), so they are
 * asserted through the hook in `viewState.hook.test.tsx` -- against literal key strings
 * rather than by recomputing the key with the same function under test, which is a
 * stronger statement about the on-disk contract than a round trip through the derivation
 * could be.
 */
import { describe, it, expect } from 'vitest'
import { isViewString } from '../app-sdk/viewState'

describe('isViewString', () => {
  it('accepts any string', () => {
    expect(isViewString('docs')).toBe(true)
    expect(isViewString('')).toBe(true)
    expect(isViewString('docs/reports/q3')).toBe(true)
  })

  it.each([
    ['a number', 42],
    ['null', null],
    ['undefined', undefined],
    ['an object', {}],
    ['an array', []],
    ['a boolean', true],
  ])('refuses %s', (_label, value) => {
    // The guard runs in both directions -- it filters what may be written and validates
    // what is read back -- so a value it wrongly admits would be persisted AND restored.
    expect(isViewString(value)).toBe(false)
  })
})
