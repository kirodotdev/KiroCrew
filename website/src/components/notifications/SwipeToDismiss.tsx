import { useCallback, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'

/**
 * Travel, in px, before a touch counts as a swipe rather than a tap. Below it
 * the card's own tap paths (open, close, an action capsule) are left alone.
 */
export const SWIPE_DISMISS_SLOP = 10
/**
 * Vertical travel, in px, past which the touch is declined outright: the
 * browser has already committed it to a scroller by then (the platform scroll
 * slop is ~8px on the engines that publish one), and `preventDefault` no longer
 * reaches it. Smaller than `SWIPE_DISMISS_SLOP` on purpose, so a diagonal drag
 * that drifted this far vertically before earning its horizontal lock is left
 * to the page rather than fought over. Same rule as `useDrawerSwipe`'s
 * `PLATFORM_SCROLL_SLOP`.
 */
export const SWIPE_DISMISS_SCROLL_SLOP = 8
/**
 * Horizontal travel, in px, past which a release dismisses. Distance is the
 * only criterion, as in the lightbox's swipe: a velocity path would buy a
 * sub-threshold flick and cost per-move rate tracking plus a second
 * threshold, and the flick a user actually makes travels past this anyway.
 */
export const SWIPE_DISMISS_DISTANCE = 80
/**
 * How long after a swipe's release its synthesized `click` is still swallowed.
 *
 * A release fires `click` AFTER `pointerup`, so the swallow cannot be dropped
 * with the gesture -- but it must not stay armed either. A touch that moved
 * past the browser's own tap slop often fires NO click at all, so a flag with
 * no time limit would wait for the next activation that has no pointerdown of
 * its own (a keyboard Enter on Review, an assistive-technology click) and eat
 * that one. Same ceiling as `useDrawerSwipe`'s `CLICK_SWALLOW_MS`: long enough
 * for an engine still carrying the legacy ~300ms tap delay, and the first click
 * it swallows ends it early.
 */
export const SWIPE_CLICK_SWALLOW_MS = 350

interface Gesture {
  pointerId: number
  startX: number
  startY: number
  /** The touch is still a candidate: no axis decided yet. */
  active: boolean
  /** The horizontal axis is locked; the card follows the finger. */
  engaged: boolean
}

const IDLE: Gesture = { pointerId: -1, startX: 0, startY: 0, active: false, engaged: false }

/**
 * Finger-tracking horizontal swipe that dismisses its child.
 *
 * Wraps one notification banner card on the mobile layout. A horizontal drag
 * past `SWIPE_DISMISS_DISTANCE` calls `onDismiss` on release -- the SAME
 * handler the card's close button calls, so the two paths cannot drift apart
 * (the note stays unread in the bell either way). A shorter drag snaps the
 * card back. The gesture is touch-only and horizontal-only:
 *
 * - `touch-action: pan-y` on the wrapper leaves vertical panning to the
 *   browser, so a finger that scrolls the page under the banner is never
 *   taken, and a touch the browser has already committed to a scroll arrives
 *   as `pointercancel`, which aborts the candidate.
 * - The axis is decided once: a touch that drifts past
 *   `SWIPE_DISMISS_SCROLL_SLOP` vertically before it has travelled
 *   `SWIPE_DISMISS_SLOP` horizontally is declined for good, and the card does
 *   not move however the finger turns afterwards.
 * - A mouse never starts a gesture: a desktop pointer has the hover-revealed
 *   close and the swipe would only fight text selection.
 *
 * The offset is an inline `translateX` on the wrapper, not a framer `x`: the
 * card's outer `motion.div` is owned by `AnimatePresence` (its entrance, its
 * deck slot, its exit vector into the bell), and a second animated transform
 * on that element would fight those. The snap-back is a Tailwind
 * `transition-transform`, which the lightbox's swipe already uses for the same
 * gesture class; under `prefers-reduced-motion` the card still follows the
 * finger (direct manipulation is not decoration) but returns with no
 * transition.
 *
 * `enabled={false}` renders the same wrapper with the handlers inert, so the
 * desktop layout gets no extra element swap and no gesture.
 */
export default function SwipeToDismiss({ enabled, reduced, onDismiss, children }: {
  enabled: boolean
  /** `prefers-reduced-motion`: no snap-back transition. */
  reduced: boolean
  onDismiss: () => void
  children: ReactNode
}) {
  const [offset, setOffset] = useState(0)
  const [swiping, setSwiping] = useState(false)
  const gesture = useRef<Gesture>(IDLE)
  // The release of a real swipe may still fire `click` on whatever the finger
  // landed on (the card body opens the note, the X dismisses). A swipe that
  // snapped back must not ALSO open the bell; one that dismissed must not run
  // the open path on a card that is already leaving. Held as a deadline, not
  // a flag: see `SWIPE_CLICK_SWALLOW_MS`.
  const swallowUntil = useRef(0)

  const reset = useCallback(() => {
    gesture.current = IDLE
    setSwiping(false)
    setOffset(0)
  }, [])

  const onPointerDown = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    swallowUntil.current = 0
    if (!enabled || e.pointerType === 'mouse' || gesture.current.active) return
    gesture.current = { pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, active: true, engaged: false }
  }, [enabled])

  const onPointerMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    const g = gesture.current
    if (!g.active || e.pointerId !== g.pointerId) return
    const dx = e.clientX - g.startX
    const dy = e.clientY - g.startY
    if (!g.engaged) {
      // Declined, not deferred: the browser owns a touch this far down the
      // vertical axis, and the card must not start moving under it later.
      if (Math.abs(dy) > SWIPE_DISMISS_SCROLL_SLOP) { gesture.current = IDLE; return }
      if (Math.abs(dx) < SWIPE_DISMISS_SLOP) return
      // No separate "more horizontal than vertical" test: with the scroll slop
      // below the lock distance, any touch that gets here is already so.
      g.engaged = true
      setSwiping(true)
      // Capture so the card keeps hearing the finger once it leaves the card's
      // box. Best-effort: an engine that refuses still delivers the moves that
      // land on the card, and the release path below reads the last one.
      try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* uncaptured drag still follows while over the card */ }
    }
    setOffset(dx)
  }, [])

  const end = useCallback((e: ReactPointerEvent<HTMLDivElement>, cancelled: boolean) => {
    const g = gesture.current
    if (!g.active || e.pointerId !== g.pointerId) return
    if (!g.engaged) { gesture.current = IDLE; return }
    // Capture, when it was granted, is released by the browser on up/cancel.
    // Only a RELEASE can fire the click to swallow, and only within the window:
    // a cancel fires none, and a release past the tap slop may fire none either,
    // so anything armed for longer would wait for a later activation that has
    // no pointerdown to clear it (a keyboard Enter on Review) and eat that.
    swallowUntil.current = cancelled ? 0 : Date.now() + SWIPE_CLICK_SWALLOW_MS
    if (!cancelled && Math.abs(e.clientX - g.startX) >= SWIPE_DISMISS_DISTANCE) {
      // The card is leaving: AnimatePresence plays the exit from wherever the
      // wrapper sits, so the offset is left in place rather than snapped.
      gesture.current = IDLE
      setSwiping(false)
      onDismiss()
      return
    }
    reset()
  }, [onDismiss, reset])

  const onPointerUp = useCallback((e: ReactPointerEvent<HTMLDivElement>) => end(e, false), [end])
  const onPointerCancel = useCallback((e: ReactPointerEvent<HTMLDivElement>) => end(e, true), [end])

  const onClickCapture = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (Date.now() >= swallowUntil.current) return
    swallowUntil.current = 0
    e.preventDefault()
    e.stopPropagation()
  }, [])

  return (
    <div
      data-testid="notification-banner-swipe"
      data-swiping={swiping ? 'true' : undefined}
      className={`${enabled ? 'touch-pan-y' : ''} ${swiping || reduced ? '' : 'transition-transform duration-200 ease-out'}`}
      style={offset !== 0 ? { transform: `translateX(${offset.toFixed(1)}px)` } : undefined}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onClickCapture={onClickCapture}
    >
      {children}
    </div>
  )
}
