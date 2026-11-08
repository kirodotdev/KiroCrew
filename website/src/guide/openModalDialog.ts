import { GUIDE_LAYER_SELECTOR } from './probeRegistry'

/**
 * A page dialog that is modal right now (never the guide's own layer).
 *
 * The ONE spelling of "a modal holds the window", shared by the guide (which
 * steps aside for it) and by `paneVisibility` (a chat transcript behind it is
 * not on screen). Kept apart from `GuideLayer` so a hook can ask it without
 * importing the guide's component module.
 */
export function openModalDialog(root: Document = document): Element | null {
  // The topmost one: a confirm opened from inside another dialog portals
  // after it in document order (into the same root), so it comes later and
  // covers it.
  const open = Array.from(root.querySelectorAll('[role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]'))
  for (let i = open.length - 1; i >= 0; i--) {
    if (!open[i].closest(GUIDE_LAYER_SELECTOR)) return open[i]
  }
  return null
}
