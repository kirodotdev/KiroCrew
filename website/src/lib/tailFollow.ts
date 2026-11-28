// Follow latch for a nested pane that tails appended text (the expanded
// reasoning trace, a subagent's output pane): which scroll positions keep the
// pane pinned to its end while new text arrives.

/** Slack, in px, for "is the pane at its end". An exact test never re-arms
 *  following: sub-pixel offsets leave scrollTop a hair short, stranding a
 *  reader who scrolled back down. */
export const TAIL_SLACK_PX = 20

/** Within a line's slack of the bottom. */
export const isAtTailEnd = (el: HTMLElement) =>
  el.scrollTop + el.clientHeight >= el.scrollHeight - TAIL_SLACK_PX

/** The pane's geometry as of the last scroll event or pin. */
export interface TailPosition {
  top: number
  height: number
}

/** scrollTop held to the pane's scroll range: Safari's elastic overscroll
 *  reports a top past either edge, and its settle back is not the reader
 *  moving up. */
const scrollTopInRange = (el: HTMLElement) =>
  Math.min(Math.max(el.scrollTop, 0), Math.max(el.scrollHeight - el.clientHeight, 0))

/** Where the pane is now, to hand to the next `followsAfterScroll`. */
export const tailPosition = (el: HTMLElement): TailPosition => ({ top: scrollTopInRange(el), height: el.scrollHeight })

/**
 * Whether the pane should still follow its tail after a scroll event that
 * moved it from `prev` (null when there is no earlier position).
 *
 * Any UPWARD move releases, however small, even inside the slack: a touchpad
 * scrolls a few px (or a fraction of one) per event, so a slack-only test kept
 * a reader scrolling up from the end "at the end" and the next chunk pinned
 * them back down, the pane jittering against their scroll. The one upward move
 * that is not the reader's is the engine clamping a box that got SHORTER: it
 * lands flush at the new end, so it does not release.
 *
 * Only a DOWNWARD move (or the first event, with no `prev`) re-arms. An event
 * that did not move the pane, such as the one a pin queued and the browser
 * delivers after an upward wheel already released, keeps `following` as is.
 */
export function followsAfterScroll(el: HTMLElement, prev: TailPosition | null, following: boolean): boolean {
  if (prev === null) return isAtTailEnd(el)
  const clamped = el.scrollHeight < prev.height
    && el.scrollHeight - (el.scrollTop + el.clientHeight) <= 1
  const top = scrollTopInRange(el)
  if (top < prev.top && !clamped) return false
  if (top > prev.top && !clamped) return isAtTailEnd(el)
  return following && isAtTailEnd(el)
}
