/**
 * Sidebar width bounds and the viewport clamp.
 *
 * Deliberately NOT in ChatSidebar: ~20 ChatPage suites replace that module with
 * `{ default, SIDEBAR_MIN, SIDEBAR_MAX }`, so anything else imported from it is
 * `undefined` at run time -- silent for a constant, a crash for a function.
 */

export const SIDEBAR_MIN = 180
export const SIDEBAR_MAX = 1400

/**
 * The stored width narrowed to the room the window actually leaves beside the
 * nav rail and `chatReserve`.
 *
 * A stored width is validated only against SIDEBAR_MIN..SIDEBAR_MAX, never the
 * window, so a width saved on a wide window is carried verbatim onto a narrow
 * one. Painted as stored in list/chat view, a wide width overhangs: the chat
 * pane drops below its minimum and the resize handle on the sidebar's right edge
 * lands at or past the window edge, where it cannot be grabbed to narrow the
 * sidebar again.
 *
 * `chatReserve` is the width to keep clear beside the rail for whatever sits to
 * the sidebar's right. It is a REQUIRED operand, not an option with a default,
 * because the two callers want genuinely different values and neither is a
 * "normal" case the other decorates:
 *   - list / chat view passes `CHAT_PANE_MIN_W`, so the chat pane stays usable
 *     and the handle stays inside the window. The hold is applied to EVERY
 *     stored width, so a saved 1400 is held exactly as a saved 2884 is --
 *     adjacent preferences can no longer paint differently (#16094).
 *   - board view passes 0: the board is a horizontal column strip that is meant
 *     to occupy the width, and `boardSidebarWidth` has ALREADY reserved the
 *     chat pane (BOARD_CHAT_RESERVE) when it chose the width. Reserving again
 *     here would double-count it and cap a legitimately wide board sidebar --
 *     the regression commit 9406a36e7 reverted, which the board-column e2e specs
 *     (stored 1400 @ viewport 1800) pin.
 *
 * The result is floored at SIDEBAR_MIN: on a window too narrow to seat both the
 * reserve and the floor, the sidebar keeps its minimum and whatever is beside it
 * gives up the rest, rather than the sidebar collapsing below where it is usable.
 */
export function clampSidebarWidth(
  { stored, winW, railW, chatReserve }:
  { stored: number; winW: number; railW: number; chatReserve: number },
): number {
  return Math.min(stored, Math.max(SIDEBAR_MIN, winW - railW - chatReserve))
}
