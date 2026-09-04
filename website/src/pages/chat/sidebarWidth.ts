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
 * The stored width narrowed to the space the window actually leaves beside the
 * nav rail. Reserves NOTHING for the chat pane -- `panelReserve` owns that, and
 * subtracting a chat minimum here caps a legitimately wide board sidebar.
 */
export function clampSidebarWidth(
  { stored, winW, railW }: { stored: number; winW: number; railW: number },
): number {
  return Math.min(stored, Math.max(SIDEBAR_MIN, winW - railW))
}
