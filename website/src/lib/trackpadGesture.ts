/** A gap longer than this between wheel events ends a trackpad run: the fingers
 *  are off. A moving finger reports every 8-16ms, and a finger that only slows
 *  or pauses for a moment (60ms and less) keeps the drag.
 *
 *  One boundary for both readers of a run: the page's swipe reader
 *  (hooks/useTrackpadSwipe) and the wheel relay inside a Dashboard frame
 *  (lib/widgetSrcdoc). If the relay's gap were longer, a run that starts soon
 *  after a vertical scroll would still be the scroll's to the frame, and a
 *  swipe over the frame would never reach the page. */
export const SWIPE_GESTURE_IDLE_MS = 100

/** Whether a horizontal scroller can still move the way a wheel's `deltaX`
 *  pushes it (positive `dx` raises `scrollLeft`). `max` is `scrollWidth -
 *  clientWidth`. Left to right, `scrollLeft` runs over [0, max]; right to left
 *  (computed `direction: rtl`) it runs over [-max, 0], so the start is 0 at the
 *  right edge.
 *
 *  The frame's wheel relay (lib/widgetSrcdoc, `room`) carries a copy of this
 *  rule in its own script, as it cannot import; the relay test runs both over
 *  the same cases so they cannot drift. */
export function hasScrollRoomX(scrollLeft: number, max: number, rtl: boolean, dx: number): boolean {
  if (max <= 1) return false
  if (!rtl) return dx > 0 ? scrollLeft < max - 1 : scrollLeft > 0
  return dx > 0 ? scrollLeft < 0 : scrollLeft > -(max - 1)
}
