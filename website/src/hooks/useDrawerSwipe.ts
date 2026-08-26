import { useEffect, useRef, useState } from 'react'
import { animate, type MotionValue } from 'framer-motion'

/**
 * Finger-tracking open/close gesture for the mobile sessions drawer.
 *
 * Supersedes `useSwipeEdge`, which was a threshold DETECTOR: it read the
 * displacement once on `touchend` and fired a plain callback, so the panel
 * snapped open after the fact with nothing on screen following the finger, and
 * a drag begun and then reconsidered still committed. This hook drives the
 * panel's offset continuously instead, which is what makes a half-drag
 * readable and a drag-back cancellable.
 *
 * ONE binding covers both directions. The accept rule depends on whether the
 * panel is currently open, and `open` is deliberately read from a ref rather
 * than a dependency: the opening drag flips it mid-gesture (see
 * `onGestureOpen`), and a dep would tear the listeners down under the finger —
 * leaving the release to land on nothing while the panel stayed half-open.
 */

/** Travel, in px, before the gesture's axis is decided. */
const AXIS_LOCK = 10
/**
 * Inner edge of the band an OPENING drag may start in.
 *
 * Not 0. The platform's own back-swipe lives in the first ~20-30px and is not
 * cancellable from script — `preventDefault` does not reach it — so a band that
 * starts at the bezel loses a share of its gestures to the browser no matter
 * what this code does. Starting inboard of it means the two gestures mostly do
 * not contend for the same touch.
 */
const EDGE_START = 24
/**
 * Outer edge of that band.
 *
 * A band, not a third of the screen: the predecessor used 35% of the viewport
 * (137px on a 390px phone), so any rightward drag begun in the left third of a
 * message opened the drawer.
 */
const EDGE_END = 120
/** Release past this share of the travel commits to the new state. */
const COMMIT_DISTANCE = 0.5
/** Release faster than this (px/ms) commits regardless of how far it got. */
const COMMIT_VELOCITY = 0.4
/** Critically damped: a panel that fills the screen must not overshoot its
 *  own edge, so this is bounce-free rather than a springy tween. */
const DRAWER_SETTLE = { type: 'spring' as const, bounce: 0, duration: 0.32 }
/** Reduced motion still needs the panel to ARRIVE — dragging is direct
 *  manipulation, and dropping the settle to 0 would teleport the panel out from
 *  under the finger. A short linear tween carries it there without the spring. */
const DRAWER_SETTLE_REDUCED = { duration: 0.12, ease: 'linear' as const }

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** Animate a drawer offset to its resting place with the shared settle curve.
 *  Exported so the tap/backdrop/programmatic paths land identically to a
 *  released gesture — two curves for one panel is how the two paths visibly
 *  drift apart. */
export function animateDrawer(x: MotionValue<number>, to: number, onDone?: () => void) {
  const curve = prefersReducedMotion() ? DRAWER_SETTLE_REDUCED : DRAWER_SETTLE
  const controls = animate(x, to, { ...curve, onComplete: onDone })
  return () => controls.stop()
}

/**
 * Nearest ancestor of `from`, up to and including `root`, that scrolls
 * horizontally. Returns null when the touch did not start inside one.
 */
function findHorizontalScroller(from: EventTarget | null, root: HTMLElement): HTMLElement | null {
  let node: Element | null = from instanceof Element ? from : null
  while (node) {
    if (node instanceof HTMLElement && node.scrollWidth - node.clientWidth > 1) {
      const overflowX = getComputedStyle(node).overflowX
      if (overflowX === 'auto' || overflowX === 'scroll') return node
    }
    if (node === root) break
    node = node.parentElement
  }
  return null
}

interface DrawerSwipeOptions {
  /** Bind the gesture at all. Mobile-only — a pointer device has the toggle. */
  enabled: boolean
  /** Whether the panel is currently open. Read live, never a dependency. */
  open: boolean
  /** Panel offset, in px: `-travel` fully closed, `0` fully open. */
  x: MotionValue<number>
  /** Put the panel in the DOM at its closed offset so the drag can reveal it. */
  onGestureOpen: () => void
  /** Released. `open` is the state the panel settled into, reported only once
   *  the settle animation has finished so an unmount cannot cut it short. */
  onSettle: (open: boolean) => void
}

/** @returns whether a drag currently owns the panel (suppress transitions). */
export function useDrawerSwipe(
  ref: React.RefObject<HTMLElement | null>,
  { enabled, open, x, onGestureOpen, onSettle }: DrawerSwipeOptions,
): boolean {
  const [dragging, setDragging] = useState(false)

  // Everything the move handler reads lives in a ref. A touchmove fires at
  // frame rate and this hook is bound inside the chat pane, so a re-render per
  // sample would drop frames on the very gesture it is meant to smooth.
  const openRef = useRef(open)
  openRef.current = open
  const onGestureOpenRef = useRef(onGestureOpen)
  onGestureOpenRef.current = onGestureOpen
  const onSettleRef = useRef(onSettle)
  onSettleRef.current = onSettle

  const phase = useRef<'idle' | 'pending' | 'locked'>('idle')
  const startX = useRef(0)
  const startY = useRef(0)
  const travel = useRef(0)
  /**
   * Offset the panel sat at when this gesture locked, and the base every later
   * sample is measured from.
   *
   * Latched, NOT re-read from `open` per move — and that is load-bearing. An
   * opening drag mounts the panel from inside the touchmove handler, React
   * flushes that synchronously, and `open` is therefore already true by the time
   * the SAME handler reaches the tracking line. Reading it there computed the
   * base for an already-open panel (0) instead of the closed one, so the offset
   * clamped to 0 and the panel appeared instantly at rest: a snap, i.e. exactly
   * the behaviour this hook replaces. The base belongs to the gesture, so it is
   * decided once, when the gesture is decided.
   */
  const gestureBase = useRef(0)
  const lastX = useRef(0)
  const lastT = useRef(0)
  const velocity = useRef(0)
  const scroller = useRef<HTMLElement | null>(null)
  const scrollerLeft = useRef(0)

  useEffect(() => {
    const el = ref.current
    if (!el || !enabled) return

    /** Offset the panel rests at while closed, and the gesture's full travel. */
    const closedOffset = () => -window.innerWidth

    /**
     * Take `x` over, then run the shared settle to `to`, reporting `open` once
     * it arrives.
     *
     * `x.stop()` rather than a stop handle this hook kept for its OWN
     * animations. Two writers to one value is the failure mode, and the other
     * writer is not always this hook: the consumer animates the same value
     * programmatically for the header toggle, the backdrop tap and the
     * session-selected close, and discards those handles. `x.set()` does NOT
     * cancel an animation running on the value — only `stop`/`jump` do — so a
     * drag begun inside one of those ~0.32s windows had the drag and the
     * animation both writing every frame, and the panel juddered until the
     * animation ran out. Stopping the VALUE covers every writer, including ones
     * added later, which no amount of handle-tracking here can.
     *
     * Stopping also suppresses the stopped animation's `onComplete`, so a
     * close that a new gesture interrupts cannot later report `closed` over the
     * gesture's own outcome.
     */
    const settle = (to: number, open: boolean) => {
      x.stop()
      animateDrawer(x, to, () => onSettleRef.current(open))
    }

    /** Drop a gesture that had not taken the panel over yet (still deciding its
     *  axis). Nothing was mounted and `x` was never written, so there is no
     *  visual state to put back. */
    const reset = () => {
      phase.current = 'idle'
      scroller.current = null
      setDragging(false)
    }

    /**
     * Give up a gesture that ALREADY owns the panel — a second finger, a
     * `touchcancel` from a system interruption, an incoming call.
     *
     * It is not enough to stop tracking. The panel is sitting wherever the
     * finger left it, and for an opening drag it is also MOUNTED, so merely
     * going idle strands it half-open with the scrim half-dimmed and no
     * animation coming: the release handler is the only other place that ever
     * settles it, and it never runs for a gesture that was cancelled rather
     * than released. So return the panel to the state the gesture STARTED from —
     * which is exactly `gestureBase`, 0 when it was open and the closed offset
     * when it was not — and report that state so the mount follows it.
     */
    const abandon = () => {
      if (phase.current === 'locked') settle(gestureBase.current, gestureBase.current === 0)
      reset()
    }

    const onTouchStart = (e: TouchEvent) => {
      // A second finger LANDING is a pinch or a two-finger scroll, not this
      // gesture — abandon on the spot. Checked before the phase guard on
      // purpose: a locked gesture would otherwise return here and only give the
      // panel up on the next touchmove, so a pinch that holds still would keep
      // it owned and stranded indefinitely.
      if (e.touches.length > 1) { abandon(); return }
      if (phase.current !== 'idle') return
      const touch = e.touches[0]
      if (!openRef.current) {
        const x0 = touch.clientX
        if (x0 < EDGE_START || x0 > EDGE_END) return
      }
      travel.current = window.innerWidth
      startX.current = touch.clientX
      startY.current = touch.clientY
      lastX.current = touch.clientX
      lastT.current = e.timeStamp
      velocity.current = 0
      phase.current = 'pending'
      scroller.current = findHorizontalScroller(e.target, el)
      scrollerLeft.current = scroller.current ? scroller.current.scrollLeft : 0
    }

    const onTouchMove = (e: TouchEvent) => {
      if (phase.current === 'idle') return
      if (e.touches.length > 1) { abandon(); return }
      const touch = e.touches[0]
      const dx = touch.clientX - startX.current
      const dy = touch.clientY - startY.current

      if (phase.current === 'pending') {
        // Vertical intent: the chat scroller owns this touch. Abandon outright
        // rather than staying armed, so a later horizontal wobble during a
        // scroll cannot retroactively claim the gesture.
        if (Math.abs(dy) > Math.abs(dx)) { reset(); return }
        if (Math.abs(dx) < AXIS_LOCK) return
        // A horizontal scroller under the finger (a wide code block, a
        // carousel) owns the gesture while it still has somewhere to go in
        // this direction. Checked at lock time, on the direction now known.
        const sc = scroller.current
        if (sc) {
          if (sc.scrollLeft !== scrollerLeft.current) { reset(); return }
          const maxScrollLeft = sc.scrollWidth - sc.clientWidth
          const canReveal = dx < 0 ? sc.scrollLeft < maxScrollLeft - 1 : sc.scrollLeft > 1
          if (canReveal) { reset(); return }
        }
        // Wrong direction for the current state: closed panels only open on a
        // rightward drag, open panels only close on a leftward one.
        if (openRef.current ? dx > 0 : dx < 0) { reset(); return }
        phase.current = 'locked'
        gestureBase.current = openRef.current ? 0 : closedOffset()
        // Take the value over from ANY animation still running on it — this
        // hook's own settle, or one the consumer started for the toggle, the
        // backdrop tap or a session-selected close. `x.set()` below does not
        // cancel an animation, so without this both write every frame.
        x.stop()
        setDragging(true)
        if (!openRef.current) {
          // Seat the panel offscreen BEFORE it mounts, so the first painted
          // frame is the closed offset rather than a flash at rest position.
          x.set(closedOffset())
          onGestureOpenRef.current()
        }
      }

      if (phase.current !== 'locked') return
      const dt = e.timeStamp - lastT.current
      if (dt > 0) velocity.current = (touch.clientX - lastX.current) / dt
      lastX.current = touch.clientX
      lastT.current = e.timeStamp
      // Clamped to the panel's own range: dragging past open must not lift the
      // panel off its edge, and dragging past closed must not gap it further.
      x.set(Math.max(closedOffset(), Math.min(0, gestureBase.current + dx)))
    }

    const onTouchEnd = (e: TouchEvent) => {
      if (phase.current !== 'locked') { reset(); return }
      const touch = e.changedTouches[0]
      const dx = touch.clientX - startX.current
      // A release more than a frame after the last move is a hold, not a
      // flick — the stale sample would otherwise commit a gesture the finger
      // had already stopped.
      const v = e.timeStamp - lastT.current > 32 ? 0 : velocity.current
      const settledAt = Math.max(closedOffset(), Math.min(0, gestureBase.current + dx))
      const progress = 1 + settledAt / travel.current

      let target: boolean
      if (v > COMMIT_VELOCITY) target = true
      else if (v < -COMMIT_VELOCITY) target = false
      else target = progress > COMMIT_DISTANCE

      reset()
      // `onSettle(false)` unmounts the panel, so it is reported only once the
      // panel is offscreen — unmounting mid-slide is the snap this hook exists
      // to remove.
      settle(target ? 0 : closedOffset(), target)
    }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: true })
    el.addEventListener('touchend', onTouchEnd, { passive: true })
    el.addEventListener('touchcancel', abandon, { passive: true })
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', abandon)
      // Unbinding mid-gesture (a viewport crossing out of mobile, an unmount)
      // must also END the gesture. `phase` is a ref, so it survives the
      // teardown: left at 'locked' it makes the next bind refuse every
      // touchstart, and lets the first touchmove resume from a stale startX —
      // the gesture would be dead until a remount, with one stray jump on the
      // way. The panel itself is not settled here; the consumer's own
      // leaving-mobile reset owns where it ends up.
      phase.current = 'idle'
      scroller.current = null
      setDragging(false)
    }
    // `open` is intentionally absent — see the header note. The callbacks are
    // held in refs for the same reason.
  }, [ref, enabled, x])

  return dragging
}
