import { useCallback, useEffect, useState, type RefObject } from 'react'
import { useAnchorRemeasure } from './useAnchorRemeasure'

/**
 * Re-render a portaled picker when its anchor moves while it is open.
 *
 * The composer pickers position themselves from the anchor's rect during
 * render (see `menuGeometry`). An in-page layout change, such as the docked
 * terminal unmounting after its slide-out, moves the composer by resizing one
 * of its ancestors, with no window resize or scroll. A ResizeObserver on the
 * anchor and its ancestors sees that; useAnchorRemeasure covers the window,
 * scroll and visual-viewport moves.
 */
export function useAnchorTracking(open: boolean, anchorRef: RefObject<HTMLElement | null>): void {
  const [, setTick] = useState(0)
  const bump = useCallback(() => setTick(t => t + 1), [])
  useAnchorRemeasure(open, bump)
  useEffect(() => {
    const anchor = anchorRef.current
    if (!open || !anchor || typeof ResizeObserver === 'undefined') return
    let first = true
    const ro = new ResizeObserver(() => {
      // The first callback reports the initial sizes; nothing has moved yet.
      if (first) { first = false; return }
      bump()
    })
    for (let el: HTMLElement | null = anchor; el && el !== document.body; el = el.parentElement) ro.observe(el)
    return () => ro.disconnect()
  }, [open, anchorRef, bump])
}
