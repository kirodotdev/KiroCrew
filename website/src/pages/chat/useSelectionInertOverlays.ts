import { useEffect, type RefObject } from 'react'
import { useIsTouchDevice } from '../../hooks/useIsTouchDevice'

/** Marks an element that overlays the transcript (header, composer dock). */
export const SELECTION_INERT_ATTR = 'data-selection-inert'

/** Whether a non-empty text selection has an endpoint inside `scroller`.
 *
 * Either endpoint, not just the anchor: dragging the START handle onto an
 * overlay moves the anchor out, and releasing the overlays then let the
 * selection take the title with it.
 */
export function transcriptSelectionHeld(scroller: HTMLElement | null): boolean {
  const sel = typeof document !== 'undefined' ? document.getSelection() : null
  if (!scroller || !sel || sel.isCollapsed || sel.rangeCount === 0) return false
  return (!!sel.anchorNode && scroller.contains(sel.anchorNode)) || (!!sel.focusNode && scroller.contains(sel.focusNode))
}

/**
 * While a touch selection is held in the transcript, make the overlays `inert`.
 *
 * The transcript scrolls UNDER the header and the composer dock, so extending a
 * selection to the bottom edge puts the handle over the dock. Hit-testing there
 * resolved into the composer's draft mirror, and the selection jumped to it,
 * taking everything in between. `user-select: none` does not change where the
 * caret lands; `inert` removes the overlay from hit-testing, so the handle lands
 * on the transcript beneath and the scroller's own edge auto-scroll takes over.
 *
 * Touch only: on desktop a selection outlives the drag, and an inert composer
 * would cost an extra click before typing.
 *
 * A finger on the page releases them at once. Android's selection handles are
 * browser chrome and dispatch no touch events to the page, so a `touchstart`
 * is the reader reaching for something, not a handle drag: without the
 * release, the first tap on the composer or the header after selecting text
 * fell through to the transcript and only cleared the selection. The next
 * handle move re-applies `inert` through `selectionchange`.
 */
export function useSelectionInertOverlays(scrollerRef: RefObject<HTMLElement | null>): void {
  const isTouch = useIsTouchDevice()
  useEffect(() => {
    if (!isTouch) return
    const overlays = () => document.querySelectorAll<HTMLElement>(`[${SELECTION_INERT_ATTR}]`)
    const sync = () => {
      const held = transcriptSelectionHeld(scrollerRef.current)
      for (const el of overlays()) if (el.inert !== held) el.inert = held
    }
    const release = () => {
      for (const el of overlays()) if (el.inert) el.inert = false
    }
    document.addEventListener('selectionchange', sync)
    document.addEventListener('touchstart', release, { capture: true, passive: true })
    return () => {
      document.removeEventListener('selectionchange', sync)
      document.removeEventListener('touchstart', release, { capture: true })
      release()
    }
  }, [isTouch, scrollerRef])
}
