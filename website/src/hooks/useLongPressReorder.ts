import { useCallback, useEffect, useRef, useState } from 'react'
import { useDragControls } from 'framer-motion'
import type { DragControls } from 'framer-motion'
import type React from 'react'

/**
 * How long a finger must rest on a chip before it arms a reorder drag. Long
 * enough that a flick to scroll never reaches it (a pan starts within ~100ms),
 * short enough to feel like a deliberate press-and-hold — the same band
 * iOS/Android use for list reordering.
 */
export const LONG_PRESS_MS = 450

/**
 * Movement that cancels a pending arm. Above the browser's own pan threshold
 * (~5px) so the gesture the reader started — scrolling the strip — wins, and
 * above the jitter a resting thumb produces.
 */
export const LONG_PRESS_SLOP_PX = 10

export interface LongPressReorderItemProps {
  /** framer's own pointer listener is OFF — see the hook docstring. */
  dragListener: false
  dragControls: DragControls
  onPointerDown: (e: React.PointerEvent) => void
  draggable: false
  style: React.CSSProperties
}

/**
 * Make a `Reorder.Item` reorderable WITHOUT stealing the touch gesture that
 * scrolls the strip it lives in.
 *
 * framer-motion applies `touch-action: pan-y` to every `drag="x"` item
 * (`pan-x` for `drag="y"`), which tells the browser it may not pan along the
 * drag axis. On a horizontal strip of chips inside `overflow-x-auto` that means
 * a touch swipe can never scroll the strip: the browser refuses, framer takes
 * the pointer, and the chip the finger landed on is dragged into a new position
 * instead. Every tab past the visible edge becomes unreachable on a phone, and
 * the attempt to reach it silently reorders the tabs.
 *
 * So the pointer listener is ours, not framer's, and it splits by input:
 *
 * - **Touch** arms the drag only after a press-and-hold that does not move,
 *   which is the platform convention for reordering by touch. Until then the
 *   element declares no `touch-action`, so the browser pans the strip normally.
 * - **Mouse and pen** start the drag on press, exactly as framer's own listener
 *   does — a precise pointer has no gesture to disambiguate, and a hold there
 *   would be a regression.
 *
 * Once a touch drag is armed the browser must stop panning, and flipping
 * `touch-action` cannot do it: the value is read when the touch begins, so a
 * change mid-gesture is ignored. A non-passive `touchmove` blocker is the only
 * mechanism that works after the fact, and it is installed for exactly as long
 * as the drag lasts.
 *
 * Returns `dragging` so the caller can show that the hold registered — with a
 * long press the reader gets no feedback until they move, and without a cue a
 * successful arm is indistinguishable from a failed one.
 */
export function useLongPressReorder(): { itemProps: LongPressReorderItemProps; dragging: boolean } {
  const dragControls = useDragControls()
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cleanupRef = useRef<(() => void) | null>(null)
  const [dragging, setDragging] = useState(false)

  const clearPending = useCallback(() => {
    if (timerRef.current != null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    cleanupRef.current?.()
    cleanupRef.current = null
  }, [])

  useEffect(() => clearPending, [clearPending])

  // Bound to the WINDOW, not to the item: a drag ends wherever the pointer is
  // released, which for a mouse is routinely outside the chip, so an element
  // handler would miss the release and leave the blocker installed.
  useEffect(() => {
    if (!dragging) return
    const blockPan = (e: TouchEvent) => e.preventDefault()
    const stop = () => setDragging(false)
    document.addEventListener('touchmove', blockPan, { passive: false })
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
    return () => {
      document.removeEventListener('touchmove', blockPan)
      window.removeEventListener('pointerup', stop)
      window.removeEventListener('pointercancel', stop)
    }
  }, [dragging])

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    clearPending()
    if (e.pointerType !== 'touch') {
      setDragging(true)
      dragControls.start(e)
      return
    }
    const originX = e.clientX
    const originY = e.clientY
    // The native event outlives this handler (React stopped pooling in 17), and
    // framer reads only the press point from it — which is precisely the point
    // the hold happened at.
    const origin = e.nativeEvent
    const onMove = (ev: PointerEvent) => {
      if (Math.abs(ev.clientX - originX) > LONG_PRESS_SLOP_PX || Math.abs(ev.clientY - originY) > LONG_PRESS_SLOP_PX) {
        clearPending()
      }
    }
    window.addEventListener('pointermove', onMove, { passive: true })
    window.addEventListener('pointerup', clearPending)
    window.addEventListener('pointercancel', clearPending)
    cleanupRef.current = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', clearPending)
      window.removeEventListener('pointercancel', clearPending)
    }
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      clearPending()
      setDragging(true)
      dragControls.start(origin)
    }, LONG_PRESS_MS)
  }, [clearPending, dragControls])

  return {
    itemProps: {
      dragListener: false,
      dragControls,
      onPointerDown,
      // Both are framer's own defaults for a draggable item, and both are
      // skipped while `dragListener` is false: without them a hold on touch
      // raises the selection callout instead of arming the drag, and a press on
      // desktop can start a native HTML drag with its own ghost image.
      draggable: false,
      style: { userSelect: 'none', WebkitUserSelect: 'none', WebkitTouchCallout: 'none' },
    },
    dragging,
  }
}
