// Feature: chat-virtualizer
//
// Property tests for the windowing decision logic used by useVirtualChat. The
// pure-function pieces (expandWindowUp / Down) are extracted so we can verify
// the properties without simulating the DOM.
//
// - Property 6: Bidirectional Window Expansion
//
// (The follow-output decision lives in FollowController.evaluateAutoPin and is
// covered against live scroll geometry in FollowController.test.ts.)

import { describe, it, expect } from 'vitest'
import * as fc from 'fast-check'
import {
  expandWindowUp,
  expandWindowDown,
} from '../hooks/virtualizer/WindowCalculator'

// Feature: chat-virtualizer, Property 6: Bidirectional Window Expansion
// **Validates: Requirements 6.1, 6.2**
describe('Property 6: Bidirectional Window Expansion', () => {
  it('expandWindowUp shrinks start when not at edge, and stays put when at 0', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 1000 }),  // start
        fc.integer({ min: 0, max: 1000 }),  // end relative offset
        fc.integer({ min: 1, max: 50 }),    // overscan
        (start, endOffset, overscan) => {
          const range = { start, end: start + endOffset }
          const next = expandWindowUp(range, overscan)
          if (start === 0) {
            expect(next).toBe(range) // identity preserved
          } else {
            expect(next.start).toBeLessThan(start)
            expect(next.start).toBeGreaterThanOrEqual(0)
            expect(next.end).toBe(range.end)
          }
        },
      ),
      { numRuns: 100 },
    )
  })

  it('expandWindowDown grows end when not at edge, and stays put when at itemCount', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 1000 }),  // itemCount
        fc.integer({ min: 0, max: 999 }),   // raw start
        fc.integer({ min: 0, max: 999 }),   // raw end offset
        fc.integer({ min: 1, max: 50 }),    // overscan
        (itemCount, rawStart, rawEndOffset, overscan) => {
          const start = Math.min(rawStart, itemCount)
          const end = Math.min(itemCount, start + rawEndOffset)
          const range = { start, end }
          const next = expandWindowDown(range, itemCount, overscan)
          if (end >= itemCount) {
            expect(next).toBe(range) // identity preserved
          } else {
            expect(next.end).toBeGreaterThan(end)
            expect(next.end).toBeLessThanOrEqual(itemCount)
            expect(next.start).toBe(range.start)
          }
        },
      ),
      { numRuns: 100 },
    )
  })

  it('repeated expandWindowUp eventually reaches start=0', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 200 }),
        fc.integer({ min: 1, max: 50 }),
        (start, overscan) => {
          let r = { start, end: start + 10 }
          let iterations = 0
          while (r.start > 0 && iterations < 1000) {
            r = expandWindowUp(r, overscan)
            iterations += 1
          }
          expect(r.start).toBe(0)
          // No more than ⌈start / overscan⌉ + 1 iterations.
          expect(iterations).toBeLessThanOrEqual(Math.ceil(start / overscan) + 1)
        },
      ),
      { numRuns: 50 },
    )
  })
})

// Feature: chat-virtualizer, Property 7 (Follow Output Correctness): the
// follow-output decision lives in FollowController.evaluateAutoPin and is
// covered against live scroll geometry in FollowController.test.ts.

describe('expandWindow targeted edges', () => {
  it('expandWindowUp with start=1, overscan=10 clamps to 0', () => {
    expect(expandWindowUp({ start: 1, end: 5 }, 10)).toEqual({ start: 0, end: 5 })
  })
  it('expandWindowDown with end=99, itemCount=100, overscan=10 clamps to 100', () => {
    expect(expandWindowDown({ start: 0, end: 99 }, 100, 10)).toEqual({ start: 0, end: 100 })
  })
  it('expandWindowUp with overscan=0 is a no-op', () => {
    const r = { start: 5, end: 10 }
    // Implementation returns a new {start:5, end:10} object, not identity.
    // Just verify the values are unchanged.
    expect(expandWindowUp(r, 0)).toEqual(r)
  })
})
