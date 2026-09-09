import { describe, it, expect } from 'vitest'

import { contentShiftFor } from '../hooks/virtualizer/anchorCorrection'

/** These pin the arithmetic the harness cannot reach. Each case is a real device
 *  reading or its degenerate twin, not an invented number.
 *
 *  The first five pass `capturedWriteSum: 0, currentWriteSum: 0` explicitly rather
 *  than defaulting them, because "nothing else wrote the scroller during this
 *  window" is an ASSUMPTION each of those readings was taken under, and a default
 *  would hide it. The cases below them are the ones where it does not hold. */
describe('anchor correction: compensate content, never the reader', () => {
  it('writes NOTHING when only the reader moved', () => {
    // The device's own figures: the anchor appeared 9,440px lower while the top
    // spacer went 0 -> 0, so no content had arrived and scrollTop had dropped by
    // exactly that much. Correct answer: leave the position alone.
    expect(contentShiftFor({
      capturedTop: 0,
      capturedScrollTop: 10_337,
      currentTop: 9_440,
      currentScrollTop: 897,
      capturedWriteSum: 0,
      currentWriteSum: 0,
    })).toBe(0)
  })

  it('compensates the full growth when the reader held still', () => {
    // A page lands above a parked reader: the row is pushed down by the whole
    // inserted height and every pixel of it is owed.
    expect(contentShiftFor({
      capturedTop: -120,
      capturedScrollTop: 5_000,
      currentTop: 13_775,
      currentScrollTop: 5_000,
      capturedWriteSum: 0,
      currentWriteSum: 0,
    })).toBe(13_895)
  })

  it('separates the two when they happen together', () => {
    // Content grew 16,090px (a measured spacer change) while the reader scrolled
    // up 2,000px. Only the content term may be written.
    expect(contentShiftFor({
      capturedTop: 0,
      capturedScrollTop: 9_000,
      currentTop: 18_090,
      currentScrollTop: 7_000,
      capturedWriteSum: 0,
      currentWriteSum: 0,
    })).toBe(16_090)
  })

  it('does not invent movement when nothing happened at all', () => {
    expect(contentShiftFor({
      capturedTop: 42,
      capturedScrollTop: 1_234,
      currentTop: 42,
      currentScrollTop: 1_234,
      capturedWriteSum: 0,
      currentWriteSum: 0,
    })).toBe(0)
  })

  it('handles the reader scrolling DOWN, where the terms have the same sign', () => {
    // Scrolling down raises scrollTop and lifts the row, so both terms are
    // negative-then-positive: a naive absolute value would double-count here.
    expect(contentShiftFor({
      capturedTop: 500,
      capturedScrollTop: 1_000,
      currentTop: 100,
      currentScrollTop: 1_400,
      capturedWriteSum: 0,
      currentWriteSum: 0,
    })).toBe(0)
  })

  it('does not pay twice for a drift OUR OWN reprice caused', () => {
    // The device frame this was written from. A reprice run (`repriced 3078px in 7`)
    // landed between the capture and the consume, so scrollTop drifted -268 with no
    // finger involved. The row's measured displacement was 1px.
    //
    //   CORR d=-267 owed=-354 res=86 painted=1   WRITE resize 7562->7295
    //   HELD was=-31 now=236 off=268
    //
    // Treating the drift as the reader gives -267: a 1px displacement authorising a
    // 267px write, painted. Attributing it to us gives 1px, which is all that was
    // ever measured.
    expect(contentShiftFor({
      capturedTop: -31,
      capturedScrollTop: 7_830,
      currentTop: -30,
      currentScrollTop: 7_562,
      capturedWriteSum: 0,
      currentWriteSum: -268,
    })).toBe(1)
  })

  it('splits a window where the reader AND we both moved the scroller', () => {
    // Content grew 4,000px above the row. A reprice of ours wrote +1,500 and the
    // reader scrolled up 500 (scrollTop -500) in the same window, so scrollTop
    // drifted +1,000 net. Only the 500 belongs to the reader.
    //
    //   displacement = 4000 - 1000 = 3000
    //   readerMoved  = 1000 - 1500 = -500
    //   contentShift = 3000 + (-500) = 2500
    //
    // 2,500 rather than 4,000 because the reprice already compensated 1,500 of the
    // growth -- paying it again is the double count -- and 500 of the row's
    // remaining displacement is the reader's own scroll, which must survive.
    expect(contentShiftFor({
      capturedTop: 0,
      capturedScrollTop: 6_000,
      currentTop: 3_000,
      currentScrollTop: 7_000,
      capturedWriteSum: 0,
      currentWriteSum: 1_500,
    })).toBe(2_500)
  })

  it('reads the write total as a DIFFERENCE, not as an absolute', () => {
    // The counter is cumulative for the life of the mount, so a session that has
    // already written 400,000px must behave exactly like one that has written none.
    // Anything that reads `currentWriteSum` on its own instead of against the
    // capture drifts further off the longer a session is open -- which is the
    // failure mode that would never show up in a short test.
    const shifted = contentShiftFor({
      capturedTop: 0,
      capturedScrollTop: 6_000,
      currentTop: 3_000,
      currentScrollTop: 7_000,
      capturedWriteSum: 400_000,
      currentWriteSum: 401_500,
    })
    expect(shifted).toBe(2_500)
  })

  it('leaves the reader-only case alone even when we wrote earlier', () => {
    // Our writes BEFORE the capture must not leak in: the reader scrolled 9,440 and
    // nothing else happened inside the window, so the answer is still zero however
    // busy the mount was beforehand.
    expect(contentShiftFor({
      capturedTop: 0,
      capturedScrollTop: 10_337,
      currentTop: 9_440,
      currentScrollTop: 897,
      capturedWriteSum: 12_345,
      currentWriteSum: 12_345,
    })).toBe(0)
  })
})
