import { useCallback, useLayoutEffect, useRef } from 'react'
import { revealChip } from '../pages/chat/sidePanelReveal'

export const TAB_ID_ATTR = 'data-tab-id'

/**
 * Keeps a strip's active tab inside its scroller. `reveal(id)` serves picking
 * the tab that is already active, which does not change the id. Tabs are found
 * by attribute because the wrappers some strips put around a tab do not forward refs.
 */
export function useRevealActiveTab<T extends HTMLElement>(activeId: string | null) {
  const scrollerRef = useRef<T | null>(null)
  const activeRef = useRef(activeId)
  activeRef.current = activeId
  // Where the last reveal left the row, to tell it from the user's own scrolling.
  const revealedLeft = useRef(0)

  const reveal = useCallback((id: string) => {
    const scroller = scrollerRef.current
    if (!scroller) return
    // An id can hold any character, so match in JS instead of building a selector.
    for (const tab of scroller.querySelectorAll<HTMLElement>(`[${TAB_ID_ATTR}]`)) {
      if (tab.getAttribute(TAB_ID_ATTR) !== id) continue
      revealChip(scroller, tab, true)
      revealedLeft.current = scroller.scrollLeft
      return
    }
  }, [])

  // The scroller can mount after the id is set (a strip waiting on hydration), and
  // web fonts can widen tabs after the first reveal, so recheck once they settle
  // unless the user has scrolled since.
  const attach = useCallback((node: T | null) => {
    scrollerRef.current = node
    const id = activeRef.current
    if (!node || id == null) return
    reveal(id)
    if (document.fonts?.status === 'loading') {
      void document.fonts.ready.then(() => {
        const current = activeRef.current
        if (scrollerRef.current === node && current != null && node.scrollLeft === revealedLeft.current) reveal(current)
      })
    }
  }, [reveal])

  // Keyed on the id alone, so a list-only change keeps manual scrolling.
  useLayoutEffect(() => {
    if (activeId != null) reveal(activeId)
  }, [activeId, reveal])

  return [attach, reveal] as const
}
