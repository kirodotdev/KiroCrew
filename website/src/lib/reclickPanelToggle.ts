/** A plain click on the session list row of the chat already open shows or
 *  hides the side panel. It goes through the same window event the `side-panel`
 *  shortcut (⌘\ / Ctrl+\) dispatches (shell/shortcuts/shellKeyboard.ts), so the
 *  per-chat open/closed memory is the shortcut's own.
 *
 *  Lives in lib, not in the sidebar's owners, because the chat page cancels it
 *  too, on a switch made any other way. In Split View the row never arms it
 *  (`SessionRowView.reclickPanel`). */

/** How long the click waits before it toggles: the chip debounce in FollowUpBar,
 *  so a double-click on the title opens rename without also flipping the panel. */
export const RECLICK_PANEL_DELAY_MS = 220
const TOGGLE_EVENT = 'toggle-activity-panel'
// One timer for the page, not one per row: only one row is the open chat, and a
// click on any other row must cancel a toggle still waiting.
let reclickTimer: ReturnType<typeof setTimeout> | null = null
// Whether the current click sequence's toggle already fired. Scoped to the
// sequence, not to a clock: every new first click and every other click clears
// it, so a double-click slower than the delay (the platform sets that interval)
// still finds the toggle its own first click made, and nothing older.
let reclickFired = false

/** Drop a toggle still waiting. */
export function cancelReclickPanelToggle(): void {
  if (reclickTimer !== null) { clearTimeout(reclickTimer); reclickTimer = null }
}

/** `eligible` is the row's "I am the open chat, on a desktop layout, and not
 *  being renamed". Returns true when the click is consumed. A click with a
 *  modifier, or on any other row, cancels a waiting toggle and returns false. */
export function reclickTogglesPanel(e: React.MouseEvent, eligible: boolean): boolean {
  cancelReclickPanelToggle()
  if (!eligible || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { reclickFired = false; return false }
  // The second click of a double-click: the first one's toggle is cancelled above.
  if (e.detail > 1) return true
  reclickFired = false
  reclickTimer = setTimeout(() => {
    reclickTimer = null
    reclickFired = true
    window.dispatchEvent(new Event(TOGGLE_EVENT))
  }, RECLICK_PANEL_DELAY_MS)
  return true
}

/** A double-click on the open chat's title is a rename, not a panel gesture:
 *  cancel the first click's toggle, or undo it when a slow double-click let it
 *  fire. The row calls it for a double-click on the title only, so a row control
 *  that stops its clicks but not its dblclick (Duplicate, Close) reaches none. */
export function reclickDoubleClick(): void {
  cancelReclickPanelToggle()
  if (reclickFired) window.dispatchEvent(new Event(TOGGLE_EVENT))
  reclickFired = false
}
