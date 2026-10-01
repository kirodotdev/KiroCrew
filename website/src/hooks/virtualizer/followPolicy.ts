// Follow / pin / reader-intent policy for the chat virtualizer.
//
// FOLLOW / STICK-TO-BOTTOM
// ========================
// A single `stickRef` boolean is the source of truth for "keep the viewport
// pinned to the bottom". The decision logic lives in FollowController as pure
// functions and is race-proof against the ResizeObserver-vs-scroll-event
// ordering — see that module's header for the rationale. This module owns the
// state those decisions read and write, and every code path that MOVES the
// scroller on follow's behalf:
//   - automatic pins (the resize observer, the append layout effect, the
//     pre-paint re-pin of a height commit) → `pinAuto()` / `prePaintRepin()`
//   - explicit pins and navigation (slot entry, scrollToBottom,
//     scrollToIndex) → `forcePin()` and the imperative API
//
// INVARIANT — every programmatic `scrollTop` write MUST record itself in
// `lastWriteTopRef`, which is why `writeScrollTop` below is the only place a
// raw write is allowed. Read this before adding any code that moves the
// scroller.
//
// The stick-release guard distinguishes "the user scrolled" from "we scrolled"
// by comparing live `scrollTop` against the value we last wrote. An unrecorded
// write therefore looks exactly like user input and releases follow. The guard
// is reliable only because pins are instant, so there is no in-flight animation
// to desynchronise the reference. That makes the invariant load-bearing rather
// than hygienic: the anchor-compensation write has to honour it too, and so must
// any future one.

import { useCallback, useLayoutEffect, useRef, type MutableRefObject, type RefObject } from 'react'
import { attachUserScrollIntent, type ScrollIntentDirection } from '../../utils/searchScroll'
import {
  SCROLL_SETTLE_MS,
  SELF_SCROLL_EPSILON,
  bottomTarget,
  evaluateAutoPin,
  isSelfScroll,
  pinSuppressedNow,
  resolveUserScrollStick,
  scrollerCollapsed,
  scrollIntentPending,
  type ScrollGeom,
} from './FollowController'
import { computeJumpWindow, getOffset as getOffsetFn, tailWindow, type HeightGetter, type WindowRange } from './WindowCalculator'
import type { ScrollToIndexOptions } from './types'
import type { ReadingPositionEntry } from './readingPosition'
import type { ResizeBatch } from './measurement'
import { devLog, inspectorOn } from '../../dev/scrollInspector'

/** How a programmatic write must be accounted for by the follow guard. */
export type WriteScrollTop = (
  el: HTMLDivElement,
  top: number,
  behavior: ScrollBehavior,
  accounting: 'pin' | 'release',
  who?: string,
) => void

type Ref<V> = MutableRefObject<V>
type SetWindowRange = (next: WindowRange | ((prev: WindowRange) => WindowRange)) => void

/** The follow state every other owner reads and, at named sites, writes. */
export interface FollowState {
  stickRef: Ref<boolean>
  lastWriteTopRef: Ref<number>
  /** Target of the last programmatic write, whatever its accounting. Only
   *  writeScrollTop sets it, so the follow handler never re-baselines it. */
  lastProgrammaticTopRef: Ref<number>
  lastWriteClientHRef: Ref<number>
  smoothPinActiveRef: Ref<boolean>
  prevSmoothTopRef: Ref<number>
  lastUserScrollAtRef: Ref<number>
  lastHardInputAtRef: Ref<number>
  /** Hard input that named a direction (wheel delta, touch drag, scrolling key).
   *  A tap, a scrollbar grab or a zero-delta wheel names none and moves nothing. */
  lastDirectionalInputAtRef: Ref<number>
  lastUpwardInputAtRef: Ref<number>
  lastGrabInputAtRef: Ref<number>
  lastScrollEventAtRef: Ref<number>
  lastScrollClientHRef: Ref<number>
  lastObservedTopRef: Ref<number>
  pinCascadeUntilRef: Ref<number>
  detachSmoothAbort: () => void
  releaseFollowBaseline: () => void
  writeScrollTop: WriteScrollTop
  getFollow: () => boolean
  /** Hardware scroll intent (wheel / touch / scrollbar grab / scrolling key). */
  noteHardInput: (dir?: ScrollIntentDirection) => void
}

export function useFollowState(followOutput: boolean): FollowState {
  // ---- Follow / stick-to-bottom state (see FollowController) ----
  //
  // `stickRef`: should the viewport stay pinned to the bottom. Turned OFF only
  // by a genuine user scroll-up; turned ON only by the user returning to the
  // bottom or an explicit/forced pin (slot entry, scrollToBottom).
  //
  // `lastWriteTopRef`: the scrollTop value we last WROTE programmatically.
  // `-1` means "nothing written this session" (resets the race guard on slot
  // switch). Used to (a) recognise our own scroll events and (b) detect, at
  // pin time, that the user scrolled up since our last write — synchronously,
  // beating the RO-vs-scroll-event race.
  const stickRef = useRef<boolean>(followOutput)
  const lastWriteTopRef = useRef<number>(-1)
  // `lastWriteClientHRef`: the scroller's `clientHeight` at the moment
  // `lastWriteTopRef` was recorded — i.e. the viewport box that value was a
  // bottom FOR. Kept in lockstep with it (`-1` alongside `-1`) so the pin
  // evaluation can tell how much of the current distance-from-bottom is our
  // own viewport shrink rather than the user's move (see evaluateAutoPin's
  // `viewportShrink`).
  const lastWriteClientHRef = useRef<number>(-1)
  // True while a smooth scrollTo animation (from pinAuto) is in flight.
  // During this period, scroll events are NOT treated as user-scrolls — they
  // are intermediate frames of our own programmatic smooth-pin.
  const smoothPinActiveRef = useRef(false)
  // Previous scrollTop during smooth-pin animation. Used to detect genuine
  // user scroll-up (scrollTop decreased) vs normal forward animation progress.
  const prevSmoothTopRef = useRef(0)
  // Detaches the current smooth-glide abort listeners. Held in a ref so the
  // glide can be torn down from wherever it ends: user input, natural arrival
  // at the bottom, a replacing glide, or unmount.
  const smoothAbortDetachRef = useRef<(() => void) | null>(null)
  const detachSmoothAbort = useCallback(() => {
    smoothAbortDetachRef.current?.()
  }, [])
  // Timestamp (performance.now) of the last genuine USER scroll. Used to gate
  // RO-driven follow pins so they don't fire mid-fling — see SCROLL_SETTLE_MS.
  // Starts at -Infinity: "no input yet" must never read as "input just
  // happened" (performance.now() can legitimately be near 0 early in a page's
  // life, and is under fake timers in tests).
  const lastUserScrollAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  // Hard-input-only sibling of lastUserScrollAtRef (see
  // ANCHOR_SAVE_INTENT_WINDOW_MS): written ONLY by attachUserScrollIntent's
  // hardware events and by the smooth-pin grab interrupts, never by scroll
  // events themselves.
  const lastHardInputAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  const lastProgrammaticTopRef = useRef<number>(-1)
  const lastDirectionalInputAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  // UPWARD-only sibling of lastHardInputAtRef: stamped when the input's own
  // direction was up (wheel up / upward key / upward touch drag), or when a
  // smooth-glide grab moved scrollTop backward (confirmed upward by motion).
  // resolveUserScrollStick's clamp branch keys its release on THIS stamp, not
  // the direction-blind one: a wheel-down at the bottom is an ordinary input
  // during streaming, and a content-shrink clamp inside its settle window must
  // keep follow armed rather than releasing the reader who asked for the end.
  const lastUpwardInputAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  // A pointer landing on the SCROLLBAR (the intent listener's `grab`): a scroll
  // is about to happen and nothing names its direction until the first drag
  // movement scrolls. Held like an upward input until that scroll event (see
  // pinAuto), because a drag that starts upward looks exactly like the upward
  // sub-frame race -- the reader still on our write, the pin about to land on
  // top of the gesture. NOT stamped for a pointer anywhere else in the
  // scroller: selecting text or clicking a link moves nothing, and holding on
  // every click would turn the transcript's own surface into a follow brake.
  const lastGrabInputAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  // When the scroller last dispatched a scroll event, of ANY origin. Compared
  // against the upward and grab stamps above it answers "has the reader's
  // scroll landed yet": a stamp newer than the last scroll event is intent
  // whose effect on `scrollTop` is still in flight, and `scrollIntentPending`
  // holds the automatic pin off until it lands or expires (see pinAuto).
  // Stamped by the scroll handler before anything else reads it.
  const lastScrollEventAtRef = useRef<number>(Number.NEGATIVE_INFINITY)
  // Whether the reader has LEFT the bottom is answered by position alone --
  // `lastWriteTopRef` against live scrollTop in evaluateAutoPin's resting rule
  // -- never by whether a hardware input has been stamped since our last write.
  // An input stamp cannot tell a wheel-down at the end that moved nothing from
  // a scroll-up that did, and reading every stamp as "the reader left" is how a
  // complete message landing in an idle chat stopped following a reader who was
  // still at the end.
  // Follow was RELEASED by the reader: there is no write of ours they are
  // resting on any more, so drop the self-scroll reference with it. Left in
  // place, it kept pointing at the bottom we last pinned -- and a reader who
  // scrolls back down lands on exactly that pixel, because it is still the
  // maximum scrollTop. That arrival then read as our own scroll, the handler
  // skipped its re-engagement branch, and follow never re-armed: the next turn
  // streamed past a reader who had put themselves at the end to watch it. The
  // coverage watchdog's per-tick forcePin used to paper over this by re-arming
  // follow every 500ms; with that misfire gone the reference has to be honest.
  const releaseFollowBaseline = useCallback(() => {
    lastWriteTopRef.current = -1
    lastWriteClientHRef.current = -1
  }, [])
  // Scroller height as of the last SCROLL event. Deliberately not
  // `viewportHeightRef`, which the ResizeObserver updates: the resize and the
  // clamp it causes are two separate events, and if the observer ran first the
  // growth would already be folded away by the time the clamp's scroll event
  // asked about it.
  const lastScrollClientHRef = useRef(0)
  // scrollTop as of the last observed scroll event (self or user). Gives the
  // user-scroll stick decision its direction: a genuine upward move releases
  // follow even inside the 100px at-bottom band. `-1` = no observation yet.
  const lastObservedTopRef = useRef<number>(-1)
  // Deadline for cascade-extended pin suppression — see pinSuppressedNow.
  const pinCascadeUntilRef = useRef<number>(0)

  // Live follow state for consumers. A stable callback rather than state:
  // `stick` flips inside hot paths (scroll handler, RO callback) where a
  // setState per tick would be waste, and the consumers are effect gates that
  // need the CURRENT value at fire time, not a render-synced snapshot.
  const getFollow = useCallback(() => stickRef.current, [])

  // Hardware-intent stamps, fed by the scroller's persistent intent listeners
  // (see the scroll listener in observers.ts). They only stamp time -- the stick
  // decision itself stays with the scroll handler.
  const noteHardInput = useCallback((dir?: ScrollIntentDirection) => {
    lastUserScrollAtRef.current = performance.now()
    lastHardInputAtRef.current = performance.now()
    // Only a confirmed upward input arms the clamp-release stamp — a
    // directionless grab or a downward input must not disable the clamp
    // guard (see lastUpwardInputAtRef).
    if (dir === 'up') lastUpwardInputAtRef.current = performance.now()
    if (dir === 'up' || dir === 'down') lastDirectionalInputAtRef.current = performance.now()
    // A scrollbar grab arms its own hold (see lastGrabInputAtRef); it is still
    // directionless for the clamp guard above.
    if (dir === 'grab') lastGrabInputAtRef.current = performance.now()
  }, [])

  // ---- The single chokepoint for programmatic scroll writes ----
  //
  // Enforces the follow invariant STRUCTURALLY rather than by convention: you
  // cannot move the scroller without stating how the follow guard should account
  // for it, because `accounting` is a required argument.
  //   - 'pin'     — we are pinning; the guard remembers this position, so the
  //                 resulting scroll event is recognised as our own.
  //   - 'release' — we are deliberately leaving the bottom (explicit
  //                 navigation); reset the guard sentinel, follow is off anyway.
  // An unaccounted write is indistinguishable from user input and would release
  // follow spuriously. Making the argument mandatory means a future contributor
  // has to make a choice rather than forget one.
  const writeScrollTop = useCallback(
    (
      el: HTMLDivElement,
      top: number,
      behavior: ScrollBehavior,
      accounting: 'pin' | 'release',
      // Which code path is moving the reader. Diagnostic only -- it changes no
      // behaviour -- but fourteen call sites write this scroller and the log
      // could not tell them apart, so a position that ends up somewhere nobody
      // intended cannot be attributed to the write that put it there.
      who?: string,
    ) => {
      if (inspectorOn()) devLog('WRITE', `${who ?? '?'} ${Math.round(el.scrollTop)}->${Math.round(top)}${behavior === 'smooth' ? ' smooth' : ''}`)
      if (typeof el.scrollTo === 'function') el.scrollTo({ top, behavior })
      else el.scrollTop = top
      lastWriteTopRef.current = accounting === 'pin' ? top : -1
      lastProgrammaticTopRef.current = top
      lastWriteClientHRef.current = accounting === 'pin' ? el.clientHeight : -1
      // The direction reference must move WITH our own writes, synchronously.
      // A programmatic scroll's event lands asynchronously (and a fake scroller
      // in tests dispatches none), so leaving the reference to the scroll
      // handler alone would measure the user's next move against a position
      // from BEFORE our pin — an upward scroll right after a pin then reads as
      // downward and fails to release follow.
      lastObservedTopRef.current = top
      // A SMOOTH pin animates toward `top` over many frames, and every
      // intermediate scroll event carries a scrollTop that differs from the
      // recorded target — so the passive listener would read those frames as
      // user input and release follow, and a mid-animation append would then be
      // skipped by auto-pin, landing short of the new bottom. Arm the
      // smooth-pin guard so the listener tolerates the glide (it disarms on
      // arrival, or on a genuine upward move: see the scroll handler).
      //
      // Only the explicit "jump to latest" path is smooth; the streaming pin
      // is instant, so this guard only needs to cover the jump-to-latest glide.
      if (accounting === 'pin' && behavior === 'smooth') {
        smoothPinActiveRef.current = true
        prevSmoothTopRef.current = el.scrollTop
        // ...but the guard must yield to REAL input. Its only other release
        // condition is "scrollTop moved backward", which a wheel cannot satisfy
        // while a fast animation is still driving scrollTop forward — so a user
        // wheeling up mid-glide was ignored and still ended up pinned to the
        // bottom (verified in a real browser). A one-shot input listener
        // disarms the guard and releases follow, matching how the jump/search
        // convergence polls already abort on user input.
        const abort = () => {
          // Stale-invocation guard: if the glide already finished, these
          // listeners are leftovers — detach and do nothing. Without this a
          // completed jump left handlers behind that a later, unrelated wheel
          // would fire, releasing follow while no smooth scroll was active.
          if (!smoothPinActiveRef.current) {
            detachSmoothAbort()
            return
          }
          smoothPinActiveRef.current = false
          stickRef.current = false
          lastUserScrollAtRef.current =
            typeof performance !== 'undefined' ? performance.now() : Date.now()
          lastHardInputAtRef.current = lastUserScrollAtRef.current
          // Releasing `stick` alone is not enough: the browser's NATIVE smooth
          // animation keeps running and would still land at the bottom, so the
          // user's input appears ignored. Re-issuing an instant scroll to the
          // CURRENT position cancels the in-flight animation and freezes where
          // they are. lastWriteTop is reset because we are releasing follow.
          if (typeof el.scrollTo === 'function') el.scrollTo({ top: el.scrollTop, behavior: 'auto' })
          lastWriteTopRef.current = -1
          lastWriteClientHRef.current = -1
          detachSmoothAbort()
        }
        // Replace any previous glide's listeners rather than stacking them:
        // repeated jump-to-latest presses would otherwise accumulate handlers.
        // attachUserScrollIntent is the shared input set, so a scrollbar drag
        // or a keyboard scroll aborts the glide too — wheel/touch alone let the
        // animation override both.
        detachSmoothAbort()
        const detachIntent = attachUserScrollIntent(el, abort)
        smoothAbortDetachRef.current = () => {
          detachIntent()
          smoothAbortDetachRef.current = null
        }
      }
    },
    [detachSmoothAbort],
  )

  return {
    stickRef,
    lastWriteTopRef,
    lastProgrammaticTopRef,
    lastDirectionalInputAtRef,
    lastWriteClientHRef,
    smoothPinActiveRef,
    prevSmoothTopRef,
    lastUserScrollAtRef,
    lastHardInputAtRef,
    lastUpwardInputAtRef,
    lastGrabInputAtRef,
    lastScrollEventAtRef,
    lastScrollClientHRef,
    lastObservedTopRef,
    pinCascadeUntilRef,
    detachSmoothAbort,
    releaseFollowBaseline,
    writeScrollTop,
    getFollow,
    noteHardInput,
  }
}

export interface Pinning {
  pinAuto: () => void
  forcePin: () => void
  scrollToBottom: (behavior?: ScrollBehavior) => void
  scrollToIndex: (index: number, options?: ScrollToIndexOptions) => void
  /** The stick section of the scroll handler: a scroll event's effect on follow. */
  onFollowScroll: (el: HTMLDivElement, geom: ScrollGeom) => void
  /** Drop a pin retry held for in-flight scroll intent. The scroll listener
   *  calls it when it detaches from the scroller, since the intent stamps the
   *  retry waits on come from that listener's own input hooks. */
  cancelHeldPinRetry: () => void
  /** Re-target the bottom before paint when a height commit lands under a followed reader. */
  prePaintRepin: (el: HTMLDivElement) => void
  /** The resize observer's follow decision for one batch of entries. */
  followResizeBatch: (batch: ResizeBatch) => void
}

export function usePinning<T>(ctx: {
  followOutput: boolean
  overscan: number
  scrollerRef: RefObject<HTMLDivElement | null>
  itemsRef: Ref<T[]>
  runActiveRef: Ref<boolean | undefined>
  getH: HeightGetter
  setWindowRange: SetWindowRange
  follow: FollowState
  reading: Pick<ReadingPositionEntry, 'settleGateRef' | 'restoreOwnsPosition'>
}): Pinning {
  const { followOutput, overscan, scrollerRef, itemsRef, runActiveRef, getH, setWindowRange } = ctx
  const {
    stickRef,
    lastWriteTopRef,
    lastWriteClientHRef,
    smoothPinActiveRef,
    prevSmoothTopRef,
    lastUserScrollAtRef,
    lastHardInputAtRef,
    lastUpwardInputAtRef,
    lastGrabInputAtRef,
    lastScrollEventAtRef,
    lastScrollClientHRef,
    lastObservedTopRef,
    pinCascadeUntilRef,
    detachSmoothAbort,
    releaseFollowBaseline,
    writeScrollTop,
  } = ctx.follow
  const { settleGateRef, restoreOwnsPosition } = ctx.reading

  // A pin HELD for in-flight scroll intent (see pinAuto) is not a pin skipped:
  // if the intent never scrolls -- the reader clicked the scrollbar thumb
  // without dragging, wheeled up on a transcript shorter than its viewport --
  // no scroll event ever retires it and nothing else would re-run the pin the
  // append asked for. So the hold schedules one retry for the moment the
  // intent expires; pinAuto then re-evaluates against the position, and the
  // scroll handler has had every chance to release `stick` first. One timer at
  // a time (a burst of appends during one hold retries once). It is cancelled
  // from the scroll listener's teardown (observers.ts) -- the lifecycle that
  // owns the intent stamps it waits on -- so this owner adds no effect of its
  // own (the module's effect sequence is pinned: leading chrome, append pin).
  // `pinAutoRef` lets the timer reach the latest pinAuto without the callback
  // naming itself in its own deps.
  const pinAutoRef = useRef<() => void>(() => {})
  const heldPinRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scheduleHeldPinRetry = useCallback((delayMs: number) => {
    if (heldPinRetryRef.current !== null) return
    heldPinRetryRef.current = setTimeout(() => {
      heldPinRetryRef.current = null
      pinAutoRef.current()
    }, Math.max(1, delayMs))
  }, [])
  const cancelHeldPinRetry = useCallback(() => {
    if (heldPinRetryRef.current !== null) clearTimeout(heldPinRetryRef.current)
    heldPinRetryRef.current = null
  }, [])

  // ---- Pin helpers (the only code that writes el.scrollTop for follow) ----

  // Automatic pin: called when content changed (RO / append / streaming).
  // DELEGATES the decision to FollowController.evaluateAutoPin — the pure,
  // unit-tested race-proof core. evaluateAutoPin reads the LIVE geometry and
  // (a) never pins when stick is released, (b) releases stick synchronously if
  // the user has scrolled up since our last write (scrollTop < lastWriteTop and
  // still away from the bottom — the distance guard tolerates mid-stream
  // shrink), and (c) otherwise pins to the bottom. Its at-bottom test uses the
  // DPR-aware epsilon, so this and the delegated core share one gate.
  //
  // The pin WRITE is INSTANT (behavior:'auto'), not smooth: a streaming
  // response grows the bottom target every token, and a fresh smooth scroll
  // CANCELS the in-flight one and restarts toward the moving target, so on a
  // tall transcript it chases the bottom and never converges. Smooth is
  // reserved for the explicit "jump to latest" path (scrollToBottom).
  //
  // The synchronous scroll-up release is reliable only with the instant write:
  // there is no animation lag, so scrollTop == lastWriteTop right after each pin.
  const pinAuto = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    // An in-flight smooth pin is OUR scroll, and mid-glide `scrollTop` sits
    // below the recorded target while still being meaningfully away from the
    // bottom — which is exactly evaluateAutoPin's user-scroll-up signature. A
    // ResizeObserver tick during the glide (streaming output resizes constantly)
    // therefore released follow and left the rest of the response behind. The
    // scroll handler already exempts in-flight glides; this path did not.
    //
    // Preserve follow and do NOT write: re-issuing a smooth scroll every resize
    // tick would cancel and restart the animation each time. Content appended
    // mid-glide is instead re-targeted the
    // moment the glide lands — the arrival branch of the scroll handler runs
    // pinAuto(), which then snaps instantly to the new bottom.
    if (smoothPinActiveRef.current) return
    // A collapsed box (backgrounded mobile tab, display:none pane) has no
    // reader in it, and its geometry lies -- see scrollerCollapsed. Skip the
    // evaluation entirely; the visibility-return handler (useVisibilityReplacement) re-places once
    // the box has a real height again.
    if (scrollerCollapsed(el)) return
    // Scroll intent whose scroll event has not dispatched yet -- an UPWARD
    // wheel/key/touch, or a pointer that landed on the SCROLLBAR and is about
    // to drag: the reader is still on our last write to the pixel, so
    // evaluateAutoPin's position test would read them as resting, and an
    // append landing in this frame -- the one caller of this function with no
    // settle gate of its own (the growth layout effect in
    // useFollowPlacementPins) -- would pin them to the bottom against the
    // scroll they have just begun. Hold the pin and leave `stick` to the
    // scroll event, which releases it if the reader moved up and re-baselines
    // if they did not; retry once when the intent expires, in case it never
    // scrolls (see scheduleHeldPinRetry). Downward and directionless input is
    // NOT held: a wheel-down at the end, a click on a message, a finger that
    // landed and lifted move nothing and must not stop a reply from being
    // followed (the regression the resting rule fixes).
    const now = performance.now()
    const heldFor = Math.max(
      scrollIntentPending(now, lastUpwardInputAtRef.current, lastScrollEventAtRef.current, SCROLL_SETTLE_MS),
      scrollIntentPending(now, lastGrabInputAtRef.current, lastScrollEventAtRef.current, SCROLL_SETTLE_MS),
    )
    if (heldFor > 0) {
      scheduleHeldPinRetry(heldFor)
      return
    }
    const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
    const viewportShrink =
      lastWriteClientHRef.current >= 0 ? lastWriteClientHRef.current - geom.clientHeight : 0
    const result = evaluateAutoPin({
      stick: stickRef.current,
      geom,
      lastWriteTop: lastWriteTopRef.current,
      // Undefined = the caller gave no run signal, which the predicate reads as
      // "assume live" so an unaware caller keeps its behaviour.
      runActive: runActiveRef.current,
      restoreGate: settleGateRef.current,
      // Chrome mounting below the transcript shrinks this box, often
      // spring-animated across many frames. Measure the scroll-up guard
      // against the box our reference was a bottom for, never the box the
      // animation just applied.
      viewportShrink,
    })
    const wasStick = stickRef.current
    stickRef.current = result.stick
    if (wasStick && !result.stick) releaseFollowBaseline()
    if (result.pin) {
      writeScrollTop(el, result.target, 'auto', 'pin', 'autopin')
    } else if (result.stick) {
      // Still following but already at the bottom (no write needed) — keep the
      // self-scroll reference aligned with the current bottom.
      lastWriteTopRef.current = result.target
      lastWriteClientHRef.current = geom.clientHeight
    }
  }, [
    scrollerRef, writeScrollTop, releaseFollowBaseline,
    smoothPinActiveRef, lastWriteClientHRef, stickRef, lastWriteTopRef, runActiveRef, settleGateRef,
    lastUpwardInputAtRef, lastGrabInputAtRef, lastScrollEventAtRef, scheduleHeldPinRetry,
  ])
  pinAutoRef.current = pinAuto

  // Forced pin: explicit jump-to-bottom (slot entry, scrollToBottom API,
  // jump-to-latest pill). Always lands at the bottom and (re-)arms follow.
  const forcePin = useCallback(() => {
    const el = scrollerRef.current
    if (!el) return
    stickRef.current = followOutput
    const target = bottomTarget({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight })
    writeScrollTop(el, target, 'auto', 'pin', 'forcepin')
  }, [followOutput, scrollerRef, writeScrollTop, stickRef])

  // FOLLOWED reader under a height commit: re-target the bottom PRE-PAINT in the
  // same commit that repriced the tree. The height-sync anchor consumer calls
  // this in place of its anchor correction whenever follow is armed; it is the
  // pre-paint half of the one automatic-pin decision pinAuto makes post-paint.
  const prePaintRepin = useCallback((el: HTMLDivElement) => {
    // A collapsed box is no place to re-target a bottom from: its
    // `bottomTarget` is `scrollHeight` itself, and the write would be clamped
    // -- and read as a user scroll -- the moment the box comes back. Same
    // predicate as pinAuto, deliberately (see scrollerCollapsed).
    if (scrollerCollapsed(el)) return
    // FOLLOWED reader: re-target the bottom PRE-PAINT in the same commit
    // that repriced the tree. pinAuto also does this, but post-paint --
    // which leaves ONE visible frame when a large reprice lands (idle
    // prefetch pages priced by a whale-skewed estimate, then collapsed
    // to farm-measured truth: the bottom rig recorded the spacer swinging
    // tens of thousands of px and the pinned reader teleporting with the
    // clamp -- the field report's parked-at-bottom self-bounce). Writing
    // here makes the whole reprice invisible to a bottom-pinned reader.
    //
    // SETTLE GATE, same principle as SCROLL_SETTLE_MS on the RO auto-pin:
    // a reader who has just started scrolling UP is still `stick` for the
    // few ms until the scroll handler's rAF evaluates and releases follow.
    // A repricing batch committing inside that gap would force them to the
    // bottom mid-gesture -- reported as "I was reading mid-transcript and it
    // suddenly jumped to the end". Real hardware input within the window
    // means the reader's own decision is in flight and outranks this
    // correction; the post-paint pinAuto still handles the genuinely-parked
    // case a frame later.
    if (pinSuppressedNow(performance.now(), lastHardInputAtRef.current, pinCascadeUntilRef.current, SCROLL_SETTLE_MS)) return
    const geom = { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }
    // A SHRINKING viewport is the composer growing under the reader's own
    // typing. The bottom moves DOWN by the shrink without anyone scrolling, so
    // re-targeting it walks the transcript up a line every few characters —
    // reported as the transcript springing back to the bottom while typing a
    // short way above it. `pinSuppressedNow` cannot catch this: the hardware
    // intent listeners are on the SCROLLER, and a keystroke in the composer
    // never reaches them. Growth (composer collapsing, keyboard closing) still
    // pins: that space is being given back.
    if (lastWriteClientHRef.current >= 0 && geom.clientHeight < lastWriteClientHRef.current) return
    // Delegate to the same predicate the post-paint pin uses instead of
    // hand-rolling a second gate here: this branch is the pre-paint half of one
    // decision, and a private copy of it is how the idle rule (`runActive`)
    // came to cover one half and not the other.
    const decision = evaluateAutoPin({
      stick: stickRef.current,
      geom,
      lastWriteTop: lastWriteTopRef.current,
      runActive: runActiveRef.current,
      restoreGate: settleGateRef.current,
    })
    const wasStick = stickRef.current
    stickRef.current = decision.stick
    if (wasStick && !decision.stick) releaseFollowBaseline()
    if (!decision.pin) return
    if (Math.abs(el.scrollTop - decision.target) > 0.5) writeScrollTop(el, decision.target, 'auto', 'pin', 'prepin')
  }, [
    writeScrollTop, releaseFollowBaseline,
    stickRef, lastHardInputAtRef, pinCascadeUntilRef, lastWriteClientHRef, lastWriteTopRef, runActiveRef, settleGateRef,
  ])

  const onFollowScroll = useCallback((el: HTMLDivElement, geom: ScrollGeom) => {
    // Every scroll event, ours or the reader's, retires the in-flight upward
    // intent (see lastScrollEventAtRef): from here on the position IS the
    // reader's answer, and the decisions below read it.
    lastScrollEventAtRef.current = performance.now()
    // Only a genuine USER scroll updates stick. Our own programmatic pins
    // fire scroll events too; isSelfScroll filters them out so they never
    // flip stick. (Releasing on user scroll-up also happens synchronously
    // inside pinAuto via the live-scrollTop guard — this handler covers the
    // common case and re-arming when the user returns to the bottom.)
    // During a smooth-pin animation, intermediate scroll events are ours —
    // don't treat them as user scrolls.
    if (smoothPinActiveRef.current) {
      // Arrived: the glide is over, so drop its abort listeners.
      //
      // Arrival is measured against the value we actually WROTE
      // (`lastWriteTopRef`), not `atBottom`. `atBottom` uses the 100px UI
      // threshold, which the glide enters while the native animation still
      // has up to 100px to run; disarming there left the remaining animation
      // un-abortable, so a user grabbing the page inside that band would be
      // scrolled to the bottom anyway. `isSelfScroll` compares against the
      // pin target within SELF_SCROLL_EPSILON, so we disarm only once the
      // animation has genuinely landed. `bottomAnchored` is the fallback for
      // a pin whose target was clamped by the browser (a shrinking
      // scrollHeight can leave scrollTop short of the requested value
      // forever, which would otherwise leak the listeners).
      const bottomAnchored =
        geom.scrollHeight - (geom.scrollTop + geom.clientHeight) <= SELF_SCROLL_EPSILON
      if (isSelfScroll(el.scrollTop, lastWriteTopRef.current) || bottomAnchored) {
        smoothPinActiveRef.current = false
        detachSmoothAbort()
        // Content appended DURING the glide moved the bottom, and pinAuto
        // deliberately declined to re-target mid-animation (restarting a
        // smooth scroll every resize tick stutters). Now that the animation
        // has landed, correct the shortfall instantly.
        pinAuto()
      }
      // If the user grabs the page mid-animation and scrolls up,
      // scrollTop moves backward. Normal forward animation progress
      // always increases scrollTop toward the target.
      else if (el.scrollTop < prevSmoothTopRef.current - 1) {
        smoothPinActiveRef.current = false
        lastUserScrollAtRef.current = performance.now()
        lastHardInputAtRef.current = lastUserScrollAtRef.current
        // scrollTop moving backward against the animation IS a confirmed
        // upward gesture, so it also arms the clamp-release stamp.
        lastUpwardInputAtRef.current = lastUserScrollAtRef.current
        stickRef.current = false
        detachSmoothAbort()
      }
      prevSmoothTopRef.current = el.scrollTop
    } else if (!isSelfScroll(el.scrollTop, lastWriteTopRef.current)) {
      // A scroll we did not write that leaves us EXACTLY at the bottom was the
      // layout engine's: the browser clamps scrollTop when a shrinking
      // scrollHeight drops the maximum below it, and a spacer re-estimate does
      // the same.
      //
      // The test is the CLAMP — distance within SELF_SCROLL_EPSILON — and NOT
      // the 100px `atBottom` UI band. resolveUserScrollStick's bottom-epsilon
      // branch is what keeps `stick` armed across the clamp; widening this to
      // the 100px band would erase the only evidence evaluateAutoPin has of a
      // real 3-100px scroll-up.
      const clampedAtBottom =
        geom.scrollHeight - (geom.scrollTop + geom.clientHeight) <= SELF_SCROLL_EPSILON
      const wasStick = stickRef.current
      stickRef.current = resolveUserScrollStick({
        stick: stickRef.current,
        followOutput,
        scrollTop: el.scrollTop,
        prevScrollTop: lastObservedTopRef.current,
        geom,
        // How much viewport came BACK since the previous scroll event. A
        // deletion shrinks the composer, this grows, the maximum scrollTop
        // drops, and the engine clamps a near-bottom reader to the end with no
        // write to see. Without this the clamp reads as the reader returning to
        // the bottom and re-arms follow for someone who never touched it.
        viewportGrowth:
          lastScrollClientHRef.current > 0
            ? geom.clientHeight - lastScrollClientHRef.current
            : 0,
        // An UPWARD hardware input stamped within the settle window is proof
        // the reader scrolled up. The intent listeners stamp its direction
        // BEFORE this scroll event dispatches, so a landing at the bottom
        // under a fresh upward stamp is the reader's own scroll-up coinciding
        // with a content shrink, not the engine's clamp — release follow
        // instead of holding the reader at the end. A downward or
        // directionless input leaves the clamp guard in place.
        upwardInputWithinSettle:
          performance.now() - lastUpwardInputAtRef.current < SCROLL_SETTLE_MS,
      })
      if (wasStick && !stickRef.current) releaseFollowBaseline()
      const layoutClamp = stickRef.current && clampedAtBottom
      // A clamp is OUR layout change, so it must not be stamped as input.
      // `lastUserScrollAtRef` arms the SCROLL_SETTLE_MS gate that holds
      // automatic pins off while a gesture is in flight, so stamping it for a
      // clamp spent the whole window on our own reflow. A send that queues
      // behind a busy turn does both halves at once: the queued row regroups
      // the turn and remounts tail rows (content shrinks — the clamp), and the
      // queue band mounts below the transcript and spring-animates the
      // scroller's box smaller over the following frames. Every one of those
      // viewport re-pins was then suppressed, so the transcript sat up to a
      // card-height below the bottom until the gate expired — measured on the
      // real build: the box shrank 617 -> 588 across 130ms with scrollTop
      // frozen, and the first pin landed at 154ms, one frame AFTER the last
      // shrink step. Whether the animation outlived the gate is what made the
      // defect intermittent.
      //
      // Genuine input keeps its own signal: the persistent intent listeners
      // (useScrollListener in observers.ts attaches them; they stamp through
      // noteHardInput) fire at wheel/touch/key/scrollbar time, which is EARLIER than
      // the scroll event this branch handles, so nothing is lost by declining
      // to stamp here. `stick` and `lastWriteTop` already treat the clamp as
      // ours; the gate now agrees with them.
      if (!layoutClamp) lastUserScrollAtRef.current = performance.now()
      // Re-baseline the self-scroll reference to where this event left a reader
      // whose follow is still armed.
      //
      // For the CLAMP that is where the engine put us — otherwise the reference
      // keeps pointing at our last write, and the next pin evaluation reads that
      // gap as a user scroll-up, releasing follow for the rest of the turn with
      // only a manual scroll back to the bottom able to re-arm it.
      //
      // For the reader's OWN scroll that kept or re-armed follow (a return into
      // FOLLOW_REENGAGE_PX of the bottom, a downward move that stayed
      // following) it is the place they chose to follow FROM. The release that
      // preceded a re-engagement left the reference at -1, and a reference at
      // -1 has nothing for evaluateAutoPin's resting rule to match: a complete
      // message landing while nothing ran then found this reader "off our
      // write" and released them again, so a crewmate's reply arriving in an
      // idle DM stayed below the fold for someone who had just scrolled back
      // down to watch for it. Recording their position makes the next gap that
      // opens under them, with no further scroll of theirs, content's to close.
      if (stickRef.current) {
        lastWriteTopRef.current = el.scrollTop
        lastWriteClientHRef.current = geom.clientHeight
      }
    }
    // Direction reference for the next event — updated for self-scrolls too,
    // so a user move right after our own pin is measured against where the
    // pin actually left the viewport.
    lastObservedTopRef.current = el.scrollTop
    lastScrollClientHRef.current = el.clientHeight
  }, [
    followOutput, pinAuto, detachSmoothAbort, releaseFollowBaseline,
    smoothPinActiveRef, lastWriteTopRef, prevSmoothTopRef, lastUserScrollAtRef, lastHardInputAtRef,
    lastUpwardInputAtRef, lastScrollEventAtRef, stickRef, lastObservedTopRef, lastScrollClientHRef, lastWriteClientHRef,
  ])

  const followResizeBatch = useCallback((batch: ResizeBatch) => {
    const { genuineResize, tailRowResized, firstMount, viewportResized, trailingChromeResized } = batch
    // Follow streaming/widget growth — but only while the user is NOT
    // actively scrolling. A widget that re-measures mid-fling must not yank
    // the user to the bottom (which would also unmount the rows they were
    // scrolling through). pinAuto itself is still race-proof for the
    // stationary case.
    //
    // A first-mount normally must NOT pin (it fires during scroll-up window
    // expansion and would yank the user). But while we're actively following
    // (stick armed), a freshly mounted tall row at the bottom is genuinely
    // new content to follow — e.g. a widget rendering inside the streaming
    // message right as the turn re-keys (single → grouped turn) and remounts
    // the row, which otherwise looks like a first-mount and skips the pin.
    // pinAuto still releases if the live geometry shows a real scroll-up.
    // Only TAIL growth is followed. `genuineResize` alone would follow a
    // disclosure the user opened 50 messages up (see tailRowResized). A
    // viewport resize is followed only while FOLLOWING, and only for a shrink
    // (see the viewport branch of measureResizeEntries for the direction argument).
    // Trailing chrome (the host's `belowRows`: the working footer that mounts
    // under a reply gone quiet) is content BELOW the tail, so its growth is
    // followed on the same terms as a tail row's -- while following. A reader
    // who scrolled up keeps their place; the footer is theirs to scroll to.
    const shouldFollow =
      (genuineResize && tailRowResized)
      || ((firstMount || viewportResized || trailingChromeResized) && stickRef.current)
    // The settle gate applies even while following: with `stick` armed the
    // old bypass meant every RO tick pinned instantly DURING an active
    // gesture — the pin write and the user's input fought over scrollTop
    // frame by frame (visible as jitter) until the scroll event finally
    // released `stick`. Intent listeners bump the timestamp at input time,
    // so the gate holds pins off from the first wheel/touch/key/scrollbar
    // event; a stationary reader at the bottom is untouched (no input →
    // timestamp stays old → pins flow).
    if (shouldFollow) {
      const now = performance.now()
      if (pinSuppressedNow(now, lastUserScrollAtRef.current, pinCascadeUntilRef.current, SCROLL_SETTLE_MS)) {
        // This resize belongs to a cascade the user's own input started —
        // hold the gate open past it rather than pinning into its tail.
        pinCascadeUntilRef.current = now + SCROLL_SETTLE_MS
      } else {
        pinAuto()
      }
    }
  }, [pinAuto, stickRef, lastUserScrollAtRef, pinCascadeUntilRef])

  // ---- scrollToIndex / scrollToBottom imperative APIs ----

  const scrollToIndex = useCallback(
    (index: number, options?: ScrollToIndexOptions) => {
      const el = scrollerRef.current
      if (!el) return
      const count = itemsRef.current.length
      if (count === 0) return
      const t = Math.max(0, Math.min(count - 1, Math.floor(index)))
      setWindowRange(computeJumpWindow(t, count, overscan))
      requestAnimationFrame(() => {
        const off = getOffsetFn(t, count, getH)
        const align = options?.align ?? 'start'
        const behavior = options?.behavior ?? 'auto'
        const itemH = getH(t)
        let scrollTop = off
        if (align === 'center') scrollTop = off - el.clientHeight / 2 + itemH / 2
        else if (align === 'end') scrollTop = off - el.clientHeight + itemH
        scrollTop = Math.max(0, Math.min(el.scrollHeight - el.clientHeight, scrollTop))
        // Jumping to a specific index is an explicit "stop following" intent.
        stickRef.current = false
        writeScrollTop(el, scrollTop, behavior, 'release', 'toIndex')
      })
    },
    [overscan, getH, scrollerRef, writeScrollTop, itemsRef, setWindowRange, stickRef],
  )

  const scrollToBottom = useCallback(
    (behavior: ScrollBehavior = 'auto') => {
      const el = scrollerRef.current
      if (!el) return
      const count = itemsRef.current.length
      if (count === 0) return
      // A restore owns the position: refuse, and do NOT arm follow on the way
      // out. The hazard is not that this pin is wrong when it is decided -- it is
      // that it is decided and APPLIED in different states. The caller's gate
      // (`autoFollowAllowed`) asks "is the reader within a viewport of the
      // bottom", which is true while the previous session's scrollTop is still in
      // place, and the actual write is deferred to the rAF below -- by which time
      // the restore has placed the reader mid-history. Captured on a phone as
      // `RESTORE.OK idx=5 n=40` followed by a burst of `WRITE bottom`, landing a
      // reader who had left 24,600px from the end at `to-end 0px`.
      if (restoreOwnsPosition()) return
      // Mount the tail so the bottom items have real heights, then force-pin.
      setWindowRange(tailWindow(count, overscan))
      // Arm follow immediately so a streaming chunk that lands between now and
      // the rAF is also followed.
      stickRef.current = followOutput
      const pinToBottom = (b: ScrollBehavior) => {
        // Re-checked at APPLY time, not only at decision time: the gate can go up
        // in the frame between the two, which is exactly the race above.
        if (restoreOwnsPosition()) return
        const target = bottomTarget({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight })
        stickRef.current = followOutput
        writeScrollTop(el, target, b, 'pin', 'bottom')
      }
      requestAnimationFrame(() => {
        pinToBottom(behavior)
        // Settle: the tail window only just committed and its rows (widgets,
        // markdown) may finish measuring over the next few frames, moving the
        // true bottom down — otherwise an instant jump lands on a stale,
        // slightly-short target ("doesn't reach the end"). Re-pin over a few
        // frames so it lands exactly at the bottom. Skipped for smooth scrolls
        // (an instant re-pin mid-glide would cut the animation short); ongoing
        // streaming growth is handled by the ResizeObserver follow instead.
        if (behavior !== 'auto') return
        let n = 0
        const settle = () => {
          if (!el.isConnected || !stickRef.current) return
          pinToBottom('auto')
          if (++n < 3) requestAnimationFrame(settle)
        }
        requestAnimationFrame(settle)
      })
    },
    [overscan, followOutput, scrollerRef, writeScrollTop, restoreOwnsPosition, itemsRef, setWindowRange, stickRef],
  )

  return { pinAuto, forcePin, scrollToBottom, scrollToIndex, onFollowScroll, cancelHeldPinRetry, prePaintRepin, followResizeBatch }
}

/** The follow owner's layout effects, which must run after the shift
 *  compensation's (the facade calls this right after useShiftCompensation). */
export function useFollowPlacementPins<T>(ctx: {
  itemCount: number
  overscan: number
  sessionId: string
  scrollerRef: RefObject<HTMLDivElement | null>
  leadingOffset: (el: HTMLElement) => number
  itemsRef: Ref<T[]>
  getKeyRef: Ref<(item: T, index: number) => string>
  setWindowRange: SetWindowRange
  follow: Pick<FollowState, 'stickRef' | 'lastHardInputAtRef' | 'pinCascadeUntilRef' | 'writeScrollTop'>
  pinning: Pick<Pinning, 'pinAuto' | 'forcePin'>
}): void {
  const { itemCount, overscan, sessionId, scrollerRef, leadingOffset, itemsRef, getKeyRef, setWindowRange } = ctx
  const { stickRef, lastHardInputAtRef, pinCascadeUntilRef, writeScrollTop } = ctx.follow
  const { pinAuto, forcePin } = ctx.pinning

  // ---- Leading chrome: content ABOVE the list inside the scroller ----
  // The rows and the scroller's own box are observed; what sits between the
  // scroll origin and the first row is not -- a paging bar that mounts once the
  // server reports unloaded history, a header band the host renders above the
  // rows. That chrome mounting or resizing moves every row by its height with
  // no ResizeObserver seeing it and no scroll event: on Chromium native scroll
  // anchoring silently carries a bottom-pinned reader through it, on WebKit
  // (no anchoring) the reader is left the chrome's height short of the end.
  // Measured on the phone rig: the earlier-messages bar mounting 46px after
  // the entry pin, and nothing to bring the reader back.
  //
  // Re-evaluate the bottom pin whenever the leading offset changes between
  // commits, while following. pinAuto's predicate decides: a reader resting on
  // our last write with no input is carried to the live bottom (idempotent on
  // Chromium, where anchoring already moved them there); anyone else is left
  // alone. Measured only while following -- released readers own their position
  // -- and reset on release so a stale value cannot fire on re-engagement.
  const leadingChromeRef = useRef(-1)
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el || !stickRef.current) { leadingChromeRef.current = -1; return }
    const lead = leadingOffset(el)
    const prev = leadingChromeRef.current
    leadingChromeRef.current = lead
    if (prev < 0 || Math.abs(lead - prev) <= 0.5) return
    if (inspectorOn()) devLog('LEAD', `${Math.round(prev)}->${Math.round(lead)} y=${Math.round(el.scrollTop)}`)
    // Same settle gate as the pre-paint pin: a gesture in flight outranks this
    // correction, and the post-paint RO pin still covers a parked reader.
    if (pinSuppressedNow(performance.now(), lastHardInputAtRef.current, pinCascadeUntilRef.current, SCROLL_SETTLE_MS)) return
    pinAuto()
  })
  const prevItemCountRef = useRef(itemCount)
  // Tail identity of the previous commit, session-scoped. What separates a
  // bulk PREPEND (idle history prefetch landing hundreds of rows ABOVE a
  // reader legitimately followed at the bottom — reading or typing) from a
  // bulk hydration REPLACE (thin optimistic list swapped for the full
  // conversation): a prepend keeps the tail item, a replace does not.
  const prevTailKeyRef = useRef<{ session: string; key: string } | null>(null)
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const growth = itemCount - prevItemCountRef.current
    prevItemCountRef.current = itemCount
    const tail = itemCount > 0 ? itemsRef.current[itemCount - 1] : undefined
    const tailKey = tail !== undefined ? getKeyRef.current(tail, itemCount - 1) : null
    const prevTail = prevTailKeyRef.current
    prevTailKeyRef.current = tailKey !== null ? { session: sessionId, key: tailKey } : null
    if (growth <= 0) return
    // BULK growth while followed is history hydration, not streaming: the
    // slot-detail fetch resolving and REPLACING a thin optimistic list (e.g.
    // a lone WS streaming bubble that landed before the fetch — it consumed
    // the slot-entry one-shot pin) with the full conversation. Routing that
    // through pinAuto smooth-glides from the top across hundreds of
    // virtualized rows, visibly "paging" through the conversation and often
    // landing short while heights are still estimates. Treat it like slot
    // entry instead: remount the tail window and force-pin instantly.
    // Gated on stick so a "load older" prepend while the user reads history
    // is never yanked to the bottom.
    // A bulk PREPEND with an unchanged tail must NOT take the force-pin path:
    // the reader's bottom content is untouched (the prepend compensation holds
    // the view), and force-pinning both remounts the tail window (a visible
    // flash under a reader typing at the bottom) and races the async scroll
    // event of a reader who JUST started scrolling up — yanking them back to
    // the bottom. Only a REPLACED tail is hydration.
    const bulkPrepend = prevTail !== null && prevTail.session === sessionId && prevTail.key === tailKey
    if (growth > overscan + 1 && stickRef.current && !bulkPrepend) {
      setWindowRange(tailWindow(itemCount, overscan))
      forcePin()
      const id = requestAnimationFrame(() => {
        // Recheck stick: the user can scroll up between the synchronous pin
        // and this frame — the scroll handler releases stick, and an
        // unconditional forcePin here would yank them back and re-arm follow.
        if (!el.isConnected || !stickRef.current) return
        forcePin()
      })
      return () => cancelAnimationFrame(id)
    }
    // A prepend with an unchanged tail never takes the pin/force-pin paths --
    // but a reader FOLLOWED AT THE BOTTOM still needs their view held: the
    // upward-shift compensations (triggers 2/3) are gated on !stick, so with
    // an early return alone nobody re-anchored the bottom and every idle
    // prefetch landing shoved the parked view by the prepended height
    // (momentum rig: 32 anchor jumps, worst ~3700px, all while parked).
    // Re-target the bottom SYNCHRONOUSLY in this same layout effect -- pre-
    // paint, so the parked reader never sees an intermediate frame.
    if (bulkPrepend && growth > 0) {
      // STICK ONLY: rebase the numeric window and re-target the bottom
      // pre-paint (a followed reader has no other guardian; the anchor
      // compensations are !stick by design and pinAuto is post-paint).
      // NOT-STICK is owned by the part-1/part-2 anchor machinery -- doing
      // it here too double-shifted the window (anti-loop test pins this).
      if (stickRef.current) {
        setWindowRange((r) => ({
          start: Math.min(itemCount, r.start + growth),
          end: Math.min(itemCount, r.end + growth),
        }))
        const target = bottomTarget({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight })
        writeScrollTop(el, target, 'auto', 'pin', 'jump')
      }
      return
    }
    // Pin synchronously (pre-paint) so a new message appears at the bottom
    // without a flicker, then once more next frame after its real height is
    // known. Both go through the race-proof pinAuto.
    pinAuto()
    const id = requestAnimationFrame(() => {
      if (!el.isConnected) return
      pinAuto()
    })
    return () => cancelAnimationFrame(id)
  // eslint-disable-next-line react-hooks/exhaustive-deps -- re-pin triggers are deliberate; the rest resolves via refs
  }, [itemCount, overscan, pinAuto, forcePin, scrollerRef])
}
