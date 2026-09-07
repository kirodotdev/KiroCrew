import { useCallback, useRef } from 'react'

/**
 * Shared Pointer-Events drag hook — one implementation for resizers across the
 * app that:
 *   - works on touch as well as mouse (Pointer Events + setPointerCapture),
 *   - applies a movement threshold before committing to a drag (hysteresis),
 *   - survives the pointer leaving the element bounds (capture).
 */

export interface PointerDragState {
  /** total delta from the drag origin */
  dx: number
  dy: number
  /** current pointer position */
  x: number
  y: number
  /** true on the first committed move of this drag */
  first: boolean
  /** whether the gesture crossed the movement threshold. Only meaningful in onEnd
   *  (onMove only fires once committed); lets consumers skip drag-only work on a tap. */
  committed?: boolean
}

export interface PointerDragOptions {
  onStart?: (e: React.PointerEvent) => void
  onMove: (state: PointerDragState) => void
  onEnd?: (state: PointerDragState) => void
  /** px of movement required before the drag commits (default 10, per Apple's ~10px hysteresis). Set 0 to commit immediately. */
  threshold?: number
}

interface DragInternal {
  startX: number
  startY: number
  /** last position from a user-driven coordinate-bearing event (down/move/up).
   *  Platform-fired ends can carry sentinel coordinates: the Pointer Events
   *  spec leaves got/lostpointercapture coordinates undefined, and engines
   *  have shipped pointercancel with 0,0 (the spec needed an explicit
   *  clarification that cancel coordinates must match the last dispatched
   *  event) — so the end payload derives from this instead of trusting a
   *  terminal event. */
  lastX: number
  lastY: number
  active: boolean
  committed: boolean
}

/**
 * Attach the returned handlers to a drag handle:
 *   const drag = usePointerDrag({ onMove: ({ dx }) => setWidth(w0 - dx), threshold: 10 })
 *   <div {...drag} />
 */
export function usePointerDrag(opts: PointerDragOptions) {
  const st = useRef<DragInternal>({
    startX: 0, startY: 0, lastX: 0, lastY: 0, active: false, committed: false,
  })
  const optsRef = useRef(opts)
  optsRef.current = opts

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    // Only start on the primary mouse button; touch/pen have button 0 or -1.
    if (e.pointerType === 'mouse' && e.button !== 0) return
    const el = e.currentTarget as HTMLElement
    try { el.setPointerCapture(e.pointerId) } catch { /* capture is best-effort */ }
    const s = st.current
    s.startX = e.clientX
    s.startY = e.clientY
    s.lastX = e.clientX
    s.lastY = e.clientY
    s.active = true
    s.committed = (optsRef.current.threshold ?? 10) <= 0
    optsRef.current.onStart?.(e)
    if (s.committed) {
      optsRef.current.onMove({ dx: 0, dy: 0, x: e.clientX, y: e.clientY, first: true })
    }
    e.preventDefault()
  }, [])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const s = st.current
    if (!s.active) return
    // Track pre-threshold moves too: a capture loss during hysteresis must
    // still end from the true (small) delta, not a stale origin.
    s.lastX = e.clientX
    s.lastY = e.clientY
    const dx = e.clientX - s.startX
    const dy = e.clientY - s.startY
    const threshold = optsRef.current.threshold ?? 10
    if (!s.committed) {
      if (Math.hypot(dx, dy) < threshold) return
      s.committed = true
    }
    optsRef.current.onMove({ dx, dy, x: e.clientX, y: e.clientY, first: false })
  }, [])

  const end = useCallback((e: React.PointerEvent) => {
    const s = st.current
    if (!s.active) return
    s.active = false
    try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId) } catch { /* best-effort */ }
    // Always fire onEnd once a drag has STARTED (onStart runs unconditionally on
    // pointer-down), even for a sub-threshold tap that never committed — so a
    // consumer that set teardown-critical state in onStart (e.g. a "dragging"
    // suppression flag) is guaranteed the paired teardown. Without this, a stray
    // click on a thin handle would leave that state set forever. dx/dy reflect
    // actual movement (≈0 for a tap); `committed` tells the consumer whether the
    // gesture crossed the threshold so it can skip drag-only work.
    //
    // The payload derives from the last coordinate-bearing event, not from
    // this event unconditionally. This is an ALLOW-list: only pointerup — the
    // user-driven end whose coordinates are spec-defined — refreshes the
    // tracker. Every platform-fired end is excluded, because their
    // coordinates are unreliable by spec or by shipped engines: the Pointer
    // Events spec leaves got/lostpointercapture coordinates undefined, and
    // engines have delivered pointercancel with 0,0 (Pointer Events L3 added
    // an explicit clarification that cancel coordinates must match the last
    // dispatched event precisely because behavior diverged). Trusting a
    // sentinel would hand consumers dx of roughly -startX: resizers run
    // persist(apply(sign * dx)) in onEnd, which would snap the pane to a
    // clamped extreme or its collapsed state and WRITE it to localStorage,
    // a persisted wrong layout on the exact path this hook exists to heal.
    // On a conformant engine the excluded cancel carries the last dispatched
    // coordinates — exactly what the tracker already holds — so committing
    // the tracked position is lossless there and fail-safe everywhere else.
    if (e.type === 'pointerup') {
      s.lastX = e.clientX
      s.lastY = e.clientY
    }
    optsRef.current.onEnd?.({
      dx: s.lastX - s.startX,
      dy: s.lastY - s.startY,
      x: s.lastX,
      y: s.lastY,
      first: false,
      committed: s.committed,
    })
    s.committed = false
  }, [])

  // lostpointercapture is the terminal event the Pointer Events spec fires
  // when capture ends for ANY reason (explicit release, a capture steal by
  // another element, browser-initiated cancellation). Without it, a drag
  // whose capture dies mid-gesture never delivers onEnd: pointerup stops
  // being retargeted to this element, so consumer onStart side effects
  // (several resizers suppress body-wide text selection; others pin
  // body.cursor or teardown-critical dragging flags) stay stuck while the
  // component remains mounted and its unmount guards never run. The normal
  // end path stays single-fire: `end` flips `s.active` false BEFORE calling
  // releasePointerCapture, so the lostpointercapture that release triggers
  // is a no-op re-entry.
  return {
    onPointerDown,
    onPointerMove,
    onPointerUp: end,
    onPointerCancel: end,
    onLostPointerCapture: end,
  }
}
