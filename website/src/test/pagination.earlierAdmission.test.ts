import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { earlierAffordanceInView, EARLIER_ADMISSION_LEAD_PX } from '../pages/chat/pagination'

/**
 * THE admission rule for automatic older-history fetches: the control that offers
 * history must be on the reader's screen.
 *
 * Adopted after four separate automatic triggers were each found to page history
 * on something that was not the reader asking, all reproduced on a real phone:
 *
 *   - the idle prefetch, whose quiet timer was refreshed by a landing's OWN
 *     compensation write, so it ran land → quiet → land at a steady beat;
 *   - the same prefetch's authorization, a one-way latch that one touch of the
 *     transcript opened for the rest of the session;
 *   - the top sentinel's geometry branch, firing on a transient produced by the
 *     per-keystroke re-render the composer's ChatPage-state text causes;
 *   - the window-start crossing, walked down across its lead by the soft keyboard
 *     CLOSING, which grows the viewport by its whole height so the window extends
 *     upward with nobody travelling.
 *
 * Each proxy had a second cause. The control's visibility does not, and it also
 * subsumes the short-transcript special case: a transcript too short to scroll
 * shows the control without anyone scrolling.
 */
describe('earlierAffordanceInView', () => {
  const viewport = { top: 100, bottom: 700 }

  it('is false when the control is not mounted', () => {
    // It unmounts with the virtualized window, which is what makes "not mounted"
    // mean "the reader is nowhere near the head of the transcript".
    expect(earlierAffordanceInView(null, viewport)).toBe(false)
  })

  it('is false when the control sits above the viewport by more than the lead', () => {
    const farAbove = viewport.top - EARLIER_ADMISSION_LEAD_PX - 50
    expect(earlierAffordanceInView({ top: farAbove, bottom: farAbove + 40 }, viewport)).toBe(false)
  })

  it('is TRUE while the control is still above the viewport but within the lead', () => {
    // The whole point of the lead: the fetch is authorized before the reader
    // arrives, so the prepend lands off-screen. Without it the reader watches the
    // rows they are reading get re-laid-out, which is a regression even when the
    // compensation is exact.
    const nearAbove = viewport.top - 200
    expect(earlierAffordanceInView({ top: nearAbove, bottom: nearAbove + 40 }, viewport)).toBe(true)
  })

  it('the lead extends the region UPWARD only, never below', () => {
    // Extending downward would let a reader at the live end of a long transcript
    // authorize a fetch, which is the entire class of defect this rule exists for.
    const belowByLess = viewport.bottom + 100
    expect(earlierAffordanceInView({ top: belowByLess, bottom: belowByLess + 40 }, viewport)).toBe(false)
  })

  it('is false when the control sits below the viewport', () => {
    expect(earlierAffordanceInView({ top: 800, bottom: 840 }, viewport)).toBe(false)
  })

  it('is true when the control is fully inside', () => {
    expect(earlierAffordanceInView({ top: 200, bottom: 240 }, viewport)).toBe(true)
  })

  it('is true when the control is only partly revealed', () => {
    // A control clipped by a pixel of the fade band is still one the reader sees
    // and can press; demanding full containment would refuse it.
    expect(earlierAffordanceInView({ top: 80, bottom: 120 }, viewport)).toBe(true)
    expect(earlierAffordanceInView({ top: 680, bottom: 720 }, viewport)).toBe(true)
  })

  it('is false for a zero-height control exactly on each boundary', () => {
    // Strict comparisons on both sides: an element collapsed onto the boundary is
    // not visible, and admitting it would re-open the door on a layout transient.
    // Checked with lead 0 so the boundary under test is the viewport's own.
    expect(earlierAffordanceInView({ top: 700, bottom: 700 }, viewport, 0)).toBe(false)
    expect(earlierAffordanceInView({ top: 100, bottom: 100 }, viewport, 0)).toBe(false)
  })

  it('keeps the lead well under the distance to a long transcript head', () => {
    // A lead that grew to transcript scale would silently become "always on".
    expect(EARLIER_ADMISSION_LEAD_PX).toBeGreaterThan(0)
    expect(EARLIER_ADMISSION_LEAD_PX).toBeLessThanOrEqual(1000)
  })
})

describe('every automatic older trigger is gated on it', () => {
  const SRC = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')

  it('the top sentinel checks it first', () => {
    const i = SRC.indexOf('const handleTopReached = useCallback(')
    const body = SRC.slice(i, SRC.indexOf('}, [dispatch, earlierBarInView])', i))
    expect(body).toMatch(/if \(!earlierBarInView\(\)\) return/)
    // Before the geometry test, so a transient cannot reach it at all.
    expect(body.indexOf('earlierBarInView()')).toBeLessThan(body.indexOf('shouldAutoFillOlder'))
  })

  it('the walk poll and the idle prefetch both check it', () => {
    // Two separate interval bodies; both read the mirrored ref so the interval is
    // not re-armed per render (which would reset their own quiet clocks).
    const gated = SRC.split('if (!earlierBarInViewRef.current()) return').length - 1
    expect(gated).toBe(2)
  })

  it('the manual click path is NOT gated', () => {
    // The reader pressing the control IS the authorization; requiring the control
    // to be visible in order to honour a press on it would be circular.
    const i = SRC.indexOf('const handleLoadEarlier = useCallback(')
    expect(i).toBeGreaterThan(-1)
    const body = SRC.slice(i, SRC.indexOf('}, [', i))
    expect(body).not.toMatch(/earlierBarInView/)
  })
})
