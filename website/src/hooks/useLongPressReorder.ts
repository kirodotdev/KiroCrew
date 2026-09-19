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

interface LongPressReorderOptions {
  /**
   * Called when a touch hold arms and the finger then lifts WITHOUT moving —
   * the same hold that, had the finger moved, would have dragged the chip. A
   * chip with a context menu passes its opener here, so one touch gesture
   * carries both: hold and drag reorders, hold and release opens the menu.
   * `target` is the element the press landed on.
   */
  onHoldRelease?: (e: PointerEvent, target: HTMLElement) => void
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
 * A chip that also has a context menu shares the hold with it rather than
 * giving it up: the hold arms the drag as above, and what the finger does next
 * decides — moving reorders, lifting in place calls `onHoldRelease`, which the
 * chip uses to open its menu at the release point. That is the iOS home-screen
 * model (hold, then either drag or let go for the menu), so it needs no new
 * gesture. Two guards keep the two owners of the hold from colliding:
 *
 * - A Radix `ContextMenuTrigger` wrapping the chip arms its OWN touch timer
 *   (700ms) and would open the menu mid-arm, so when `onHoldRelease` is set the
 *   touch press is default-prevented — `composeEventHandlers` then skips the
 *   trigger's handler. A prevented `pointerdown` only suppresses the
 *   compatibility mouse events; `click` still fires and panning is unaffected.
 * - Android fires a native `contextmenu` on a long press (iOS never does) and
 *   cancels the touch unless it is prevented, which would both open the menu
 *   and kill the armed drag. A `contextmenu` listener on the chip swallows it
 *   for exactly as long as the touch is down. Right-click never installs it.
 *
 * Returns `dragging` so the caller can show that the hold registered — with a
 * long press the reader gets no feedback until they move, and without a cue a
 * successful arm is indistinguishable from a failed one.
 */
export function useLongPressReorder(
  { onHoldRelease }: LongPressReorderOptions = {},
): { itemProps: LongPressReorderItemProps; dragging: boolean } {
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
      // A precise pointer starts the drag on press — but only the primary
      // button. Arming a reorder on button 1 or 2 is meaningless: there is no
      // middle- or right-drag gesture, so a non-primary press has nothing to
      // reorder and must fall through untouched. This was also a suspect for
      // the side-panel middle-click-to-close report — the theory being that
      // starting the drag here swallowed the chip's auxclick — but a real-
      // browser test ruled that out: with this guard removed, a middle-click
      // (clean and past the pan threshold) still closed the tab. The guard is
      // a correctness fix on its own, not a fix for that symptom.
      if (e.button !== 0) return
      setDragging(true)
      dragControls.start(e)
      return
    }
    if (onHoldRelease) e.preventDefault() // the trigger's own long press stands down
    const target = e.currentTarget as HTMLElement
    const originX = e.clientX
    const originY = e.clientY
    const travelled = (ev: PointerEvent) =>
      Math.abs(ev.clientX - originX) > LONG_PRESS_SLOP_PX || Math.abs(ev.clientY - originY) > LONG_PRESS_SLOP_PX
    // The native event outlives this handler (React stopped pooling in 17), and
    // framer reads only the press point from it — which is precisely the point
    // the hold happened at.
    const origin = e.nativeEvent
    // Android's long-press `contextmenu` would open the menu and cancel the
    // touch; swallowed while this touch is down, released with it.
    const swallowContextMenu = (ev: Event) => { ev.preventDefault(); ev.stopPropagation() }
    if (onHoldRelease) target.addEventListener('contextmenu', swallowContextMenu)
    const onMove = (ev: PointerEvent) => { if (travelled(ev)) clearPending() }
    window.addEventListener('pointermove', onMove, { passive: true })
    window.addEventListener('pointerup', clearPending)
    window.addEventListener('pointercancel', clearPending)
    cleanupRef.current = () => {
      target.removeEventListener('contextmenu', swallowContextMenu)
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', clearPending)
      window.removeEventListener('pointercancel', clearPending)
    }
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      clearPending()
      setDragging(true)
      dragControls.start(origin)
      if (!onHoldRelease) return
      // Armed. Now the finger decides: travel is a reorder (framer has the
      // pointer), a lift in place is the hold-release action.
      let moved = false
      const onArmedMove = (ev: PointerEvent) => { if (travelled(ev)) moved = true }
      const onArmedUp = (ev: PointerEvent) => {
        clearPending() // drops the swallow first, so the opener's own event goes through
        if (!moved) onHoldRelease(ev, target)
      }
      target.addEventListener('contextmenu', swallowContextMenu)
      window.addEventListener('pointermove', onArmedMove, { passive: true })
      window.addEventListener('pointerup', onArmedUp)
      window.addEventListener('pointercancel', clearPending)
      cleanupRef.current = () => {
        target.removeEventListener('contextmenu', swallowContextMenu)
        window.removeEventListener('pointermove', onArmedMove)
        window.removeEventListener('pointerup', onArmedUp)
        window.removeEventListener('pointercancel', clearPending)
      }
    }, LONG_PRESS_MS)
  }, [clearPending, dragControls, onHoldRelease])

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
