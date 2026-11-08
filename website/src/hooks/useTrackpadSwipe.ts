/**
 * A two-finger horizontal trackpad swipe on desktop, read as a DRAG.
 *
 * A trackpad swipe reaches the page as a run of `wheel` events carrying
 * `deltaX`; there are no touch events off a touch screen. This hook cuts that
 * stream into runs and reports each run that is the page's as a drag the fingers
 * drive: `onDragStart(dir)`, then `onDragMove(sumX, at)` per event with the
 * summed `deltaX`, then `onDragEnd(how, at)` once the fingers are off. The page
 * drives its panel's offset from that sum and decides the release itself.
 *
 * A run is wheel events with no gap longer than {@link SWIPE_GESTURE_IDLE_MS}.
 * Its owner is decided by its FIRST event and held for the whole run, as the
 * browser latches a scroll's axis:
 * - vertical-first, or a pinch (`ctrlKey`), is never ours;
 * - horizontal-first over something that can still scroll that way (a code
 *   block, a wide table, a sideways list) belongs to that scroller;
 * - horizontal-first in a direction `wants` declines is left to the browser
 *   (on macOS that is its back/forward swipe);
 * A new run right after a drag ended (even mid-settle) is a new drag: the
 * page continues it from wherever the panel is, as a phone's drag can grab a
 * settling panel. A momentum tail never starts one: it stays in its own run.
 * Anything else is a drag: every event of it is `preventDefault`ed and
 * `overscroll-behavior-x: contain` is set on the document root while it runs,
 * so the browser's history swipe does not start under it.
 *
 * A drag ends only on a real lift-off: no event for
 * {@link SWIPE_GESTURE_IDLE_MS} (`gap`), or the momentum tail's signature,
 * {@link MOMENTUM_DECAY_EVENTS} steps in a row each smaller than the last by a
 * near-constant ratio (`momentum`). A finger that slows down or pauses (60ms
 * and less) wobbles instead, so it keeps following 1:1. Steps that may be the
 * tail are held back, applied the moment the streak stops looking like one, and
 * dropped once it is recognised, so the panel stops where the fingers left it
 * and the rest of the run is held, never dragging again.
 *
 * In the desktop app the shell also reports the trackpad's real finger-down and
 * finger-up (lib/scrollTouch). Once a phase has been heard, the lift-off is
 * that `end` (`lift`), and a pause with the fingers still down no longer ends
 * the drag, up to {@link FINGER_HOLD_MAX_MS}. The gap and the momentum
 * signature stay armed, so a lost or late phase only costs precision, and a
 * page that never hears one (a plain browser) behaves exactly as above.
 *
 * Under `prefers-reduced-motion` there is no live drag: the run is read whole
 * and, if it was a clear horizontal swipe past {@link SWIPE_THRESHOLD_PX},
 * `onSwipe(dir)` fires once when it ends.
 *
 * Directions name the FINGERS: `left` is a swipe toward the left, which a
 * trackpad reports as a positive `deltaX` (the content moves right to left,
 * the way a drag on a phone reveals a panel anchored on the right).
 *
 * A frame inside the root swallows the wheel while the pointer is over it.
 * A frame built with `relayWheel` (lib/widgetSrcdoc) posts its unowned
 * horizontal wheels as `{type: 'mc-frame-wheel', dx, dy, mode}`; the hook feeds
 * those into the same reader, accepted only from a frame `acceptFrame` names,
 * and asks `wants(dir, true)` so the host can limit what a frame may start.
 */
import { useEffect, useRef, type RefObject } from 'react'
import { WHEEL_LINE_PX } from './useWheelToHorizontal'
import { hasScrollRoomX, SWIPE_GESTURE_IDLE_MS } from '../lib/trackpadGesture'
import { subscribeScrollTouch } from '../lib/scrollTouch'
import { isModalLayer } from './modalLayer'

export type TrackpadSwipeDir = 'left' | 'right'

/** Summed horizontal travel, in px, that commits a reduced-motion swipe. */
export const SWIPE_THRESHOLD_PX = 80
export { SWIPE_GESTURE_IDLE_MS }
/** The live gap the reader and its quiet timer use. A page test that waits on
 *  the screen between two halves of one swipe widens it, so a slow assertion is
 *  not read as the fingers lifting; nothing else writes it. */
export const swipeTiming = { idleMs: SWIPE_GESTURE_IDLE_MS }
/**
 * The momentum tail's signature: this many events in a row, each smaller than
 * the one before by a NEAR-CONSTANT ratio. A trackpad's inertia decays
 * geometrically; a finger that is just slowing down wobbles. Only the signature
 * is a lift-off, so a slow or slowing finger keeps following 1:1.
 */
export const MOMENTUM_DECAY_EVENTS = 3
/** A held run ends when this many steps in a row each grow by HELD_RISE_RATIO:
 *  momentum only decays, so that is new fingers. Two, so one spike does not. */
export const HELD_RISE_RATIO = 1.3
export const HELD_RISE_STEPS = 2
/** The creeping tail: after a step of at least SLOW_TAIL_MIN_PEAK_PX, steps no
 *  bigger than SLOW_TAIL_PX, MOMENTUM_DECAY_EVENTS in a row. The floor is
 *  absolute on purpose: a slow drag's own steps are a few px and uneven, and a
 *  rule against the run's peak read a slow drag as its own tail. */
export const SLOW_TAIL_MIN_PEAK_PX = 16
export const SLOW_TAIL_PX = 4
/** With the fingers known down, the longest pause, ms, that still keeps the
 *  drag. Past it the drag ends as a gap anyway, so a lost `end` cannot hold a
 *  panel half-open. */
export const FINGER_HOLD_MAX_MS = 1000
/** The band a momentum step's ratio lies in. */
const MOMENTUM_RATIO_MIN = 0.5
const MOMENTUM_RATIO_MAX = 0.98
/** How far apart the ratios of one tail may lie. */
const MOMENTUM_RATIO_SPREAD = 0.06
/** How much larger the summed |dx| must be than |dy| for a reduced-motion swipe. */
const SWIPE_DOMINANCE = 2
/** The message a relaying frame posts. */
export const FRAME_WHEEL_MESSAGE = 'mc-frame-wheel'
/** A relaying frame's hello, and the host's answer: relay only while armed. */
export const FRAME_WHEEL_HELLO = 'mc-frame-wheel-hello'
export const FRAME_WHEEL_ARMED = 'mc-frame-wheel-armed'
/** The oldest a relayed wheel's own time may be and still be used, ms. A
 *  stall that delays a burst is well under this; anything older, newer than
 *  now, or not a number is read at the time it arrives instead. */
export const FRAME_WHEEL_MAX_AGE_MS = 1000

/** When a relayed wheel happened, on this page's clock. `t` is the frame's
 *  `performance.timeOrigin + timeStamp` (epoch ms); `received` is the message's
 *  own time here, the fallback. */
export function relayedWheelTime(t: unknown, received: number, now = performance.now()): number {
  const fallback = received > 0 ? received : now
  if (typeof t !== 'number' || !Number.isFinite(t)) return fallback
  const at = t - performance.timeOrigin
  // Two documents' origins can disagree by a fraction of a ms; that is not
  // the future.
  if (at > now + 1 || now - at > FRAME_WHEEL_MAX_AGE_MS) return fallback
  return Math.min(at, now)
}

export interface WheelSample {
  dx: number
  dy: number
  /** `WheelEvent.deltaMode`. */
  mode: number
  ctrl: boolean
  /** Whether the content under the pointer can still scroll this way. */
  owned: boolean
  /** ms, on any monotonic clock. */
  at: number
  /** The sample came from a relaying frame. */
  fromFrame?: boolean
}

export interface WheelVerdict {
  /** The event belongs to a run the page owns: cancel the browser's default. */
  consume: boolean
  /** Set on the first event of a drag. */
  start?: TrackpadSwipeDir
  /** The drag's summed `deltaX` after this event, px. */
  move?: number
  /** The drag ended on this event: its momentum tail was recognised. */
  end?: boolean
  /** A drag from an earlier run was still open and this event's gap ended it. */
  endPrev?: boolean
  /** With `endPrev`: the earlier run's last event time, its sum after any
   *  held-back deltas were applied (set only when some were), and its summary. */
  prevAt?: number
  prevMove?: number
  prevSummary?: { dir: TrackpadSwipeDir; sumX: number; sumY: number }
}

const px = (d: number, mode: number) => (mode === 1 ? d * WHEEL_LINE_PX : mode === 2 ? d * 800 : d)

type RunOwner = 'drag' | 'held' | 'other'

/**
 * The run reader on its own, with no DOM and no timer: feed it every wheel
 * sample in order and act on each verdict, and call `finish(at)` when the run
 * has gone quiet. `wants` is asked once per run, at its first event.
 */
export function createTrackpadSwipeReader(
  wants: (dir: TrackpadSwipeDir, fromFrame: boolean) => boolean,
  /** True while the shell reports the fingers down. Unset: never known. */
  fingersDown: () => boolean = () => false,
) {
  let lastAt = -Infinity
  let owner: RunOwner | null = null
  /** Bumped when a run starts, whoever owns it. */
  let runNo = 0
  let sumX = 0
  let sumY = 0
  let dir: TrackpadSwipeDir = 'left'
  /** Step ratios of the shrinking streak that may be the tail, newest last. */
  let ratios: number[] = []
  let lastAbs = 0
  /** The last non-zero signed delta: a tail never changes direction. */
  let lastDx = 0
  /** The run's largest applied step, and how many tiny steps in a row since:
   *  the evidence of a creeping tail. */
  let peakAbs = 0
  let tinyRun = 0
  /** While held: the last signed delta, the last size, and how many steps in
   *  a row grew by `HELD_RISE_RATIO` -- the evidence a held run is over. */
  let heldLastDx = 0
  let heldPrevAbs = 0
  let heldRises = 0
  /** Hold the rest of the run, remembering where its deltas stood. */
  const hold = () => {
    owner = 'held'
    heldLastDx = lastDx
    heldPrevAbs = lastAbs
    heldRises = 0
  }
  /** Shrinking deltas held back while they may be the tail: applied as soon as
   *  the streak stops looking like one, dropped once it is recognised. */
  let pendX = 0
  let pendY = 0
  /** The drag's sum after `finish` applied held-back deltas, until taken. */
  let flushed: number | undefined
  /** End the run. Returns whether it was a drag still in progress. Deltas held
   *  back as a possible tail that was never recognised were the fingers: they
   *  are applied here (the momentum branch clears them before it finishes). */
  const finish = (_at?: number): boolean => {
    const wasDrag = owner === 'drag'
    if (wasDrag) {
      if (pendX !== 0 || pendY !== 0) {
        sumX += pendX
        sumY += pendY
        pendX = 0
        pendY = 0
        flushed = sumX
      }
    }
    owner = null
    return wasDrag
  }
  const takeFlush = () => { const f = flushed; flushed = undefined; return f }
  return {
    feed(s: WheelSample): WheelVerdict {
      const out: WheelVerdict = { consume: false }
      const gap = s.at - lastAt
      // Fingers known down: a pause is not a lift-off, up to the hold cap.
      const holding = owner === 'drag' && gap <= FINGER_HOLD_MAX_MS && fingersDown()
      if (gap > swipeTiming.idleMs && owner !== null && !holding) {
        // The quiet timer should have ended it already; end it here regardless,
        // so a new run is always decided afresh.
        if (finish(lastAt)) {
          out.endPrev = true
          out.prevAt = lastAt
          out.prevMove = takeFlush()
          out.prevSummary = { dir, sumX, sumY }
        }
      }
      lastAt = s.at
      const dx = px(s.dx, s.mode)
      const dy = px(s.dy, s.mode)
      // A held run (a recognised tail, a lift, a drag the page committed) is
      // over as soon as its events stop looking like its momentum, even with no
      // gap: macOS sends momentum for up to a second, and a new swipe inside it
      // was swallowed until it died out. Momentum never turns back and only
      // decays, so a turn back, or two steps in a row each growing by
      // HELD_RISE_RATIO, are new fingers: this event decides a new run.
      if (owner === 'held' && dx !== 0 && !s.ctrl) {
        const a = Math.abs(dx)
        if (heldLastDx !== 0 && (dx > 0) !== (heldLastDx > 0)) owner = null
        else {
          heldRises = heldPrevAbs > 0 && a >= HELD_RISE_RATIO * heldPrevAbs ? heldRises + 1 : 0
          heldPrevAbs = a
          heldLastDx = dx
          if (heldRises >= HELD_RISE_STEPS) owner = null
        }
      }
      if (owner === null) {
        if (s.ctrl) { owner = 'other'; runNo += 1; return out }
        if (dx === 0 && dy === 0) return out
        runNo += 1
        dir = dx > 0 ? 'left' : 'right'
        if (Math.abs(dx) <= Math.abs(dy) || s.owned || !wants(dir, !!s.fromFrame)) owner = 'other'
        else owner = 'drag'
        sumX = 0
        sumY = 0
        ratios = []
        lastAbs = 0
        lastDx = 0
        peakAbs = 0
        tinyRun = 0
        pendX = 0
        pendY = 0
        if (owner === 'drag') out.start = dir
      }
      if (owner === 'other') return out
      out.consume = true
      // A pinch landing mid-drag moves nothing; the browser keeps its zoom.
      if (owner === 'held' || s.ctrl) {
        if (s.ctrl) out.consume = false
        return out
      }
      const a = Math.abs(dx)
      // A turn back is the fingers, however it shrinks: it scores no ratio, so
      // the streak resets and what was held back is applied before it.
      const flipped = dx !== 0 && lastDx !== 0 && (dx > 0) !== (lastDx > 0)
      // The shell reports the fingers down (the desktop app's finger phases):
      // what arrives is the fingers, whatever its shape, so no tail is
      // inferred from it. The real lift-off ends the drag, and a lost `end`
      // still does at the hold cap (FINGER_HOLD_MAX_MS of quiet, above).
      if (fingersDown()) {
        if (a > peakAbs) peakAbs = a
        tinyRun = 0
        ratios = []
        lastAbs = a
        if (dx !== 0) lastDx = dx
        sumX += pendX + dx
        sumY += pendY + dy
        pendX = 0
        pendY = 0
        out.move = sumX
        return out
      }
      // The other shape of a tail: after a run that moved for real, a string of
      // tiny steps. macOS momentum after a light lift does not decay smoothly
      // enough for the ratio test below (it creeps 2px, 2px, 1px, 0, 1px...),
      // and a browser followed it 1:1 for a third of a second before the gap
      // ended it: the panel visibly slowed to a crawl, then jumped. Three tiny
      // steps in a row are held back like a tail; a bigger step applies them.
      // Only once the run had moved fast (a step of SLOW_TAIL_MIN_PEAK_PX), so a
      // drag that was slow all along follows every pixel, however uneven.
      const tiny = dx !== 0 && !flipped && peakAbs >= SLOW_TAIL_MIN_PEAK_PX
        && a <= SLOW_TAIL_PX
      if (tiny) {
        tinyRun += 1
        pendX += dx
        pendY += dy
        lastAbs = a
        lastDx = dx
        if (tinyRun >= MOMENTUM_DECAY_EVENTS) {
          // Fingers off: decide here; what was held back is the tail.
          pendX = 0
          pendY = 0
          finish(s.at)
          hold()
          out.end = true
        }
        return out
      }
      // A zero step says nothing about the fingers: inside a streak it is held
      // with it, neither applying the streak nor breaking it.
      if (dx === 0 && tinyRun > 0) {
        pendY += dy
        return out
      }
      tinyRun = 0
      const r = lastAbs > 0 && !flipped ? a / lastAbs : 0
      lastAbs = a
      if (dx !== 0) lastDx = dx
      const inBand = r >= MOMENTUM_RATIO_MIN && r <= MOMENTUM_RATIO_MAX
      if (a > 0 && inBand) {
        const next = [...ratios, r].slice(-MOMENTUM_DECAY_EVENTS)
        if (Math.max(...next) - Math.min(...next) <= MOMENTUM_RATIO_SPREAD) {
          // Still shaped like a tail: hold this delta back.
          ratios = next
          pendX += dx
          pendY += dy
          if (ratios.length >= MOMENTUM_DECAY_EVENTS) {
            // Fingers off: decide here, then hold the rest of the run. What was
            // held back is the tail, so it is dropped, not applied.
            pendX = 0
            pendY = 0
            finish(s.at)
            hold()
            out.end = true
          }
          return out
        }
        // A wobble, not a tail: this step may start one, so keep its ratio.
        ratios = [r]
      } else {
        ratios = []
      }
      // The fingers: apply what was held back, then this delta.
      if (a > peakAbs) peakAbs = a
      sumX += pendX + dx
      sumY += pendY + dy
      pendX = 0
      pendY = 0
      out.move = sumX
      return out
    },
    finish,
    /** The fingers left (the shell's `end`). Ends a drag in progress the way a
     *  recognised tail does, but keeps what was held back (it was the fingers),
     *  then holds the rest of the run. Returns whether a drag was in progress. */
    lift(): boolean {
      if (owner !== 'drag') return false
      finish()
      hold()
      return true
    },
    /** The page committed the drag mid-run (rule C): the rest of this run is
     *  held, as a recognised tail is, and ends the same ways. */
    commit(): void {
      if (owner !== 'drag') return
      pendX = 0
      pendY = 0
      hold()
    },
    /** The fingers landed again (the shell's `begin`): a held tail is over, so
     *  the next event decides a new run. Returns whether one was held. */
    dropHeld(): boolean {
      if (owner !== 'held') return false
      owner = null
      return true
    },
    /** The drag's run so far, for a reduced-motion read of the whole swipe. */
    summary: () => ({ dir, sumX, sumY }),
    /** The drag's new sum when the last `finish` applied held-back deltas. */
    takeFlush,
    /** When the last event arrived. */
    lastEventAt: () => lastAt,
    /** Which run the last event belonged to: changes when a new one starts,
     *  including one the page leaves to the browser. */
    runId: () => runNo,
  }
}

/** Whether a whole run reads as a clear swipe: past the threshold, horizontal. */
export function isClearSwipe(dir: TrackpadSwipeDir, sumX: number, sumY: number): boolean {
  const signed = dir === 'left' ? sumX : -sumX
  return signed >= SWIPE_THRESHOLD_PX && Math.abs(sumX) >= SWIPE_DOMINANCE * Math.abs(sumY)
}

/** Whether an element on the way from the event's target up to (not past)
 *  `root` can still scroll horizontally in the direction `dx` moves it. Takes
 *  the event's composed path, which crosses shadow roots, or a bare target to
 *  walk up from. */
export function canScrollX(from: EventTarget | null | readonly EventTarget[], dx: number, root: Element | null): boolean {
  const chain: Element[] = []
  if (Array.isArray(from)) {
    for (const n of from as readonly EventTarget[]) if (n instanceof Element) chain.push(n)
  } else {
    for (let n = from instanceof Element ? from : null; n; n = n.parentElement) chain.push(n)
  }
  for (const n of chain) {
    const max = n.scrollWidth - n.clientWidth
    if (max > 1) {
      const style = getComputedStyle(n)
      const o = style.overflowX
      if ((o === 'auto' || o === 'scroll') && hasScrollRoomX(n.scrollLeft, max, style.direction === 'rtl', dx)) return true
    }
    if (n === root) break
  }
  return false
}

/** Whether `el` is on screen within `container`: no element on the way up to
 *  it is hidden (`display: none`, `hidden`, or `aria-hidden="true"`, the way a
 *  side panel keeps its other tabs' bodies mounted). For a frame's relayed
 *  wheels: only the frame the person is looking at may drive the panel. */
export function shownWithin(el: Element, container: Element): boolean {
  if (!container.contains(el)) return false
  for (let n: Element | null = el; n && n !== container; n = n.parentElement) {
    if ((n as HTMLElement).hidden || n.getAttribute('aria-hidden') === 'true') return false
    if ((n as HTMLElement).style?.display === 'none') return false
  }
  return true
}

/** Whether an element on the way from the event's target up to (not past)
 *  `root` is a modal layer: `role="dialog"` / `"alertdialog"`, `aria-modal`, or
 *  an open `<dialog>` (`isModalLayer`, the test the phone drawer walks up
 *  with too), so the next dialog needs no attribute of its own. */
export function inModalLayer(from: EventTarget | null | readonly EventTarget[], root: Element | null): boolean {
  const chain: Element[] = []
  if (Array.isArray(from)) {
    for (const n of from as readonly EventTarget[]) if (n instanceof Element) chain.push(n)
  } else {
    for (let n = from instanceof Element ? from : null; n; n = n.parentElement) chain.push(n)
  }
  for (const n of chain) {
    if (n === root) break
    if (isModalLayer(n)) return true
  }
  return false
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

export interface TrackpadSwipeOptions {
  enabled: boolean
  /** Whether a run this way may start now. `fromFrame` is true for a run a
   *  relaying frame reported. A declined run is left entirely to the browser. */
  wants: (dir: TrackpadSwipeDir, fromFrame: boolean) => boolean
  /** A drag began. */
  onDragStart: (dir: TrackpadSwipeDir) => void
  /** The drag's summed `deltaX`, px (positive is the fingers moving left), at
   *  `at` on the event clock. Only the fingers' own deltas arrive here: a
   *  momentum tail never does. Return `true` when the page committed on this
   *  move: the rest of the run is then held (no further moves, no end). */
  onDragMove: (sumX: number, at: number) => void | boolean
  /** The fingers are off. `momentum`: the tail was recognised, so they left
   *  with the speed of the last moves. `gap`: no event for the idle window, so
   *  they had stopped before they left. `lift`: the desktop shell reported the
   *  real lift-off and `at` is when, so the speed over the moves just before
   *  `at` is the fingers' own. For `gap` and `momentum`, `at` is when it was
   *  decided. */
  onDragEnd: (how: 'momentum' | 'gap' | 'lift', at: number) => void
  /** Reduced motion: one clear swipe, reported once at the end of its run. */
  onSwipe: (dir: TrackpadSwipeDir) => void
  /** Whether a frame's relayed wheel may join a run. Unset: none may. */
  acceptFrame?: (frame: HTMLIFrameElement) => boolean
}

export function useTrackpadSwipe(rootRef: RefObject<HTMLElement | null>, opts: TrackpadSwipeOptions) {
  const optsRef = useRef(opts)
  optsRef.current = opts
  const { enabled } = opts
  useEffect(() => {
    const root = rootRef.current
    if (!enabled || !root) return
    /** The shell's last finger phase: null until one is heard (a browser never). */
    let fingers: boolean | null = null
    const reader = createTrackpadSwipeReader(
      (d, fromFrame) => optsRef.current.wants(d, fromFrame),
      () => fingers === true,
    )
    const docEl = document.documentElement
    let held = false
    let prior = ''
    let quiet: number | undefined
    /** The live run's mode: a live drag, a reduced-motion read, or none. */
    let live: 'drag' | 'reduced' | null = null
    const hold = () => {
      if (held) return
      held = true
      prior = docEl.style.overscrollBehaviorX
      docEl.style.overscrollBehaviorX = 'contain'
    }
    const letGo = () => {
      if (!held) return
      held = false
      docEl.style.overscrollBehaviorX = prior
    }
    /** The fingers are off: settle a live drag, or read a reduced-motion run. */
    const endLive = (how: 'momentum' | 'gap' | 'lift', at: number, run = reader.summary()) => {
      const was = live
      live = null
      if (was === 'drag') optsRef.current.onDragEnd(how, at)
      if (was === 'reduced') {
        const { dir, sumX, sumY } = run
        if (isClearSwipe(dir, sumX, sumY)) optsRef.current.onSwipe(dir)
      }
    }
    /** Bumped by every event of an owned run. */
    let seq = 0
    let quietFrame: number | undefined
    const onQuiet = () => {
      quiet = undefined
      // Wait one frame first: after a slow frame the timer can fire ahead of
      // wheel events that were queued behind it, and input is delivered before
      // the next frame's callbacks. Ends only if nothing arrived meanwhile.
      const at = seq
      const settle = () => {
        quietFrame = undefined
        if (seq !== at) return
        const now = performance.now()
        // The fingers are still down: a pause, not a lift-off. Look again when
        // the hold cap runs out, unless an event or the `end` comes first.
        const paused = now - reader.lastEventAt()
        if (fingers === true && live === 'drag' && paused < FINGER_HOLD_MAX_MS) {
          quiet = window.setTimeout(onQuiet, Math.max(1, FINGER_HOLD_MAX_MS - paused))
          return
        }
        reader.finish(now)
        const moved = reader.takeFlush()
        if (moved !== undefined && live === 'drag') optsRef.current.onDragMove(moved, reader.lastEventAt())
        endLive('gap', now)
        letGo()
      }
      if (typeof requestAnimationFrame === 'function') quietFrame = requestAnimationFrame(settle)
      else settle()
    }
    const run = (s: WheelSample): boolean => {
      // The run this event may end: read before the feed, which starts the next
      // run and resets the summary a reduced-motion swipe is judged on.
      const prev = reader.summary()
      const runBefore = reader.runId()
      const v = reader.feed(s)
      if (reader.runId() !== runBefore) {
        // A new run: the old one is over (the feed ended it), so its quiet
        // check must not end this one. A run the page leaves to the browser
        // would otherwise be reset by it, and a later sideways drift would
        // start a drag in the middle of a scroll. The old run's hold goes too;
        // a run the page takes holds again below.
        window.clearTimeout(quiet)
        quiet = undefined
        if (quietFrame !== undefined) cancelAnimationFrame(quietFrame)
        quietFrame = undefined
        if (!v.consume) letGo()
      }
      if (v.endPrev && live) {
        if (v.prevMove !== undefined && live === 'drag') optsRef.current.onDragMove(v.prevMove, v.prevAt ?? s.at)
        endLive('gap', s.at, v.prevSummary ?? prev)
      }
      if (v.start) {
        live = prefersReducedMotion() ? 'reduced' : 'drag'
        if (live === 'drag') optsRef.current.onDragStart(v.start)
      }
      if (v.move !== undefined && live === 'drag' && optsRef.current.onDragMove(v.move, s.at) === true) {
        // The page committed on this move: the rest of the run is held, and a
        // turn back or a new burst starts a new run at once.
        reader.commit()
        live = null
      }
      if (v.end && live) endLive('momentum', s.at)
      if (v.consume) {
        seq += 1
        hold()
        window.clearTimeout(quiet)
        quiet = window.setTimeout(onQuiet, swipeTiming.idleMs)
      }
      return v.consume
    }
    const onWheel = (e: WheelEvent) => {
      if (e.defaultPrevented) return
      // The composed path, so a scroller inside a shadow root (a code block
      // rendered by a web component) is seen, not just its host.
      const path = typeof e.composedPath === 'function' ? e.composedPath() : []
      // A run that starts inside a modal layer is the layer's: the page behind
      // it must not move (a swipe over the What's New dialog opened the panel).
      const owned = inModalLayer(path.length ? path : e.target, root)
        || (e.deltaX !== 0 && canScrollX(path.length ? path : e.target, e.deltaX, root))
      // The event's own time, not the time it is handled: wheels queued behind a
      // slow frame arrive in a burst, and handling time would read that frame
      // as a gap in the fingers and end the drag under them.
      const when = e.timeStamp > 0 ? e.timeStamp : performance.now()
      const sample = { dx: e.deltaX, dy: e.deltaY, mode: e.deltaMode, ctrl: e.ctrlKey, owned, at: when }
      if (run(sample)) e.preventDefault()
    }
    /** Tell a relaying frame whether this host is listening. A frame that is
     *  not armed leaves every wheel to the browser, so a run nobody would act on
     *  is never cancelled. Frames inside the root only. */
    const arm = (frame: HTMLIFrameElement, on: boolean) => {
      try {
        // '*' because the frame is a sandboxed srcdoc document we built, with an
        // opaque origin no target string can name; the payload is one boolean.
        // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
        frame.contentWindow?.postMessage({ type: FRAME_WHEEL_ARMED, on }, '*')
      } catch { /* gone */ }
    }
    /** Only frames whose document carries the relay (`data-wheel-relay`, set
     *  where the srcdoc is built with `relayWheel`): any other iframe under the
     *  root is never told anything. */
    const frames = () => Array.from(root.querySelectorAll<HTMLIFrameElement>('iframe[data-wheel-relay]'))
    if (optsRef.current.acceptFrame) for (const f of frames()) arm(f, true)
    const onMessage = (event: MessageEvent) => {
      const d = event.data as { type?: unknown; dx?: unknown; dy?: unknown; mode?: unknown; t?: unknown } | null
      if (!d || (d.type !== FRAME_WHEEL_MESSAGE && d.type !== FRAME_WHEEL_HELLO)) return
      // A message with no source window names no frame, and must never match a
      // frame whose own window is not there yet.
      if (!event.source) return
      if (d.type === FRAME_WHEEL_HELLO) {
        // A frame that loaded while this host listens: arm it.
        const hello = frames().find((f) => f.contentWindow === event.source)
        if (hello && optsRef.current.acceptFrame) arm(hello, true)
        return
      }
      const { acceptFrame } = optsRef.current
      if (!acceptFrame || typeof d.dx !== 'number' || typeof d.dy !== 'number') return
      if (!Number.isFinite(d.dx) || !Number.isFinite(d.dy)) return
      const frame = Array.from(root.querySelectorAll('iframe')).find((f) => f.contentWindow === event.source)
      if (!frame || !acceptFrame(frame)) return
      const mode = d.mode === 1 || d.mode === 2 ? d.mode : 0
      // The frame posts only wheels its own content does not own. The wheel's
      // own time, not the message's: it decides run gaps and the release speed.
      run({ dx: d.dx, dy: d.dy, mode, ctrl: false, owned: false, at: relayedWheelTime(d.t, event.timeStamp), fromFrame: true })
    }
    let liftFrame: number | undefined
    let pendingLift: (() => void) | undefined
    const stopTouch = subscribeScrollTouch((phase, at) => {
      if (phase === 'begin') {
        // A lift still waiting for its frame belongs to the gesture before.
        if (pendingLift) {
          if (liftFrame !== undefined) cancelAnimationFrame(liftFrame)
          pendingLift()
        }
        fingers = true
        if (reader.dropHeld()) {
          window.clearTimeout(quiet)
          quiet = undefined
          letGo()
        }
        return
      }
      fingers = false
      if (!live) return
      // Wait one frame: wheels queued ahead of this message are the fingers'
      // last moves, and input is delivered before the next frame's callbacks.
      const lift = () => {
        liftFrame = undefined
        pendingLift = undefined
        if (!live || !reader.lift()) return
        const moved = reader.takeFlush()
        if (moved !== undefined && live === 'drag') optsRef.current.onDragMove(moved, reader.lastEventAt())
        endLive('lift', at)
      }
      pendingLift = lift
      if (typeof requestAnimationFrame === 'function') liftFrame = requestAnimationFrame(lift)
      else lift()
    })
    root.addEventListener('wheel', onWheel, { passive: false })
    window.addEventListener('message', onMessage)
    return () => {
      stopTouch?.()
      if (liftFrame !== undefined) cancelAnimationFrame(liftFrame)
      root.removeEventListener('wheel', onWheel)
      window.removeEventListener('message', onMessage)
      window.clearTimeout(quiet)
      if (quietFrame !== undefined) cancelAnimationFrame(quietFrame)
      // No longer listening: frames stop relaying and cancelling.
      for (const f of frames()) arm(f, false)
      // An unbind mid-drag still settles it, so the panel is never stranded.
      endLive('gap', performance.now())
      letGo()
    }
  }, [enabled, rootRef])
}
