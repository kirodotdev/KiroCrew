// How the meeting hides the transcript and the other agent panels while one
// panel is expanded (#21179).
//
// Not `display:none`: that drops the element's box, and with it every scroll
// position inside (engines differ on whether it comes back), and it stops the
// transcript's follow-new-speech effect from moving a scroller that has no box.
// OFFSTAGE keeps the element laid out, but takes it out of the flow and makes
// it invisible, which also removes it from the accessibility tree and the tab
// order:
//
//   - pinned to its positioned parent's box (top, left, right, bottom 0), at
//     the parent's full width unless its own width class says otherwise, so
//     its text does not rewrap;
//   - clipped there (`overflow: hidden`): a hidden box still counts toward its
//     scroll container's overflow, and a panel taller than the column would
//     otherwise give the expanded panel blank room to scroll into;
//   - laid out as a plain block, so a child that is a flex item keeps its own
//     height inside the clip instead of shrinking to it, and its inner scroll
//     positions are untouched.
//
// ONSTAGE puts it back in the flow with its usual `display`. Both are Framer
// Motion end states: OFFSTAGE is applied when the fade-out ends, ONSTAGE when
// the fade-in starts.

export const OFFSTAGE = {
  display: 'block',
  visibility: 'hidden',
  position: 'absolute',
  top: 0,
  left: 0,
  right: 0,
  bottom: 0,
  overflow: 'hidden',
  pointerEvents: 'none',
} as const

export function onstage(display: 'block' | 'flex') {
  return {
    display,
    visibility: 'visible',
    position: 'relative',
    top: 'auto',
    left: 'auto',
    right: 'auto',
    bottom: 'auto',
    overflow: 'visible',
    pointerEvents: 'auto',
  } as const
}
