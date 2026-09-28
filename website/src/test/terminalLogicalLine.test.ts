import { describe, it, expect } from 'vitest'
import { logicalLineBounds, logicalLineTop } from '../utils/terminalLogicalLine'

/**
 * The single source of truth for the wrapped logical-line walk, shared by the
 * staged Select key and the touch range-select hook. `wrapped` is the set of
 * physical rows flagged isWrapped (continuation rows).
 */
function pred(wrapped: number[]) {
  const s = new Set(wrapped)
  return (r: number) => s.has(r)
}

describe('terminalLogicalLine', () => {
  it('returns the row itself for an unwrapped line', () => {
    expect(logicalLineBounds(2, 5, pred([]))).toEqual([2, 2])
    expect(logicalLineTop(2, pred([]))).toBe(2)
  })

  it('walks UP through continuation rows to the logical start', () => {
    // Rows 1,2 are continuations of row 0 → logical line 0..2.
    const isWrapped = pred([1, 2])
    expect(logicalLineTop(2, isWrapped)).toBe(0)
    expect(logicalLineBounds(2, 5, isWrapped)).toEqual([0, 2])
  })

  it('walks DOWN through continuation rows to the logical end', () => {
    // Row 1 starts a logical line; rows 2,3 continue it → 1..3.
    const isWrapped = pred([2, 3])
    expect(logicalLineBounds(1, 5, isWrapped)).toEqual([1, 3])
  })

  it('expands from a middle continuation row in both directions', () => {
    // Logical line spans rows 0..3 (1,2,3 wrapped); asked from row 2.
    const isWrapped = pred([1, 2, 3])
    expect(logicalLineBounds(2, 5, isWrapped)).toEqual([0, 3])
  })

  it('caps the downward walk at the buffer length', () => {
    // Row 3 would continue but length is 3 (rows 0..2 only).
    const isWrapped = pred([1, 2, 3])
    expect(logicalLineBounds(1, 3, isWrapped)).toEqual([0, 2])
  })

  it('does not walk above row 0', () => {
    const isWrapped = pred([0, 1]) // row 0 flagged (defensive) — walk still stops at 0
    expect(logicalLineTop(1, isWrapped)).toBe(0)
  })
})
