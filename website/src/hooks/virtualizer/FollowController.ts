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

// After a genuine user scroll, suppress ResizeObserver-driven auto-pins for
// this long. Streaming/widget growth that should "follow" happens while the
// user is stationary at the bottom; a re-measuring widget that fires mid-fling
// must NOT yank the user (which also unmounts the rows they were scrolling
// through, leaving a blank flash). Explicit pins (slot entry, scrollToBottom,
// append) bypass this — only the RO follow path is gated.
export const SCROLL_SETTLE_MS = 150

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
 * Is the scroller laid out at ZERO height -- a backgrounded mobile tab, a
 * `display:none` pane, a not-yet-sized column?
 *
 * No reader can be moving in a box with no height, yet the geometry of one
 * reads like a reader who is: `distanceFromBottom` is the whole transcript and
 * `bottomTarget` is `scrollHeight` itself. Fed to evaluateAutoPin that either
 * releases follow for a reader who is not on screen or writes a scrollTop the
 * browser clamps the moment the box comes back -- and the clamp then reads as
 * a user scroll. ONE predicate, asked by BOTH automatic-pin sites (the
 * post-paint pinAuto and the pre-paint height-sync re-pin): a private copy at
 * one site is how the idle rule came to cover one half and not the other.
 *
 * Deliberately geometry-only. `document.hidden` is NOT part of it: a desktop
 * tab keeps its full layout while hidden, and its follower is pinned there
 * exactly as before.
 */
export function scrollerCollapsed(el: { clientHeight: number }): boolean {
  return el.clientHeight === 0
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
 * A row that lies entirely above the fold always counts. A row that STRADDLES
 * the top edge counts too when it is being REPRICED (an estimate replaced by a
 * measurement, a disclosure opened): every pixel of that change lands above the
 * fold and shoves the reader by it. It does NOT count when the change is
 * APPENDED at the row's bottom -- the streaming row growing by a token. Those
 * pixels arrive BELOW the reader's eye line, nothing they can see moves, and
 * the row's top edge above the fold stays exactly where it was. Compensating
 * that walks the reader down the message by one token's height per tick,
 * arriving on screen as the text they are reading sliding UP and out from
 * under them -- reported as "can't read the middle of a long reply while it
 * streams" (kirodotdev/KiroCrew#10810). The scroll inspector showed the
 * signature directly: `WRITE abovefold` firing +27/+54px per tick with
 * `Δtop == Δh`, while the same reader parked at the message's HEAD (row top
 * inside the fold) held perfectly.
 *
 * The sign is kept: a SHRINK above the fold pulls content up by the same rule.
 */
export function repriceAboveFoldDelta(input: {
  /** Row's viewport-relative top BEFORE the change being classified -- the
   *  position the reader last saw it at, not the post-layout rect (which
   *  already carries the batch's displacement and any native adjustment). */
  rowTop: number
  prevHeight: number
  newHeight: number
  /** Viewport-relative top of the scroll container. */
  foldTop: number
  /**
   * True when the row's height change is appended at its BOTTOM (the
   * actively-streaming row, or the row still in its post-stream settle grace).
   * A straddling row growing this way moves nothing the reader can see.
   */
  appendsAtBottom?: boolean
}): number {
  // A row whose top is at or below the fold is excluded: it grows and shrinks
  // downward, away from everything already on screen, and its own top -- the
  // reader's eye line on it -- does not move.
  if (input.rowTop >= input.foldTop) return 0
  // Entirely above the fold: the whole change is above the reader whatever
  // its cause, and moves them by exactly that.
  const rowBottom = input.rowTop + input.prevHeight
  if (rowBottom <= input.foldTop) return input.newHeight - input.prevHeight
  // STRADDLING. A reprice does not move a row's top -- it moves its BOTTOM,
  // and with it everything below, so a repriced straddler displaces the reader
  // by the full change just like one entirely above it. Measured on the device
  // and reproduced in Chromium with `overflow-anchor: none`: four of the five
  // drift steps in a twelve-step walk were straddling rows shrinking 12-24px
  // each, and excluding them is what left the reader displaced.
  //
  // Appended growth is the one case where that reasoning inverts: the new
  // pixels are at the bottom, below the fold, and the visible part of the row
  // is unchanged. See the docstring for what compensating it does.
  if (input.appendsAtBottom) return 0
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

/**
 * Whether an automatic pin must yield right now.
 *
 * The base rule is a fixed window from the last hardware input: a reader whose
 * gesture is in flight outranks any correction. That window alone is too short
 * for a height change the input CAUSED. Opening a disclosure with many lines
 * (a tool's error output, "Worked through N steps") renders and re-measures in
 * a cascade that outlives the window, so the tail of the cascade escaped the
 * gate and pinned — the expanded content the user opened was dragged past the
 * viewport top, felt as bounce, and only for the long ones.
 *
 * So suppression is extended by the CASCADE, not the clock: each resize that
 * arrives while suppressed pushes the deadline out again. Streaming growth is
 * unaffected because no hardware input precedes it, so nothing is ever armed.
 */
export function pinSuppressedNow(
  now: number,
  lastHardInputAt: number,
  cascadeUntil: number,
  settleMs: number,
): boolean {
  return now - lastHardInputAt < settleMs || now < cascadeUntil
}

/**
 * How much longer is a hardware scroll intent still waiting for its own scroll
 * event? 0 = not pending.
 *
 * Input lands BEFORE the scroll it causes: the wheel/touch/key listener stamps
 * intent, and the scroll event that moves `scrollTop` dispatches a frame later.
 * In that gap the reader still sits on our last write to the pixel, so a
 * position test alone reads them as resting -- and an append landing in the
 * same frame (the one automatic pin path with no settle gate of its own) would
 * pin them to the bottom against the scroll they have just started. The scroll
 * event then releases follow, so the harm is one visible yank, but it is a
 * yank the reader did not ask for.
 *
 * This is the ONE input term the resting rule keeps, and it is deliberately
 * narrow. The caller feeds it two stamps only: an input whose own direction was
 * UP (wheel deltaY < 0, an upward key, an upward touch drag), and a pointer that
 * landed on the SCROLLBAR (a drag is about to scroll and nothing names its
 * direction until it does). Each holds only until its scroll event arrives or
 * the settle window expires. A wheel-DOWN at the end, a click on a message, a
 * finger that landed and lifted move nothing and are never stamped -- treating
 * those as "the reader left" is the regression the position-only rule exists to
 * fix. The expiry covers the intent that never scrolls at all (a click on the
 * thumb without a drag, a wheel-up on a transcript shorter than its viewport):
 * the caller retries the held pin at that moment, so intent that produced no
 * scroll event within `settleMs` is spent, not preserved forever.
 *
 * Pure so the caller can hand it clock and stamps; `lastScrollEventAt` is the
 * time of the last scroll event that landed OUTSIDE the self-scroll epsilon of
 * the caller's last write, ours or the reader's, because after such an event
 * the position reflects the input and the position test is exact again. An
 * event inside that epsilon is not an answer: it is our own pin landing or the
 * first frame of the input, and the caller does not record it, so the hold
 * outlives it. Returns the ms left on the hold so the caller can schedule
 * its retry exactly at expiry.
 */
export function scrollIntentPending(
  now: number,
  lastIntentInputAt: number,
  lastScrollEventAt: number,
  settleMs: number,
): number {
  if (!(lastIntentInputAt > lastScrollEventAt)) return 0
  const left = settleMs - (now - lastIntentInputAt)
  return left > 0 ? left : 0
}

/**
 * Whether a prepend SHIFT COMPENSATION may write the scroll position.
 *
 * Those corrections keep the reader visually still when rows are inserted above
 * them, by adding the inserted height to `scrollTop`. That is only meaningful
 * when the reader's position is what it was before the insert -- so it must
 * stand down for every OTHER owner of the position:
 *
 * The one owner it stands down for is `stick`: follow-the-tail is pinning to the
 * bottom on its own schedule, so compensating would fight it.
 *
 * The second is `settleMeasuring`, and the wording is load-bearing. These
 * compensations and the restore's settle do the SAME job -- hold a row where it
 * was -- from different reference points, so while the settle is correcting they
 * fight it: captured on a phone inside one decisecond as
 *
 *   abovefold 49760->49632 / settle 49632->49760 / abovefold 49760->52142 /
 *   resize 52142->50321 / growth 50321->50021
 *
 * five writes, each undoing part of the last, netting +261px the reader sees as
 * the position sliding after it had landed.
 *
 * But it must NOT read "a restore is in flight". Standing down for the whole
 * restore window was tried and was worse: a settle that cannot see its anchor row
 * (`SETTLE.x no-node`) corrects nothing while still holding its gate, so blanking
 * these too left EVERY prepend in that window uncompensated -- which walked the
 * reader up a page per landing, pulled the top sentinel into view, and reopened
 * the older-history door. History loading itself, one page at a time.
 *
 * So the condition is whether the settle is actually doing the job, not whether it
 * is nominally in charge. Exactly one mechanism corrects at a time, and when the
 * settle goes blind these take over rather than everyone standing down.
 *
 * Extracted rather than left as three inline conditions because the rule is one
 * decision and has to read like one.
 */
export function shiftCompensationAllowed(input: {
  stick: boolean
  /** Is the restore's settle loop ACTIVELY correcting -- gate up AND able to see
   *  its anchor row? Not merely "a restore is in flight". */
  settleMeasuring: boolean
}): boolean {
  return !input.stick && !input.settleMeasuring
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
 *   1. At the true bottom (within the DPR-aware epsilon) → follow, PROVIDED
 *      follow was already armed. This absorbs the layout engine's clamp: a
 *      mid-stream content SHRINK drops scrollTop (which reads as an upward
 *      move) but lands exactly at the new bottom — releasing there froze
 *      streaming follow for the rest of the turn. Two exceptions, both meaning
 *      "this landing is not the engine carrying a follower":
 *        - a non-downward landing under a confirmed UPWARD user input inside
 *          the settle window (`upwardInputWithinSettle`): that shrink
 *          coincided with the reader's own scroll-up, so it belongs to the
 *          user and releases follow. A downward or directionless input keeps
 *          the clamp absorbed.
 *        - follow already RELEASED (`stick === false`): re-engage only when the
 *          reader moved DOWN to get here AND their own downward travel across
 *          the gesture is at least the viewport's growth (the same
 *          `readerTravel >= viewportGrowth` split as rule 3). A non-downward
 *          landing is the content below them collapsing (or the box growing)
 *          and the engine clamping them flush: arriving is not asking, and
 *          re-arming hands the rest of the turn to the pin. A downward landing
 *          the growth did most of the work for is a nudge the engine clamped
 *          flush -- rule 3's refused arrival delivered to distance 0 instead
 *          of the band -- and stays released too.
 *   2. Any other upward move → release, regardless of distance from the
 *      bottom. The scroll position now belongs to the user; only returning to
 *      the bottom (3) re-engages.
 *   3. A genuine DOWNWARD move that arrives within FOLLOW_REENGAGE_PX of the
 *      bottom → re-engage. A neutral event inside the band does NOT: that is
 *      how content collapsing under a still reader re-armed follow. Nor does
 *      a move the VIEWPORT's own growth did most of the work for: the arrival
 *      counts as the reader's only when their own downward travel across the
 *      gesture (`readerTravel`) is at least the growth (`viewportGrowth`) that
 *      brought the bottom up to meet them. That is how the iOS toolbar
 *      collapsing under a downward nudge re-armed it.
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
  /** Change in the scroller's own height across the current GESTURE: the
   *  caller accumulates the per-event deltas for as long as scroll events keep
   *  arriving within the settle window, and starts over once the reader rests.
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
   *  distinguishable, and this is the delta that tells them apart.
   *
   *  Rule 3 reads it too, as the growth's share of the reader's approach to
   *  the band (see `readerTravel`). That is why the value is the gesture's
   *  TOTAL and not one event's delta: Safari spreads the collapse over several
   *  frames, and a reader nudging down across them was credited only the last
   *  frame's growth while the earlier frames' growth had already carried the
   *  band onto them. Growth that landed while the reader RESTED is not in the
   *  total -- the caller re-baselines when the box changes with no reader
   *  scroll event in flight -- so a fresh drag toward the bottom is judged
   *  against the box it began in. Rule 1's released row reads the same total
   *  for the same split: a nudge the growth clamps FLUSH arrives at distance
   *  0 rather than inside the band, and is refused there by the same
   *  `readerTravel >= viewportGrowth` test. Rule 1's follower row and its
   *  non-downward landings are indifferent to it: a clamp keeps `stick`
   *  exactly as it was. */
  viewportGrowth?: number
  /** The reader's own DOWNWARD travel across the current gesture, in px: the
   *  caller sums the positive scrollTop deltas of the user scroll events that
   *  arrive within the settle window of one another (a shrink of the box, an
   *  engine clamp and an upward move all contribute nothing) and starts over
   *  once the reader rests. Omitted, no travel is credited.
   *
   *  The question, in rule 3 and in rule 1's released row alike, is "did the
   *  reader close the gap themselves?". Write
   *  the gap they were from the bottom when the gesture began as D0, their
   *  travel since as T and the box's growth as G; with the content unchanged
   *  the live distance is `dist = D0 - T - G`, so the approach `D0 - dist`
   *  splits exactly into the reader's T and the browser's G. An arrival
   *  (`dist <= FOLLOW_REENGAGE_PX`) is theirs when their share is at least the
   *  browser's, `T >= G`, and is refused when `T < G`: the growth carried them
   *  further than they moved, so the band came to them.
   *
   *  Judging the band against the pre-growth box instead
   *  (`dist + G <= FOLLOW_REENGAGE_PX`) asks a different question and is
   *  UNSATISFIABLE once G exceeds the band: growth lowers the maximum scrollTop
   *  by G, so every position the reader can reach has `dist + G >= G`. Worked
   *  at Safari's real G = 50: a reader parked 200px up drags 148px down while
   *  the bar collapses and lands 2px from the bottom. `dist + G = 52 > 16`
   *  refuses them; `T = 148 >= G = 50` re-engages -- they closed 148 of the
   *  198px approach. The nudge the rule exists for still holds: parked 60px
   *  up, 3px down, G = 50, `dist = 7`; `T = 3 < G = 50` refuses it, since 50 of
   *  the 53px of approach were the browser's. And a neutral event (T = 0) is
   *  refused by the direction test before this one is reached. With no growth
   *  in flight (G = 0) every downward arrival satisfies `T >= 0`, which is rule
   *  3 exactly as it was before viewport growth was considered at all. */
  readerTravel?: number
  /** The reader's own downward INPUT across the same gesture, in px, as the
   *  intent listeners reported it (a pixel wheel delta, a finger's path) --
   *  the travel they ASKED for, where `readerTravel` is the travel the engine
   *  ANSWERED with. The answer is clamped at the scroller's maximum, and a
   *  growth G lowers that maximum by G, so a reader parked D0 up is answered
   *  at most D0 - G however far they drag. Measured on the answer alone, any
   *  deliberate drag to the end from under 2G away has `T = D0 - G < G` and is
   *  refused as a nudge (at Safari's G = 50 that is everyone within 100px of
   *  the bottom), and once flush a further drag raises no scroll event to be
   *  judged at all. The input has neither limit: the larger of the two is the
   *  reader's travel. An input in the gesture is also the "hand on the
   *  scroller" a flush landing needs, when the growth outran the drag within
   *  the frame and the position alone reads as upward. Omitted or zero, the
   *  decision is the position-only one above. */
  readerIntent?: number
  /** How far above the bottom the reader was when the gesture began, in px.
   *  A second way for an arrival to be the reader's: their own travel covers
   *  the WHOLE gap (`travel >= gestureStartGap`), so they would have reached
   *  the bottom with no growth at all, however much the box grew meanwhile.
   *  The `T >= G` split cannot say that -- a keyboard closing (~300px) under
   *  a 250px drag from 200px up refuses a return the reader plainly made.
   *  Omitted, only the split applies. */
  gestureStartGap?: number
  /** A hardware user input whose own direction was UPWARD (wheel up / upward
   *  key / upward touch drag) landed within the scroll-settle window before
   *  this scroll event.
   *
   *  The bottom-epsilon branch below treats a scroll landing within
   *  `atBottomEpsilon` of the true bottom as the layout engine's clamp and
   *  keeps `stick` as it was. But a genuine user scroll-UP that happens to
   *  coincide with a mid-turn content shrink terminates within epsilon of the
   *  NEW bottom too, so it wears the same signature — and keeping `stick` armed
   *  there pins the reader back to the end on the next streaming resize. The
   *  intent listeners stamp the input's direction BEFORE its scroll event
   *  dispatches, so a fresh UPWARD stamp is proof the reader scrolled up: a
   *  non-downward landing at the bottom under it is the reader, not the
   *  engine, and releases follow.
   *
   *  The direction requirement is load-bearing: a wheel-DOWN at the bottom is
   *  an ordinary input during streaming, and a content-shrink clamp landing
   *  inside its settle window must NOT release follow — the reader asked to
   *  stay at the end. Directionless inputs (a scrollbar grab, a first touch
   *  move) are treated the same conservative way: only confirmed upward
   *  intent disables the clamp guard. A genuine clamp carries no upward
   *  input, so it still keeps `stick`. */
  upwardInputWithinSettle?: boolean
}): boolean {
  const { stick, followOutput, scrollTop, prevScrollTop, geom } = args
  if (!followOutput) return false
  const dist = distanceFromBottom(geom)
  // The split of the reader's approach between their own hand and the box's
  // growth (see `readerTravel` for the derivation). Read by BOTH arrival
  // branches below -- the bottom-epsilon one and rule 3's band -- because a
  // viewport growth can deliver a nudge to either: growth lowers the maximum
  // scrollTop, so a reader parked 60px up is only 10px from the new maximum
  // after Safari's 50px collapse, and any nudge of 10px or more is clamped
  // FLUSH (distance ~0) rather than landing inside the band. Guarding rule 3
  // alone left that -- most of the nudge range -- to a branch that decided on
  // direction, and a clamped nudge moves DOWN.
  const growth = Math.max(0, args.viewportGrowth ?? 0)
  const intent = Math.max(0, args.readerIntent ?? 0)
  const travel = Math.max(0, args.readerTravel ?? 0, intent)
  const startGap = args.gestureStartGap ?? Number.POSITIVE_INFINITY
  // The reader's arrival is theirs when their travel is at least the growth
  // that carried them (the split, see `readerTravel`) OR covers the whole gap
  // they set out to close (see `gestureStartGap`). The gap term needs a REAL
  // gap: a gesture that opens at the bottom (a released reader the engine
  // clamped flush, now at rest) seeds a gap of ~0, and `travel >= 0` would let
  // a 1px nudge re-arm follow for someone who never asked for the end.
  const arrivalIsTheirs = travel >= growth || (startGap > atBottomEpsilon() && travel >= startGap)
  if (dist <= atBottomEpsilon()) {
    // AT THE TRUE BOTTOM. `movedDown` is the discriminator: a clamp only ever
    // LOWERS scrollTop, so a downward landing here is the reader's own move
    // (or their move plus a clamp of its tail), never the engine alone.
    //
    //   upward input in the window, not moved down  -> release
    //     A user scroll-UP that coincided with a content shrink terminates at
    //     the NEW bottom and wears the clamp's signature; the input's own
    //     direction is the proof it was the reader's.
    //   follow armed (`stick`)                       -> keep following
    //     Rule 1 proper: the engine carrying a follower across a content
    //     shrink, or across a viewport growth -- both lower scrollTop, both
    //     keep `stick` exactly as it was. Travel and growth are irrelevant to
    //     a reader who is already following.
    //   released, not moved down                     -> stay released
    //     The content below the reader collapsed (or the box grew) far enough
    //     to drop the maximum under them and the engine clamped them flush
    //     with no finger near the screen. Arriving is not asking; re-arming
    //     hands the rest of the turn to the pin.
    //   released, moved down, travel >= growth        -> re-engage
    //     They dragged to the end themselves (T >= G, as in rule 3), or their
    //     drag covered the whole gap they started with (T >= D0) -- the
    //     keyboard closing under a drag to the end.
    //   released, moved down, travel <  growth        -> stay released
    //     The growth did most of the work and clamped the tail of a nudge
    //     flush: the same arrival rule 3 refuses inside the band, arriving
    //     at distance 0 instead. With G = 0 this row is unreachable (T >= 0),
    //     so a caller with no growth signal keeps the direction-only rule.
    //
    // The growth-only clamp (growth, no downward move) is the third row: it
    // is `stick` for a follower and `false` for a released reader, which is
    // what the rows already return for any non-downward landing, so it needs
    // no term of its own.
    //
    // "Moved down" is the position's answer OR the input's: a growth that
    // outruns the drag within one frame (the keyboard's ~300px against a
    // finger's ~20px per frame) lowers scrollTop on every frame of a drag that
    // is plainly downward, and the position alone would file the whole drag
    // under "no finger near the screen". The input substitutes ONLY while a
    // growth is in flight, because that is the one thing that can make a
    // downward drag read as a fall. With no growth a falling scrollTop at the
    // bottom is a CONTENT shrink clamping the reader flush -- a tool result
    // collapsing under a 30px drag from 200px up -- and the position is the
    // honest read: arriving is not asking, whatever the finger was doing.
    //
    // The input also substitutes ONLY with no upward evidence in the window.
    // Touch sampling outruns the frame rate, so an upward flick can be
    // followed by a 1px reversal sample before the frame's scroll event:
    // the reversal re-banks `readerIntent` after the upward sample zeroed
    // it (the last sample before the event wins), and the clamp's event then
    // carries BOTH the upward stamp and a positive intent. Letting the input
    // answer "moved down" there skips the very release the upward stamp
    // arms, so a follower who flicked up while the keyboard closed stayed
    // armed and the next pin yanked them back to the end. An upward input in
    // the window means the position is the honest read; the keyboard-close
    // returns this substitute exists for carry no upward input.
    const movedDown =
      (prevScrollTop >= 0 && scrollTop > prevScrollTop + atBottomEpsilon()) ||
      (intent > 0 && growth > atBottomEpsilon() && !args.upwardInputWithinSettle)
    if (args.upwardInputWithinSettle && !movedDown) return false
    if (stick) return true
    return movedDown && arrivalIsTheirs
  }
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
  //
  // The band also has to be reached by the reader's OWN move. A viewport GROWTH
  // lowers the bottom by exactly the growth with no scroll of theirs, and on
  // iOS it lands in the SAME frame as a downward move by construction: Safari's
  // toolbar collapses under the very drag that scrolls toward the bottom, so
  // for the frames of that animation a reader who nudged down a few px from
  // well outside the band read as arriving inside it -- the band came up to
  // meet them, the same class as the neutral-event case above. Follow re-armed
  // for a reader who never reached the bottom, and the next automatic pin (the
  // toolbar re-showing, a streaming token) carried them to the end.
  //
  // The arrival test stays the live distance, because the live bottom is the
  // only one the reader can reach: the growth lowers the maximum scrollTop, so
  // a band judged against the pre-growth box lies past the wall whenever the
  // growth exceeds the band, and a reader dragging all the way down would be
  // refused. The discriminator is the split of the approach instead: the
  // reader's own downward travel this gesture against the box's growth this
  // gesture. Their arrival is theirs when they moved at least as far as the
  // growth carried them (see `readerTravel` for the derivation and a worked
  // example); a nudge the growth did most of the work for is not. A SHRINK is
  // not credited either way: the live distance already reads the bottom moving
  // AWAY from the reader.
  if (!(scrollTop > prevScrollTop + 0.5) || dist > FOLLOW_REENGAGE_PX) return stick
  return arrivalIsTheirs ? true : stick
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
 *   - Sticking and RESTING on our last write (`|scrollTop - lastWriteTop| <=
 *     epsilon`) → pin: the reader never left the end, so whatever opened the
 *     gap under them was content (a new message, a reprice), live turn or not.
 *   - Sticking, off our write, and nothing running → release stick, don't pin:
 *     with no output to follow, a reader above the bottom is one who left it.
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
   *  Follow means "keep me at the end of the transcript". A reader RESTING on
   *  our own last write is at that end whether or not a turn is live, and is
   *  carried by the resting rule below without consulting this flag. This flag
   *  decides the OTHER reader: one whose scrollTop has left our write while
   *  nothing runs. With no output to follow, that reader is not following —
   *  and an automatic pin there is a yank with no cause, reported from a phone
   *  as the transcript springing back after scrolling up about a hundred
   *  pixels with nothing streaming.
   *
   *  Defaults to `true` = assume a run is live, which keeps the behaviour of a
   *  caller that has no run signal to give (the app-SDK chat surface). The chat
   *  transcript passes the real thing. */
  runActive?: boolean
  /** Is an anchor restore currently OWNING the scroll position?
   *
   *  A restore places the reader at an absolute offset and then re-lands it as
   *  measurements arrive. An automatic pin during that window is a second owner
   *  writing the same scroller, and the two fight: captured on a phone as
   *  `WRITE autopin 3091->4245` answered by `WRITE settle 4245->3091`, twice in
   *  120ms, 1,154px each way. The settle won those rounds, but only because its
   *  budget had not expired yet -- which is why the same switch sometimes landed
   *  at the bottom and sometimes did not.
   *
   *  Released rather than merely skipped, for the reason the idle branch below
   *  gives: skipping leaves follow armed, so the next growth yanks the reader
   *  from wherever the restore just put them. */
  restoreGate?: boolean
}): AutoPinResult {
  const { stick, geom, lastWriteTop } = args
  const epsilon = args.epsilon ?? SELF_SCROLL_EPSILON
  const viewportShrink = Math.max(0, args.viewportShrink ?? 0)
  const runActive = args.runActive ?? true
  const target = bottomTarget(geom)
  if (args.restoreGate) return { pin: false, stick: false, target }
  if (!stick) return { pin: false, stick: false, target }
  // RESTING RULE. The reader is exactly where we last put them -- a pin, the
  // clamp that re-baselined the reference, or their own return to the bottom
  // that the scroll handler recorded as the place they chose to follow from --
  // so any gap between them and the end was opened by CONTENT: a new message
  // landing, a row settling from its estimate, a code-block stand-in swapping
  // for the highlighted block, the top spacer repricing. That gap is one we owe
  // them, live turn or not. Carry them back.
  //
  // Position is the whole test; hardware input is deliberately NOT consulted.
  // A wheel-down at the end, a finger that landed and lifted, a scrollbar grab
  // that went nowhere all stamp input and move nothing, and a reader who did
  // any of those is still at the bottom. Reading that input as "the reader
  // left" is how a crewmate's reply landing in an idle DM stopped following a
  // reader who had done nothing but wheel at the end -- they had to scroll by
  // hand to see it. A reader who DID leave is not resting: their scroll moved
  // scrollTop off our write, and the scroll handler released follow on the way
  // (the guard further down catches the race where the height commit lands
  // before that scroll event; the frame between an UPWARD input and its scroll
  // event, where the reader has not moved YET, is the caller's to hold --
  // `scrollIntentPending`, checked in pinAuto before this predicate runs, for
  // an upward input and for a pointer that grabbed the scrollbar). A
  // programmatic reveal -- a search hit, a pinned prompt, find-in-page -- moves
  // scrollTop off our write too, so it is never mistaken for content settling
  // under a still reader. The old `readerMovedSinceWrite` term ("any input
  // since the last pin") is gone for good, not merely narrowed: the per-scroll
  // re-baseline in the scroll handler makes position sufficient, and an
  // any-input term cannot tell a no-op wheel from a departure.
  const restingOnOurWrite = lastWriteTop >= 0 && Math.abs(geom.scrollTop - lastWriteTop) <= epsilon
  if (restingOnOurWrite) {
    return { pin: distanceFromBottom(geom) > atBottomEpsilon(), stick: true, target }
  }
  // Idle: release rather than merely skip the pin. Skipping would leave follow
  // armed, so the next turn to start would yank this reader to the bottom from
  // wherever they had settled — the same defect one event later.
  //
  // Reaching here means scrollTop has LEFT our last write (or nothing has been
  // written this session) while nothing runs. Distance alone cannot say WHO
  // opened a gap, but the one reader the CONTENT moved is already answered
  // above by position, so a reader who is above the bottom here is one who
  // left it -- or one a reveal placed -- and is not ours to move.
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


/**
 * Does ONE row answer to this anchor, in either identity?
 *
 * Neither end of a row is stable: appends rename the tail, and a landing page that
 * regroups messages into the head renames the lead. So an anchor carries both, and
 * anything that asks "is this the anchored row" has to accept either -- otherwise a
 * row found through `alt` fails the next check by construction, because `alt` only
 * matched at all when the tail did not.
 *
 * The two prefixes make cross-matching impossible, so accepting both cannot widen a
 * match; it only stops a resolved row from being disowned one step later.
 */
export function anchorMatchesRow(input: {
  anchor: { key: string; alt?: string }
  tailId: string | null
  altId: string | null
}): boolean {
  const { anchor, tailId, altId } = input
  if (tailId !== null && tailId === anchor.key) return true
  return !!anchor.alt && altId !== null && altId === anchor.alt
}

/**
 * Resolve a persisted anchor to a row index, matching EITHER identity.
 *
 * A row is named by one of its member messages, and a turn's membership changes
 * at both ends: streaming appends rename its tail, an older page landing
 * regroups messages into its head and renames its lead. So a single identity is
 * reliable only against the growth direction it was chosen for -- and a switch
 * into a live turn does both at once, which is how a restore came to miss and
 * fall back to the bottom every time.
 *
 * TAIL FIRST, as a whole pass. The tail id is the stronger signal (it is the one
 * a page landing cannot rename), so an alt match must never win over a tail
 * match on a different row -- which interleaving the two comparisons per row
 * would allow. The two vocabularies carry different prefixes, so a cross-match
 * is impossible by construction rather than by ordering alone.
 */
export function resolveAnchorRow(input: {
  count: number
  anchor: { key: string; alt?: string }
  tailIdAt: (i: number) => string | null
  altIdAt: (i: number) => string | null
}): number {
  const { count, anchor, tailIdAt, altIdAt } = input
  for (let i = 0; i < count; i++) {
    if (tailIdAt(i) === anchor.key) return i
  }
  if (!anchor.alt) return -1
  for (let i = 0; i < count; i++) {
    if (altIdAt(i) === anchor.alt) return i
  }
  return -1
}


/**
 * Has an anchor restore finished landing?
 *
 * Two conditions, and the second one is the subtle half. The row must sit where
 * the anchor says (`delta`), AND the thing CAUSING the corrections must have
 * stopped -- otherwise "in tolerance right now" declares victory mid-measurement,
 * observed as ok at frame 1 (d=0.5) followed by a further +49px at frame 3: 49px
 * of visible hop just after the cover lifted.
 *
 * The cause is height arriving ABOVE the anchor (rows above it repricing from
 * their estimates), which is NOT the same as the transcript growing. Testing the
 * whole `scrollHeight` conflates the two, and during a live turn the difference
 * is total: appends land BELOW the anchor and do not move it at all, yet they
 * change the total height on every frame -- so convergence became unreachable and
 * every restore into a streaming session burned the entire budget with the
 * skeleton up, however early it had actually landed.
 *
 * `aboveDelta` is the change in the anchor's own content offset since the last
 * frame. It has the property this needs: OUR corrective write moves `scrollTop`
 * by exactly the delta it corrects, so it leaves that offset unchanged -- the loop
 * cannot mistake its own action for instability -- while measurement arriving
 * above moves it without us touching `scrollTop`.
 */
export function anchorSettleConverged(input: {
  delta: number
  aboveDelta: number
  tolerance: number
  /** False on the first frame, where there is no previous offset to compare. */
  hasPrevious: boolean
}): boolean {
  if (!input.hasPrevious) return false
  if (Math.abs(input.delta) > input.tolerance) return false
  return Math.abs(input.aboveDelta) <= input.tolerance
}
