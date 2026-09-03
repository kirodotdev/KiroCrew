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
 * Is a height-sync anchor captured at `capturedScrollTop` still usable now that
 * the scroller reads `liveScrollTop`?
 *
 * A viewport-relative capture consumed after the viewport MOVED corrects the
 * reader's own scrolling rather than the repricing it was taken for (measured
 * as a 2706px teleport on the phone rig during a cold-cache walk). scrollTop is
 * the exact discriminator: a reprice ABOVE the viewport changes where rows sit,
 * never scrollTop. So unchanged ⇒ the whole delta belongs to the reprice and is
 * safe to correct HOWEVER LATE it lands; changed ⇒ something else moved the
 * viewport (a finger, iOS momentum — which keeps moving with no further hard
 * input, so an input-timestamp gate misses it — or Chromium's native anchoring,
 * which already absorbed the shift, making the correction a no-op anyway).
 *
 * Wall-clock age was the first approximation and failed on the wrong side at
 * the worst moment: a turn ending is the busiest the main thread gets, so the
 * consumer runs late, a STILL reader's anchor was dropped, and they paid the
 * entire reprice as one displacement.
 */
/**
 * How far a reader must be moved to stay put when a row ABOVE them is repriced.
 *
 * The height INDEX learns a mounted row's real height only when the debounced
 * sync runs, and the released-reader correction is keyed on the index's version
 * — so growth above a mid-transcript reader displaces them for the whole
 * debounce and is then undone. On the device that is one +108 CSS px step and an
 * exact −108 step ~100ms later: a bounce with a net effect of nothing. The
 * observer already knows the row and both heights, so the correction belongs in
 * that same fire.
 *
 * Only a row that lay ENTIRELY above the fold BEFORE the change counts, and
 * `prevHeight` is what decides that: a row straddling the top edge grows
 * downward from its own top, so what the reader sees is the row they are looking
 * at expanding — usually because they opened it — and holding their scroll
 * position there would fight the expansion instead of hiding it.
 *
 * The sign is kept: a SHRINK above the fold pulls content up by the same rule.
 */
export function repriceAboveFoldDelta(input: {
  /** Row's viewport-relative top, as the observer sees it (post-layout). */
  rowTop: number
  prevHeight: number
  newHeight: number
  /** Viewport-relative top of the scroll container. */
  foldTop: number
}): number {
  // The test is on the row's TOP, not its whole box. A reprice does not move a
  // row's top -- it moves its BOTTOM, and with it everything below, so a row
  // that STRADDLES the top edge displaces the reader by the full change just
  // like one entirely above it. Measured on the device and reproduced in
  // Chromium with `overflow-anchor: none`: four of the five drift steps in a
  // twelve-step walk were straddling rows shrinking 12-24px each, and excluding
  // them is what left the reader displaced.
  //
  // A row whose top is at or below the fold is still excluded: it grows and
  // shrinks downward, away from everything already on screen, and its own top --
  // the reader's eye line on it -- does not move.
  if (input.rowTop >= input.foldTop) return 0
  return input.newHeight - input.prevHeight
}


/**
 * Whether a geometry commit (spacer repricing) must WAIT for the reader to stop.
 *
 * The invariant this enforces: whatever is loading, what the reader is looking
 * at does not move. Growth above them extends upward, growth below extends
 * downward, and their own eye line stays put.
 *
 * Compensating a commit that lands mid-gesture cannot deliver that on iOS
 * Safari, which has no native scroll anchoring: the correction is a `scrollTop`
 * write, and a write issued while a finger or momentum owns the scroller either
 * fights the gesture or arrives a frame late, which is the bounce. Not
 * committing is the only option that moves nothing — so a released reader's
 * geometry waits, and lands in one compensated commit once they are still.
 *
 * A FOLLOWED reader is exempt: the bottom pin owns their position, and stalling
 * the streaming row's growth would re-create the spacer lurch that its eager
 * sync path exists to prevent.
 *
 * There is deliberately NO deferral ceiling. A cap would guarantee a visible
 * displacement during exactly the long continuous scroll this exists to protect,
 * and it buys nothing that waiting does not: a gesture always ends, and the
 * spacers stay on their estimates until it does — which is how every
 * never-measured row is already priced.
 */
export function geometryCommitDeferred(input: {
  /** Follow armed — the bottom pin owns positioning, so never defer. */
  stick: boolean
  now: number
  /** Last real hardware input (wheel, touch, key). */
  lastHardInputAt: number
  /** Last scroll event that was NOT one of our own writes (includes momentum). */
  lastUserScrollAt: number
  settleMs: number
}): boolean {
  if (input.stick) return false
  const lastMotion = Math.max(input.lastHardInputAt, input.lastUserScrollAt)
  return input.now - lastMotion <= input.settleMs
}

export function heightAnchorStillUsable(
  capturedScrollTop: number,
  liveScrollTop: number,
  epsilon: number = SELF_SCROLL_EPSILON,
): boolean {
  return Math.abs(liveScrollTop - capturedScrollTop) <= epsilon
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
 *   3. A genuine DOWNWARD move that arrives within FOLLOW_REENGAGE_PX of the
 *      bottom → re-engage. A neutral event inside the band does NOT: that is
 *      how content collapsing under a still reader re-armed follow.
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
  /** Change in the scroller's own height since the previous scroll event.
   *
   *  Positive = the viewport GREW (the composer shrank under a deletion, the
   *  keyboard closed). That growth lowers the maximum scrollTop, so the engine
   *  clamps any reader parked closer to the bottom than the growth — with no
   *  application write anywhere. The clamp then arrives here as an ordinary
   *  scroll event sitting at distance ~0, which rule 1 below used to read as
   *  "the reader came back to the bottom" and re-arm follow for someone who
   *  never touched the scroller. The next turn to start then took them to the
   *  end. Rule 1 exists to absorb a CONTENT-shrink clamp mid-stream, and content
   *  shrink moves `scrollHeight`, not `clientHeight` — so the two are
   *  distinguishable, and this is the delta that tells them apart. */
  viewportGrowth?: number
}): boolean {
  const { stick, followOutput, scrollTop, prevScrollTop, geom } = args
  if (!followOutput) return false
  const dist = distanceFromBottom(geom)
  // A viewport growth large enough to explain the reader's arrival at the bottom
  // is the engine's clamp, not the reader. Leave `stick` exactly as it was.
  // A native clamp only ever LOWERS scrollTop, so a downward move concurrent with
  // the growth is the user's own and must still re-engage follow. Without the
  // direction term a reader who deliberately scrolls down while the keyboard
  // closes is refused their re-engagement.
  const clampedByViewport =
    (args.viewportGrowth ?? 0) > atBottomEpsilon() && scrollTop <= prevScrollTop + atBottomEpsilon()
  if (dist <= atBottomEpsilon()) return clampedByViewport ? stick : true
  if (prevScrollTop < 0) return dist <= FOLLOW_REENGAGE_PX
  if (scrollTop < prevScrollTop - 0.5) return false
  // Re-engagement requires a genuine DOWNWARD move, not merely a non-upward
  // event that finds the reader inside the band. A neutral event (identical
  // scrollTop -- the tail of an iOS momentum run, or any scroll fired while the
  // reader is at rest) used to satisfy this, so a reader sitting mid-transcript
  // could be re-armed by CONTENT rather than by their own hand: when rows
  // outside the window reprice smaller than their estimates, the remaining
  // content collapses under them and the bottom band arrives at the reader
  // instead of the reader arriving at it. Follow re-engaged, and the next pin
  // took them to the end -- reported as scrolling along and suddenly landing at
  // the bottom. Distance alone cannot tell those apart; the direction of the
  // reader's own move can.
  if (scrollTop > prevScrollTop + 0.5 && dist <= FOLLOW_REENGAGE_PX) return true
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
 *
 * `viewportShrink` (px, default 0) is how much the SCROLLER'S OWN BOX has
 * shrunk since that reference was recorded — chrome mounting below the
 * transcript (a queue band, an attachment strip, a tip card), often
 * spring-animated over several frames. Our own shrink inflates
 * `distanceFromBottom` with no user input, so without this allowance the
 * distance guard reads it as "meaningfully away from the bottom". Paired with
 * a content SHRINK in the same commit window — a tail-row remount clamping
 * scrollTop below `lastWriteTop` — that produced a full user-scroll-up
 * signature out of two of our own layout changes: follow released mid
 * animation and the content settled a card-height low. Judging the distance
 * against the box we were last a bottom FOR keeps the guard measuring the
 * user's move rather than our own. Only the shrink's own pixels are forgiven,
 * so a genuine drag inside the same tick still releases.
 */
export function evaluateAutoPin(args: {
  stick: boolean
  geom: ScrollGeom
  lastWriteTop: number
  epsilon?: number
  viewportShrink?: number
  /** Is a turn actually producing output right now?
   *
   *  Follow means "keep me at the end of a LIVE turn". With nothing running there
   *  is no output to follow, so a reader sitting above the bottom is not
   *  following — and an automatic pin there is a yank with no cause, reported
   *  from a phone as the transcript springing back after scrolling up about a
   *  hundred pixels with nothing streaming.
   *
   *  Defaults to `true` = assume a run is live, which keeps the behaviour of a
   *  caller that has no run signal to give (the app-SDK chat surface). The chat
   *  transcript passes the real thing. */
  runActive?: boolean
}): AutoPinResult {
  const { stick, geom, lastWriteTop } = args
  const epsilon = args.epsilon ?? SELF_SCROLL_EPSILON
  const viewportShrink = Math.max(0, args.viewportShrink ?? 0)
  const runActive = args.runActive ?? true
  const target = bottomTarget(geom)
  if (!stick) return { pin: false, stick: false, target }
  // Idle: release rather than merely skip the pin. Skipping would leave follow
  // armed, so the next turn to start would yank this reader to the bottom from
  // wherever they had settled — the same defect one event later.
  if (!runActive && distanceFromBottom(geom) > atBottomEpsilon()) {
    return { pin: false, stick: false, target }
  }
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
    distanceFromBottom(geom) - viewportShrink > epsilon
  ) {
    return { pin: false, stick: false, target }
  }
  return { pin: Math.abs(geom.scrollTop - target) > atBottomEpsilon(), stick: true, target }
}
