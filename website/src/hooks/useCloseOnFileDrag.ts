import { useEffect, useRef } from 'react'

import { carriesFiles } from '../lib/fileDrag'

/**
 * Close a modal Radix menu the moment a file drag from outside the page
 * enters the window.
 *
 * Why: a modal Radix menu (DropdownMenu, ContextMenu) sets
 * `pointer-events: none` on `document.body` while open, and HTML5 drag
 * events honour that property. Nothing under the menu -- the chat composer's
 * drop zone included -- can receive `dragover`/`drop`, so a file dragged in
 * from the OS while a menu is open is silently refused. The menu never
 * dismisses on its own either: Radix closes on an outside `pointerdown`, and
 * a native file drag produces none. The `dragenter` still reaches `window`
 * (it fires on the `<html>` element, which `body`'s pointer-events cannot
 * hide; Radix's own click-outside dismissal relies on the same fact), so
 * listen there and close the menu; the drop zone then picks up the following
 * `dragover`.
 *
 * @param active  true while the menu is open AND modal. A non-modal menu
 *                never blocks the drop zone, so pass false for it.
 * @param close   the menu's own close path (its `onOpenChange(false)`), so
 *                controlled callers see the change like any click-outside.
 */
export function useCloseOnFileDrag(active: boolean, close: () => void): void {
  // Callers often pass an inline `onOpenChange`, which gives `close` a new
  // identity per render; read it through a ref so the listener is armed once
  // per open, not once per render. The ref is written in an effect, not
  // during render, so a discarded concurrent render cannot leave it stale.
  const closeRef = useRef(close)
  useEffect(() => {
    closeRef.current = close
  }, [close])
  useEffect(() => {
    if (!active) return
    const onFileDragEnter = (event: DragEvent) => {
      if (carriesFiles(event.dataTransfer)) closeRef.current()
    }
    // Capture phase: fire before any handler that might stop propagation.
    window.addEventListener('dragenter', onFileDragEnter, true)
    return () => window.removeEventListener('dragenter', onFileDragEnter, true)
  }, [active])
}
