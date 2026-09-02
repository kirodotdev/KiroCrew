// FollowController — pure decision logic for the chat "stick to bottom" follow.
//
// WHY THIS EXISTS
// ===============
// The chat scroller has to keep the latest message pinned to the bottom while
// content streams in and widget iframes load asynchronously — but it must NOT
// fight the user when they scroll up to read history. Earlier attempts encoded
// this as a tangle of refs (`pinToBottomRef` + `intentionalPinRef` +
// `lastScrollTopRef` + a two-mode distance gate) whose updates depended on the
// `scroll` event firing before a ResizeObserver callback. That ordering is not
// guaranteed: a widget that finishes loading right after the user scrolls up
// fires its RO with a stale "we're following" flag and yanks the user back to
// the bottom. Every fix to one symptom spawned another because the decision
// was spread across event handlers that race each other.
//
// THE MODEL
// =========
// A single boolean `stick` ("the viewport should stay pinned to the bottom").
//   - It is turned OFF only by a genuine user scroll away from the bottom.
//   - It is turned ON only by the user returning to the bottom, or by an
//     explicit jump-to-bottom / slot-entry (a "forced" pin).
//
// Two facts make the decision race-proof without depending on event ordering:
//
//   1. `el.scrollTop` is readable SYNCHRONOUSLY. At the moment we are about to
//      pin (inside the RO / layout-effect), we compare the live scrollTop to
//      the position we last WROTE ourselves (`lastWriteTop`). If the live value
//      is below it, the user has scrolled up since our last write — even if the
//      `scroll` event has not dispatched yet — so we release `stick` and skip
//      the pin. (`evaluateAutoPin`)
//
//   2. Our own programmatic writes also fire `scroll` events. We recognise them
//      by comparing scrollTop to `lastWriteTop` (`isSelfScroll`) so they never
//      get mistaken for the user scrolling and never flip `stick`.
//
// All functions here are pure so the behaviour is verifiable without a DOM.

/** Default distance (px) from the bottom within which `isAtBottom` is true. */
export const DEFAULT_BOTTOM_THRESHOLD = 100

/**
 * Tolerance (px) for treating a scroll position as "the same" as a value we
 * wrote programmatically. Covers sub-pixel rounding and 1px momentum overshoot.
 * Must stay small so a deliberate user scroll of even a few px is still seen as
 * a user scroll.
 */
export const SELF_SCROLL_EPSILON = 2

/**
 * "At the bottom" tolerance (px) for deciding whether an auto-pin still has
 * work to do. A flat 0.5 is UNDER one device pixel at fractional device-pixel
 * ratios (0.67 CSS px at 150% zoom, 0.8 at 125%): the scroller's resting
 * maximum scrollTop lands on a fractional value, so `|scrollTop - target|`
 * stays just above 0.5 even when the viewport is visually pinned to the
 * bottom — making the pin re-fire on every ResizeObserver tick. Scaling the
 * epsilon to the device pixel (never below 1 CSS px) absorbs that fractional
 * resting error. `devicePixelRatio` is read defensively so a jsdom / SSR
 * environment that leaves it undefined falls back to 1 (→ 1.5px).
 */
export function atBottomEpsilon(): number {
  const dpr =
    typeof window !== 'undefined' &&
    typeof window.devicePixelRatio === 'number' &&
    window.devicePixelRatio > 0
      ? window.devicePixelRatio
      : 1
  return Math.max(1, 1 / dpr + 0.5)
}

/** Live scroll geometry snapshot read from the scroller element. */
export interface ScrollGeom {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

/** scrollTop that places the viewport exactly at the bottom (never negative). */
export function bottomTarget(geom: ScrollGeom): number {
  return Math.max(0, geom.scrollHeight - geom.clientHeight)
}

/** Pixels between the current scroll position and the bottom. */
export function distanceFromBottom(geom: ScrollGeom): number {
  return geom.scrollHeight - geom.scrollTop - geom.clientHeight
}

/** Whether the scroller is within `threshold` px of the bottom. */
export function computeAtBottom(geom: ScrollGeom, threshold: number): boolean {
  return distanceFromBottom(geom) <= threshold
}

/**
 * Recognise a `scroll` event caused by our own programmatic write rather than
 * by the user. `lastWriteTop < 0` means "we have not written this session", so
 * any scroll is treated as the user's.
 */
export function isSelfScroll(
  scrollTop: number,
  lastWriteTop: number,
  epsilon: number = SELF_SCROLL_EPSILON,
): boolean {
  return lastWriteTop >= 0 && Math.abs(scrollTop - lastWriteTop) <= epsilon
}

/**
 * Distance (px) from the true bottom within which a user scroll RE-ENGAGES
 * follow. Deliberately much tighter than DEFAULT_BOTTOM_THRESHOLD: that 100px
 * band drives the jump-to-bottom pill's visibility, and reusing it for follow
 * meant a deliberate 3-99px scroll-up kept `stick` armed — the next content
 * change then yanked the reader back to the bottom. Re-engaging only when the
 * user has returned essentially to the bottom keeps "scrolled up to read"
 * positions belonging to the user.
 */
export const FOLLOW_REENGAGE_PX = 16

/**
 * Direction-aware `stick` decision for a *user-initiated* scroll (self-scrolls
 * filtered out by the caller via `isSelfScroll`):
 *
 *   1. At the true bottom (within the DPR-aware epsilon) → follow. This also
 *      absorbs the layout engine's clamp: a mid-stream content SHRINK drops
 *      scrollTop (which reads as an upward move) but lands exactly at the new
 *      bottom — releasing there froze streaming follow for the rest of the
 *      turn.
 *   2. Any other upward move → release, regardless of distance from the
 *      bottom. The scroll position now belongs to the user; only returning to
 *      the bottom (3) re-engages.
 *   3. Downward arrival within FOLLOW_REENGAGE_PX of the bottom → re-engage.
 *   4. Otherwise (downward/neutral, still away from the bottom) → keep the
 *      previous state.
 *
 * `prevScrollTop < 0` means "no prior observation this session". Direction is
 * unknowable then, so the decision is position-only and CONSERVATIVE: follow
 * only within the re-engage band. Keeping a stale `stick` on an unattributable
 * away-from-bottom scroll is how a reader gets yanked.
 */
export function resolveUserScrollStick(args: {
  stick: boolean
  followOutput: boolean
  scrollTop: number
  prevScrollTop: number
  geom: ScrollGeom
}): boolean {
  const { stick, followOutput, scrollTop, prevScrollTop, geom } = args
  if (!followOutput) return false
  const dist = distanceFromBottom(geom)
  if (dist <= atBottomEpsilon()) return true
  if (prevScrollTop < 0) return dist <= FOLLOW_REENGAGE_PX
  if (scrollTop < prevScrollTop - 0.5) return false
  if (dist <= FOLLOW_REENGAGE_PX) return true
  return stick
}

/** Result of an automatic (RO / append) pin evaluation. */
export interface AutoPinResult {
  /** Whether to write `el.scrollTop = target` now. */
  pin: boolean
  /** Next value for `stick` (released to false if the user scrolled up). */
  stick: boolean
  /** The bottom scrollTop the caller should write when `pin` is true. */
  target: number
}

/**
 * Decide an automatic pin at the moment content changed (RO callback / append
 * layout effect / its follow-up rAF), reading LIVE geometry.
 *
 *   - Not sticking → never pin.
 *   - Sticking but the user has scrolled up since our last write
 *     (`scrollTop < lastWriteTop - epsilon`) → release stick, don't pin.
 *     This is the synchronous, race-proof guard.
 *   - Otherwise → pin to the bottom (only actually move if not already there).
 *
 * `lastWriteTop < 0` disables the scroll-up guard (used right after a slot
 * switch, before we have written anything this session).
 */
export function evaluateAutoPin(args: {
  stick: boolean
  geom: ScrollGeom
  lastWriteTop: number
  epsilon?: number
}): AutoPinResult {
  const { stick, geom, lastWriteTop } = args
  const epsilon = args.epsilon ?? SELF_SCROLL_EPSILON
  const target = bottomTarget(geom)
  if (!stick) return { pin: false, stick: false, target }
  // Release only on a genuine user scroll-UP: scrollTop dropped below our last
  // write AND we are now meaningfully away from the bottom. A pure content
  // SHRINK mid-stream (a partial markdown line re-parsing, a code fence opening
  // and reclassifying the block) clamps scrollTop below lastWriteTop too, but
  // leaves us still AT the new bottom (distance ~0). Without the distance guard
  // that shrink looked like a scroll-up and froze streaming follow — once
  // released, nothing re-armed stick for the rest of the response.
  if (
    lastWriteTop >= 0 &&
    geom.scrollTop < lastWriteTop - epsilon &&
    distanceFromBottom(geom) > epsilon
  ) {
    return { pin: false, stick: false, target }
  }
  return { pin: Math.abs(geom.scrollTop - target) > atBottomEpsilon(), stick: true, target }
}
