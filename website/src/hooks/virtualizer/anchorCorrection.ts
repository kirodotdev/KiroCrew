/** How far to move the scroller so a landing does not disturb the reader.
 *
 *  Extracted as a pure function because the harness cannot reach the case that
 *  matters: `act(() => rerender(...))` flushes layout effects synchronously, so a
 *  test can never move the scroller BETWEEN an anchor's capture and its consume --
 *  which is the normal case on a phone, where a page is fetched precisely because
 *  the reader is scrolling and momentum outlives the ~130ms fetch. Two attempts to
 *  test it through the harness passed with the correction removed AND with its sign
 *  inverted. Here the same arithmetic is checkable directly.
 */

export interface AnchorReading {
  /** The anchor row's top edge, relative to the scroller's viewport top, as
   *  measured when the anchor was captured. */
  capturedTop: number
  /** The scroller's `scrollTop` at that same moment. A screen position means one
   *  thing at one scroll offset and something else at another, so the pair travels
   *  together or neither is usable. */
  capturedScrollTop: number
  /** The same row's top edge, measured after the commit. */
  currentTop: number
  /** The scroller's `scrollTop` now. */
  currentScrollTop: number
  /** Running total of every scrollTop pixel THIS CODE had written, as of the
   *  capture. */
  capturedWriteSum: number
  /** The same running total now. Its difference from `capturedWriteSum` is how much
   *  of the scroll drift since capture was OURS. */
  currentWriteSum: number
}

/** The amount to ADD to `scrollTop`.
 *
 *  The row's on-screen displacement decomposes into two independent terms:
 *
 *      currentTop - capturedTop = (content growth above it) - (scrollTop change)
 *
 *  Only the first is ours to compensate. Correcting by the raw displacement pins
 *  the row to the glass, which also cancels the second term -- and the second term
 *  is the reader's own finger, so the transcript fights the gesture that triggered
 *  the fetch. Adding the scroll change back isolates the content term.
 *
 *  Device trace this comes from, with the measurement windows aligned: one 9,440px
 *  write while the top spacer went 0 -> 0 (no content appeared at all) and the
 *  scroller was never overscrolled. Content growth of zero means the correct write
 *  was zero, and all 9,440px was the reader's upward scroll being undone.
 *  `sinceHard=6037ms` in the same trace is why no ownership guard caught it:
 *  momentum scrolling stamps no hard input, so "the reader owns the position"
 *  never became true.
 *
 *  BUT THE DRIFT IS NOT ALL THE READER, and assuming it was is the second way this
 *  goes wrong. Another mechanism can write the same scroller between one anchor's
 *  capture and its consume -- a device frame carried `repriced 3078px in 7` writes
 *  inside that window -- and adding OUR own write back as if a finger had done it
 *  compensates the same content twice:
 *
 *    CORR d=-267 owed=-354 res=86 painted=1   WRITE resize 7562->7295
 *    HELD was=-31 now=236 off=268
 *
 *  The row's measured displacement there was 1px and the write was 267. So the term
 *  that gets added back is the drift MINUS our own writes:
 *
 *      readerMoved  = (currentScrollTop - capturedScrollTop)
 *                     - (currentWriteSum - capturedWriteSum)
 *      contentShift = (currentTop - capturedTop) + readerMoved
 *
 *  Why subtracting is right rather than merely smaller: a reprice write is itself a
 *  compensation for a height change above the reader, so that change is already
 *  inside the displacement term. Crediting it again as reader scroll is the double
 *  count. Removing it leaves exactly the part nobody has paid for yet.
 *
 *  With the reader still AND nothing else writing, all three spellings are
 *  identical -- which is why every harness test agreed to within 2px and none of
 *  them caught either failure.
 */
export function contentShiftFor(r: AnchorReading): number {
  const displacement = r.currentTop - r.capturedTop
  const drift = r.currentScrollTop - r.capturedScrollTop
  const ours = r.currentWriteSum - r.capturedWriteSum
  const readerMoved = drift - ours
  return displacement + readerMoved
}
