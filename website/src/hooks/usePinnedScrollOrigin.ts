import { useEffect, type RefObject } from 'react'

/**
 * Keep a fixed-layout `overflow: hidden` box at scroll offset 0,0.
 *
 * `overflow: hidden` hides scrollbars but still leaves the box scrollable from
 * script and from the engine's own reveal logic: bringing a caret or a focused
 * element into view walks every ancestor scroller, hidden ones included. The
 * chat layout boxes (the content column, the chat page frame, a chat pane) never
 * scroll on purpose, so a non-zero offset there is always such a reveal. When it
 * happens the whole column shifts up, the composer lands at the top with the
 * transcript out of sight, and in the desktop app the shifted controls' boxes sit
 * under the 42px drag band and cancel it (see `computeTitleBandDragGaps`), so the
 * window only moves from its very top edge.
 *
 * `overflow: clip` would make the box unscrollable outright, but it also stops
 * the box being a scroll container, which changes what `position: sticky`
 * descendants stick to; resetting on `scroll` leaves the layout as it is.
 */
export function usePinnedScrollOrigin(
  target: RefObject<HTMLElement | null> | HTMLElement | null,
  enabled = true,
): void {
  useEffect(() => {
    const el = target && 'current' in target ? target.current : target
    if (!enabled || !el) return
    const reset = () => {
      if (el.scrollTop !== 0) el.scrollTop = 0
      if (el.scrollLeft !== 0) el.scrollLeft = 0
    }
    reset()
    el.addEventListener('scroll', reset, { passive: true })
    return () => el.removeEventListener('scroll', reset)
  }, [target, enabled])
}
