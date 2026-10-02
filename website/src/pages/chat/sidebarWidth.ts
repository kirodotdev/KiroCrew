/**
 * Sidebar width bounds and the viewport clamp.
 *
 * Deliberately NOT in ChatSidebar: ~20 ChatPage suites replace that module with
 * `{ default, SIDEBAR_MIN, SIDEBAR_MAX }`, so anything else imported from it is
 * `undefined` at run time -- silent for a constant, a crash for a function.
 */

export const SIDEBAR_MIN = 180
/** The drag ceiling in the list views on every window, and in board view on a
 *  normal-width one. In board view a window wide enough to seat a wider
 *  sidebar beside the nav rail and a minimum chat pane raises it; see
 *  `sidebarMaxWidth`. */
export const SIDEBAR_MAX = 1400

/**
 * The widest the user may drag the board-view sidebar in THIS window: the
 * window minus the nav rail minus the chat pane's minimum, never below
 * `SIDEBAR_MAX`. On a normal-width window that difference is under 1400, so
 * the ceiling is exactly `SIDEBAR_MAX` as before; only a wide window lifts
 * it. A non-finite input (a test mock that omits a constant) falls back to
 * `SIDEBAR_MAX`.
 */
export function sidebarMaxWidth(
  { winW, railW, chatMin }: { winW: number; railW: number; chatMin: number },
): number {
  const room = winW - railW - chatMin
  return Number.isFinite(room) ? Math.max(SIDEBAR_MAX, room) : SIDEBAR_MAX
}

/**
 * A stored width read back from localStorage: `null` when it is not a usable
 * number or is under `SIDEBAR_MIN`, otherwise the value as saved. It is NOT
 * narrowed to this window: a width saved on a wider window stays in state, and
 * `sidebarPaintWidth` holds it to the current ceiling at render, so widening
 * the window again restores it.
 */
export function parseStoredSidebarWidth(raw: string | null): number | null {
  const n = raw ? parseInt(raw, 10) : NaN
  if (isNaN(n) || n < SIDEBAR_MIN) return null
  return n
}

/**
 * The width the sidebar root paints at for a stored preference. A width up to
 * `SIDEBAR_MAX` paints exactly as stored, as it always has. A width past
 * `SIDEBAR_MAX` exists only because board view on a wider window allowed it,
 * so it follows this window: it is held to the room beside the nav rail and
 * a `chatMin` chat pane (floored at `SIDEBAR_MIN`). The chat pane and the
 * resize handle on the root's right edge then stay on screen, including on a
 * window too narrow for `SIDEBAR_MAX` itself, where a wide width saved
 * elsewhere would otherwise fill the window and push the handle past its
 * edge.
 */
export function sidebarPaintWidth(
  { stored, winW, railW, chatMin }: { stored: number; winW: number; railW: number; chatMin: number },
): number {
  if (stored <= SIDEBAR_MAX) return stored
  const room = winW - railW - chatMin
  if (!Number.isFinite(room)) return SIDEBAR_MAX
  return Math.min(stored, Math.max(SIDEBAR_MIN, room))
}

/**
 * The stored width narrowed to the space the window actually leaves beside the
 * nav rail. Reserves NOTHING for the chat pane -- `panelReserve` owns that, and
 * subtracting a chat minimum here caps a legitimately wide board sidebar.
 */
export function clampSidebarWidth(
  { stored, winW, railW }: { stored: number; winW: number; railW: number },
): number {
  return Math.min(stored, Math.max(SIDEBAR_MIN, winW - railW))
}
