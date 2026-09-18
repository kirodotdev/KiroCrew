/** Which slot a non-chat surface is showing on screen right now.
 *
 * The websocket unread-marker asks one question of every arriving message:
 * "is this slot the one the user is looking at?" -- and answers it from
 * `chat.activeSlot`. That field is owned by the Sessions page's `switchSlot`
 * and is not moved by any other surface. The Crew Members page mounts a
 * member's thread without touching it, so every message in the OPEN thread
 * was flagged unread, and the page's own read effect cleared the flag a
 * render later. Both writes relay to the parent dashboard's crew tab
 * (`mc-unread-slots`), so the tab's badge lit and vanished on every message:
 * a flicker the user reads as a notification that keeps disappearing.
 *
 * This module is the second answer to that question, for ONE thread: the
 * member thread the Crew Members page has open. It holds a single slot and
 * has a single registrant; the marker consults both sources and never flags
 * a slot the user can already see. A multi-pane surface (the split-pane
 * session grid) is deliberately not covered -- it has no drain effect, so it
 * has no flicker, and whether its visible-but-unfocused panes should badge is
 * a product call for that surface, which would need a set-shaped registry.
 * A module-level value rather than Redux state for the same reason
 * `slotReadRelay` is one: the reader is the websocket hook's message handler,
 * which reads the store synchronously, and the writer is a page effect -- no
 * render depends on it.
 *
 * Only VISIBLE views register. A hidden or unfocused window is not reading,
 * so its open thread keeps badging (and the page's read effect clears the
 * badge on reveal, as before).
 */

let viewedThreadSlot: string | null = null

/** Register *slot* as the thread on screen. Replaces any earlier registration. */
export function setViewedThreadSlot(slot: string): void {
  viewedThreadSlot = slot
}

/** Retire *slot*'s registration. A no-op when another slot has since
 *  registered, so a late effect cleanup cannot un-register its successor. */
export function clearViewedThreadSlot(slot: string): void {
  if (viewedThreadSlot === slot) viewedThreadSlot = null
}

/** The registered slot, or `null` when no non-chat surface is showing one. */
export function getViewedThreadSlot(): string | null {
  return viewedThreadSlot
}

/** Test-only reset. */
export function _resetViewedThreadForTests(): void {
  viewedThreadSlot = null
}
