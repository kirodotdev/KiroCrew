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

/** What turns a wheel's `deltaX` into the fingers' travel: `1` when a positive
 *  `deltaX` is the fingers moving left, `-1` when it is the fingers moving
 *  right. `deltaX` is the SCROLL, and macOS flips its sign when Natural
 *  scrolling is off.
 *
 *  `inverted` is the event's `webkitDirectionInvertedFromDevice` (WebKit only):
 *  `true` with Natural scrolling on. `natural` is the desktop shell's report
 *  (lib/scrollTouch). The event's own flag wins; when neither is known the
 *  sign is `1`, the macOS default.
 *
 *  The frame's wheel relay (lib/widgetSrcdoc) carries a copy of this rule. */
export function fingerSign(inverted: unknown, natural: boolean | null): 1 | -1 {
  const on = typeof inverted === 'boolean' ? inverted : natural
  return on === false ? -1 : 1
}

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
