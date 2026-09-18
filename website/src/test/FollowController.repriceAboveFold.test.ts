/**
 * GUARD for kirodotdev/KiroCrew#10810: a reader parked in the MIDDLE of a
 * streaming reply is walked down the message -- seen as the text sliding UP
 * out from under them -- by one token's height per tick.
 *
 * The scroll inspector attributed every write to `abovefold`, i.e. this
 * predicate. The streaming row grows by APPENDING at its bottom; once the
 * reader scrolls far enough into it that its top edge is above the fold, the
 * row STRADDLES the fold and the predicate credited the whole growth as
 * "above the reader" -- but the new pixels are below their eye line and
 * nothing visible moved. With native `overflow-anchor: auto` also holding the
 * viewport, the reader was moved by the growth while the browser moved nothing:
 * `Δtop == Δh` on every tick. At the message HEAD the row's top is inside the
 * fold, so the same reader held perfectly -- which is why the report said
 * "holds at the top, drifts in the middle".
 *
 * The straddling-REPRICE case stays compensated: that fix (a twelve-step walk
 * with four straddlers shrinking 12-24px each) is real and this must not undo
 * it. `appendsAtBottom` is what tells the two apart.
 */
import { describe, it, expect } from 'vitest'
import { repriceAboveFoldDelta } from '../hooks/virtualizer/FollowController'

const FOLD = 100

describe('repriceAboveFoldDelta', () => {
  it('ignores a row whose top is at or below the fold (it grows away from the reader)', () => {
    expect(repriceAboveFoldDelta({ rowTop: FOLD, prevHeight: 200, newHeight: 260, foldTop: FOLD })).toBe(0)
    expect(repriceAboveFoldDelta({ rowTop: FOLD + 40, prevHeight: 200, newHeight: 260, foldTop: FOLD })).toBe(0)
    // Even when that row is the streaming one.
    expect(repriceAboveFoldDelta({ rowTop: FOLD + 40, prevHeight: 200, newHeight: 260, foldTop: FOLD, appendsAtBottom: true })).toBe(0)
  })

  it('compensates the full change for a row ENTIRELY above the fold, grow or shrink, whatever the cause', () => {
    // rowTop -500, prevHeight 300 => bottom at -200, above the fold at 100.
    expect(repriceAboveFoldDelta({ rowTop: -500, prevHeight: 300, newHeight: 408, foldTop: FOLD })).toBe(108)
    expect(repriceAboveFoldDelta({ rowTop: -500, prevHeight: 300, newHeight: 276, foldTop: FOLD })).toBe(-24)
    // A tail row that has scrolled entirely above the reader pushes everything
    // below it (the reader included) when it grows: still compensated.
    expect(repriceAboveFoldDelta({ rowTop: -500, prevHeight: 300, newHeight: 354, foldTop: FOLD, appendsAtBottom: true })).toBe(54)
  })

  it('compensates a STRADDLING row that is REPRICED (the walk-drift fix stays intact)', () => {
    // rowTop -200, prevHeight 600 => bottom at 400: straddles the fold at 100.
    expect(repriceAboveFoldDelta({ rowTop: -200, prevHeight: 600, newHeight: 588, foldTop: FOLD })).toBe(-12)
    expect(repriceAboveFoldDelta({ rowTop: -200, prevHeight: 600, newHeight: 708, foldTop: FOLD })).toBe(108)
  })

  it('#10810: does NOT compensate a STRADDLING row whose growth is appended at its bottom', () => {
    // The reader is mid-message: the streaming row's top is above the fold and
    // its bottom well below. A token lands at the bottom (+27 / +54, the
    // per-tick sizes seen in the inspector). Nothing above the fold changed.
    for (const grow of [27, 54, 80, 107]) {
      expect(
        repriceAboveFoldDelta({ rowTop: -3000, prevHeight: 8000, newHeight: 8000 + grow, foldTop: FOLD, appendsAtBottom: true }),
      ).toBe(0)
    }
    // A bottom-edge shrink on the same row (a streaming table snapping into
    // shape) is equally below the eye line.
    expect(repriceAboveFoldDelta({ rowTop: -3000, prevHeight: 8000, newHeight: 7954, foldTop: FOLD, appendsAtBottom: true })).toBe(0)
  })

  it('DISCRIMINATOR: the same straddling geometry IS compensated when the change is a reprice', () => {
    // Identical numbers to the case above, minus the append flag: without the
    // flag the predicate cannot tell the streaming row from a repriced one and
    // credits the growth -- which is the pre-fix behaviour this file pins
    // against.
    expect(repriceAboveFoldDelta({ rowTop: -3000, prevHeight: 8000, newHeight: 8054, foldTop: FOLD })).toBe(54)
  })
})
