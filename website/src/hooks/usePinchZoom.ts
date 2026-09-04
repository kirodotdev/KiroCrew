import { useCallback, useEffect, useRef, useState } from 'react'

/** Two-finger pinch-to-zoom with focal anchoring and pan clamping, for a
 *  full-viewport viewer that owns its own magnification.
 *
 *  Why this is a hook rather than per-viewer code: browser page zoom is off on
 *  touch across the shell (viewport meta in `index.html`, root `touch-action` in
 *  `index.css`, `gesturestart` suppression in `utils/pageZoom.ts`), because
 *  magnifying a fixed-height app shell strands the user in a layout with no
 *  scroll axis to reach what moved off-screen. The rule that buys is *"a surface
 *  that must magnify owns its own zoom"* — and this codebase has TWO such
 *  surfaces, the image `Lightbox` and `DiagramLightbox`. The first shipped this
 *  gesture; the second shipped without it and became unmagnifiable by any
 *  gesture, which is the regression this hook exists to make unrepeatable.
 *
 *  What the caller still owns, deliberately: the ONE-finger gestures. The image
 *  viewer has swipe-to-dismiss and a drag-pan on the `<img>`; the diagram viewer
 *  has neither. Those compete with a pinch over the same contacts, and only the
 *  caller knows what to surrender — so `onPinchStart` fires when the second
 *  finger lands and the caller drops whatever one-finger gesture it had in
 *  flight. Folding those in here would mean this hook knowing about swipe
 *  thresholds and image drags it has no business knowing about.
 */

type Point = { x: number; y: number }

/** Shared double-tap bounds across full-viewport magnification surfaces. */
export const DOUBLE_TAP_MS = 300
export const DOUBLE_TAP_SLOP = 32
export const DOUBLE_TAP_ZOOM = 2.5

/** Divisor turning a `wheel` deltaY into a multiplicative zoom step. 100 is the
 *  conventional "one notch" unit, so a trackpad's many small deltas accumulate
 *  smoothly while a mouse notch lands near the clamp below. */
const WHEEL_ZOOM_DIVISOR = 100
/** Ceiling on ONE wheel event's factor (and, inverted, its floor). Without it a
 *  single mouse notch would scale by e^-1 ≈ 0.37 — a jump, not a zoom. */
const WHEEL_STEP_MAX = 1.25
/** `deltaY` is only in pixels when `deltaMode` is 0. Firefox reports a mouse
 *  notch as `DOM_DELTA_LINE` with `deltaY ≈ 3`, which against the divisor above
 *  is a ~3% step instead of the intended ~25% — the same physical notch zooming
 *  roughly 8× slower. 33 is 100/3: it maps Firefox's 3-line notch onto the
 *  100px notch the divisor is tuned for. The value only has to be close —
 *  `WHEEL_STEP_MAX` bounds the result either way, so an imperfect factor cannot
 *  produce a jump, only a slightly brisker or gentler notch. */
const WHEEL_LINE_PX = 33

/** Safari's non-standard pinch events, which no DOM lib type covers. `scale` is
 *  cumulative from `gesturestart`, not per-frame. */
type GestureEventLike = Event & { scale: number; clientX: number; clientY: number }

/* The three helpers below are module-private on purpose. They are the pinch's
 * internals, not its interface: both consumers drive the gesture through the
 * hook's returned handlers and never compute a distance or a pair themselves.
 * Exporting them would publish an API with no caller, which the next consumer
 * would then bind to — and a second entry point into this math is the exact
 * divergence the extraction exists to prevent. */

/** Distance between two pointer positions, for the pinch scale factor. */
function pointerDistance(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

/** Midpoint of a pinch — the point the zoom is anchored to, so the detail under
 *  the fingers stays under the fingers instead of sliding away from them. */
function pinchMidpoint(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

/** The two contacts that own a pinch: the first two live ones, in insertion order,
 *  plus a `key` identifying that exact pair.
 *
 *  The pair is DERIVED on every read rather than stored as two pointer ids, and
 *  that is the invariant the gesture rests on. Storing ids leaves the baseline
 *  naming a pointer that has since lifted whenever the contact set changes in a
 *  way a `size < 2` test cannot see — a third finger lands, then one of the
 *  original two lifts while two contacts remain. The stored id is then absent
 *  from the map, every later move reads `undefined` for it, and the pinch is dead
 *  until the user lifts everything and starts over. Deriving the pair makes that
 *  state unrepresentable: the pair is always live by construction, and a change
 *  of `key` is the signal to re-seat the baseline. */
function pinchPair(points: Map<number, Point>): { key: string; a: Point; b: Point } | null {
  if (points.size < 2) return null
  const entries = points.entries()
  const [idA, a] = entries.next().value as [number, Point]
  const [idB, b] = entries.next().value as [number, Point]
  return { key: `${idA}:${idB}`, a, b }
}

export type PinchZoomOptions = {
  /** The element whose layout box bounds the pan. Its `offsetWidth/Height` times
   *  the current zoom is the visual size; travel is allowed up to half the
   *  overflow beyond the viewport. A null ref leaves a candidate pan unclamped. */
  targetRef: { current: HTMLElement | null }
  /** Element whose subtree claims a trackpad gesture. Defaults to `targetRef`,
   *  but a viewer whose transform target is smaller than its overlay should pass
   *  the overlay: a pinch on the letterbox around a small image is visually
   *  inside the viewer, and letting it fall through page-zooms the whole app
   *  behind a viewer that looks unchanged. */
  containRef?: { current: HTMLElement | null }
  /** Whether the trackpad path is bound at all. Default true.
   *
   *  Pass false whenever the consumer cannot act on a zoom — a viewer that is
   *  closed, or content that is not fit-scaled. Two distinct costs ride on this:
   *  a non-passive `wheel` listener makes the compositor wait on main-thread
   *  dispatch for EVERY wheel event in the app, so an always-mounted consumer
   *  would tax scrolling everywhere while its viewer is shut; and claiming a
   *  gesture the consumer ignores would suppress the browser page zoom that DOES
   *  magnify content which is not fit-to-viewport, turning a working fallback
   *  into a dead end. */
  enabled?: boolean
  min: number
  max: number
  /** Fires when a pinch seats (the second contact lands). The caller uses it to
   *  drop any one-finger gesture it had in flight — see the note above. */
  onPinchStart?: () => void
  /** Fires when the last-but-one contact lifts, i.e. the pinch is over. The
   *  caller uses it to mark the synthesised click as not-a-tap, so a finished
   *  pinch does not also dismiss the viewer the user just zoomed into. */
  onPinchEnd?: () => void
}

export function usePinchZoom({ targetRef, containRef, enabled = true, min, max, onPinchStart, onPinchEnd }: PinchZoomOptions) {
  const [zoom, setZoom] = useState(min)
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 })
  const [pinching, setPinching] = useState(false)

  /** Live zoom/pan for the pointer closures. A `useCallback([])` closure would
   *  hand the handlers the values from first render, and the pinch specifically
   *  needs what is on screen NOW to re-seat its baseline. */
  const zoomRef = useRef(zoom)
  zoomRef.current = zoom
  const panRef = useRef(pan)
  panRef.current = pan

  /** Last `scale` seen from a WebKit gesture, so each frame's factor is the ratio
   *  against the previous one rather than the cumulative value. */
  const gestureScaleRef = useRef(1)

  /** Hold a candidate zoom inside bounds. Rounded because a pinch produces a
   *  continuous factor, and an unrounded float would make the `zoom > min`
   *  checks (which gate panning and any fit-only gesture) flip on a residue like
   *  1.0000000000000002 after a pinch back down to fit. */
  const clampZoom = useCallback((z: number) => Math.min(max, Math.max(min, +z.toFixed(3))), [min, max])

  /** Clamp a candidate pan so the content can't be flung entirely off-screen.
   *
   *  `z` is explicit because the pinch clamps against the zoom it is about to
   *  SET, not the one on screen: reading `zoomRef` there would clamp a zoomed-in
   *  pan against the smaller previous box and snap the content back toward centre
   *  on every frame of the gesture. */
  const clampPan = useCallback((x: number, y: number, z: number = zoomRef.current): Point => {
    const el = targetRef.current
    if (!el) return { x, y }
    const maxX = Math.max(0, (el.offsetWidth * z - window.innerWidth) / 2)
    const maxY = Math.max(0, (el.offsetHeight * z - window.innerHeight) / 2)
    return { x: Math.min(maxX, Math.max(-maxX, x)), y: Math.min(maxY, Math.max(-maxY, y)) }
  }, [targetRef])

  /** Live contacts, tracked in a map because pointer events carry ONE pointer
   *  each: the second finger's position is only ever knowable from what an
   *  earlier event stored. */
  const pointsRef = useRef(new Map<number, Point>())
  /** `key` is the identity of the pair the baseline was measured from — '' means
   *  no pinch is armed. Everything else is that baseline: the finger distance and
   *  the zoom/pan/midpoint the content sat at when the pair was seated. Scale and
   *  pan are both derived from it, so the gesture is a pure function of the live
   *  contacts and never accumulates drift. */
  const pinchRef = useRef({ key: '', startDist: 0, baseZoom: min, startMid: { x: 0, y: 0 }, basePan: { x: 0, y: 0 } })

  const resetPinch = useCallback(() => {
    pinchRef.current = { key: '', startDist: 0, baseZoom: min, startMid: { x: 0, y: 0 }, basePan: { x: 0, y: 0 } }
  }, [min])

  const endPinch = useCallback(() => {
    if (pinchRef.current.key === '') return
    resetPinch()
    setPinching(false)
    onPinchEnd?.()
  }, [resetPinch, onPinchEnd])

  /** Measure the baseline for `pair` from what is on screen right now. Called both
   *  when a pinch begins and whenever the live pair changes identity mid-gesture —
   *  re-seating from the CURRENT zoom and pan is what makes a finger landing or
   *  lifting continue the gesture smoothly instead of snapping the content back to
   *  where the previous pair started. */
  const seatPinch = useCallback((pair: NonNullable<ReturnType<typeof pinchPair>>) => {
    const dist = pointerDistance(pair.a, pair.b)
    // Two contacts at the same point give no baseline to scale against; leave the
    // pinch unseated and let the next move (or lift) sort the gesture out.
    if (dist <= 0) return
    pinchRef.current = {
      key: pair.key, startDist: dist, baseZoom: zoomRef.current,
      startMid: pinchMidpoint(pair.a, pair.b), basePan: panRef.current,
    }
    setPinching(true)
  }, [])

  /** Record a contact and seat a pinch if this is the second one. Returns true
   *  when a pinch owns the gesture, so the caller can stop before starting a
   *  one-finger gesture of its own. Mouse pointers are ignored — a pinch is a
   *  touch gesture, and claiming mouse events would break desktop click paths. */
  const trackPointerDown = useCallback((e: { pointerId: number; clientX: number; clientY: number; pointerType: string }): boolean => {
    if (e.pointerType === 'mouse') return false
    // Record BEFORE any bail-out in the caller. A pinch is only knowable from two
    // tracked contacts, and a caller that returns early would mean the second
    // finger is never seen in exactly the cases a pinch is most likely to start from.
    pointsRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    const pair = pinchPair(pointsRef.current)
    if (!pair) return false
    seatPinch(pair)
    onPinchStart?.()
    return true
  }, [seatPinch, onPinchStart])

  /** Apply a pinch frame. Returns true when a pinch consumed the move. */
  const trackPointerMove = useCallback((e: { pointerId: number; clientX: number; clientY: number }): boolean => {
    if (pointsRef.current.has(e.pointerId)) pointsRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    const pair = pinchPair(pointsRef.current)
    if (!pair) return false
    const p = pinchRef.current
    // The live pair is not the one the baseline was measured from (a finger joined
    // or left). Re-seat and wait for the next move rather than scaling this frame
    // against a distance from a different pair of fingers.
    if (p.key !== pair.key) { seatPinch(pair); return true }
    const next = clampZoom(p.baseZoom * (pointerDistance(pair.a, pair.b) / p.startDist))
    // Anchor the scale at the gesture midpoint. The content renders as
    // `translate(pan) scale(zoom)` about its centre, so what sat under the midpoint
    // when the pinch was seated is at content-local offset
    // `(startMid - centre - basePan) / baseZoom`; holding that offset under the
    // CURRENT midpoint is what keeps a corner detail under the fingers instead of
    // pushing it away as the content grows around its centre. Using the live
    // midpoint (not the seated one) also makes two fingers moving together pan.
    const cx = window.innerWidth / 2
    const cy = window.innerHeight / 2
    const anchorX = (p.startMid.x - cx - p.basePan.x) / p.baseZoom
    const anchorY = (p.startMid.y - cy - p.basePan.y) / p.baseZoom
    const mid = pinchMidpoint(pair.a, pair.b)
    setZoom(next)
    setPan(clampPan(mid.x - cx - anchorX * next, mid.y - cy - anchorY * next, next))
    return true
  }, [clampZoom, clampPan, seatPinch])

  /** Drop a contact. Ends the pinch on the FIRST lift (rather than the last),
   *  which is what stops the finger still down from being re-read as a one-finger
   *  gesture whose origin is wherever the pinch happened to leave it. */
  const trackPointerUp = useCallback((e: { pointerId: number }) => {
    pointsRef.current.delete(e.pointerId)
    if (pointsRef.current.size < 2) endPinch()
  }, [endPinch])

  /** Return to fit and clear any pan. */
  const reset = useCallback(() => {
    setZoom(min)
    setPan({ x: 0, y: 0 })
    pointsRef.current.clear()
    resetPinch()
    setPinching(false)
  }, [min, resetPinch])

  /** Scale by `factor` while holding whatever sits under (`focalX`, `focalY`).
   *
   *  Same anchoring identity the two-finger path uses, with the baseline read from
   *  what is on screen rather than from a seated gesture: the content renders as
   *  `translate(pan) scale(zoom)` about its centre, so the detail under the focal
   *  point is at content-local offset `(focal - centre - pan) / zoom`, and keeping
   *  that offset under the focal point after the scale is what holds it under the
   *  cursor instead of letting it drift outward as the content grows. */
  const zoomAbout = useCallback((factor: number, focalX: number, focalY: number) => {
    const from = zoomRef.current
    const next = clampZoom(from * factor)
    if (next === from) return
    const cx = window.innerWidth / 2
    const cy = window.innerHeight / 2
    // Fall back to the centre when an event arrives without usable coordinates.
    // Pan PERSISTS, so a single non-finite focal would not just misplace one frame
    // — it would write NaN into the stored pan and leave the content invisible
    // until a reset, which is a much worse outcome than an unanchored zoom.
    const fx = Number.isFinite(focalX) ? focalX : cx
    const fy = Number.isFinite(focalY) ? focalY : cy
    const anchorX = (fx - cx - panRef.current.x) / from
    const anchorY = (fy - cy - panRef.current.y) / from
    setZoom(next)
    setPan(clampPan(fx - cx - anchorX * next, fy - cy - anchorY * next, next))
  }, [clampZoom, clampPan])

  /** Trackpad magnification, which reaches none of the pointer code above.
   *
   *  A trackpad pinch produces NO pointer events at all: Blink reports it as a
   *  `wheel` carrying `ctrlKey`, WebKit as `gesturestart`/`gesturechange` carrying
   *  a cumulative `scale`. So a laptop had no way to magnify a fit-scaled surface —
   *  the browser's own page zoom is the default action for both, and page zoom
   *  cannot magnify content that re-fits to the viewport it has just shrunk.
   *  Claiming these two signals is what gives a trackpad, and `ctrl`+scroll on a
   *  mouse, the same magnification the two-finger path gives a touchscreen.
   *
   *  Four details are load-bearing:
   *
   *  - The listeners are NON-PASSIVE and are not React props. React attaches
   *    `wheel` at the root passively, so `preventDefault()` inside an `onWheel`
   *    prop is ignored and the page zooms anyway.
   *  - They sit on `window` and gate on the target being inside `containRef`,
   *    rather than on the element itself. A viewer's element ref is null until it
   *    opens, so an effect that read the element at mount would bind nothing.
   *    Binding is instead gated by `enabled`, which the consumer sets from its own
   *    open/zoomable state — that is what keeps a non-passive listener off `window`
   *    while there is nothing to zoom, and what stops a claimed-but-ignored gesture
   *    from suppressing a page zoom that would have worked.
   *  - Only `ctrl`+wheel is claimed. A plain wheel stays with whatever scroller
   *    owns it, which is what a no-viewBox diagram depends on to reach its edges.
   *  - `gesture*` is bound only under `(pointer: fine)`. The reverse of the first
   *    sentence does NOT hold: a gesture event does not imply a trackpad. iOS
   *    Safari fires `gesturestart`/`gesturechange` for a two-finger TOUCH pinch as
   *    well, and those same fingers are already driving the pointer path above — so
   *    binding both on a touch device runs two independent formulas over one pinch
   *    and zooms twice. The media query is what keeps this an ADDITIONAL input path
   *    for pointing devices instead of a second one for touch.
   */
  useEffect(() => {
    if (!enabled) return
    const inTarget = (e: Event): boolean => {
      const el = (containRef ?? targetRef).current
      return !!el && e.target instanceof Node && el.contains(e.target)
    }
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey || !inTarget(e)) return
      e.preventDefault()
      // Exponentiating keeps the step multiplicative, so a given physical gesture
      // covers the same proportion of the range at any current zoom. The clamp is
      // what makes one event type serve two very different granularities: a
      // trackpad emits many small deltas, while one mouse notch is ~100 and would
      // otherwise jump by e^-1 in a single tick.
      // `deltaY` carries no unit of its own — `deltaMode` names it. Normalise to
      // pixels FIRST, or the same physical notch means different things per engine.
      const px =
        e.deltaMode === 1 ? e.deltaY * WHEEL_LINE_PX
        : e.deltaMode === 2 ? e.deltaY * (window.innerHeight || WHEEL_ZOOM_DIVISOR)
        : e.deltaY
      const raw = Math.exp(-px / WHEEL_ZOOM_DIVISOR)
      zoomAbout(Math.min(WHEEL_STEP_MAX, Math.max(1 / WHEEL_STEP_MAX, raw)), e.clientX, e.clientY)
    }
    const onGestureStart = (e: Event) => {
      if (!inTarget(e)) return
      e.preventDefault()
      gestureScaleRef.current = 1
    }
    const onGestureChange = (e: Event) => {
      if (!inTarget(e)) return
      e.preventDefault()
      const g = e as GestureEventLike
      if (typeof g.scale !== 'number' || !(g.scale > 0)) return
      // `scale` is cumulative from gesture start, so this frame's factor is the
      // ratio against the previous frame rather than the value itself.
      const factor = g.scale / gestureScaleRef.current
      gestureScaleRef.current = g.scale
      zoomAbout(factor, g.clientX, g.clientY)
    }
    const onGestureEnd = (e: Event) => {
      if (!inTarget(e)) return
      e.preventDefault()
      gestureScaleRef.current = 1
    }
    window.addEventListener('wheel', onWheel, { passive: false })
    // `gesture*` is bound ONLY under a fine primary pointer, and `wheel` never is.
    // Absent `matchMedia` (jsdom, SSR) counts as NOT fine: failing closed costs a
    // trackpad path on a platform that does not exist, while failing open restores
    // the double zoom on every touch device.
    const finePointer =
      typeof window.matchMedia === 'function' && window.matchMedia('(pointer: fine)').matches
    if (finePointer) {
      window.addEventListener('gesturestart', onGestureStart, { passive: false })
      window.addEventListener('gesturechange', onGestureChange, { passive: false })
      window.addEventListener('gestureend', onGestureEnd, { passive: false })
    }
    return () => {
      window.removeEventListener('wheel', onWheel)
      if (finePointer) {
        window.removeEventListener('gesturestart', onGestureStart)
        window.removeEventListener('gesturechange', onGestureChange)
        window.removeEventListener('gestureend', onGestureEnd)
      }
    }
  }, [enabled, containRef, targetRef, zoomAbout])

  // Return exactly what a consumer CALLS or READS — never what it merely names.
  // A handle nothing invokes is a second entry point into this gesture's maths,
  // which is what having one hook instead of two copies exists to prevent; and a
  // name that appears only in a dependency array reads as consumed while being
  // dead. `endPinch` is deliberately absent: `trackPointerUp` calls it, so inside
  // the hook is the only place it belongs.
  return {
    zoom, setZoom, pan, setPan, pinching,
    zoomRef, clampPan,
    trackPointerDown, trackPointerMove, trackPointerUp,
    reset,
  }
}
