import { useCallback, useRef, useState } from 'react'

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
  targetRef: { current: { offsetWidth: number; offsetHeight: number } | null }
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

export function usePinchZoom({ targetRef, min, max, onPinchStart, onPinchEnd }: PinchZoomOptions) {
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
