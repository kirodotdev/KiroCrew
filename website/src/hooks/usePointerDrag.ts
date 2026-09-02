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
    startX: 0, startY: 0, active: false, committed: false,
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
    optsRef.current.onEnd?.({
      dx: e.clientX - s.startX,
      dy: e.clientY - s.startY,
      x: e.clientX,
      y: e.clientY,
      first: false,
      committed: s.committed,
    })
    s.committed = false
  }, [])

  return { onPointerDown, onPointerMove, onPointerUp: end, onPointerCancel: end }
}
