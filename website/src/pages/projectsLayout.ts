// Task Runner workspace geometry — the run rail's persisted width and collapsed
// state. Pure and React-free (mirrors apps/issue-radar/lib/format.ts), so it can
// be imported from tests and from the page without pulling in the page itself.
import { loadColumnCollapsed, loadColumnWidth } from '../lib/columnWidth'

export const RAIL_WIDTH_KEY = 'kc:task-runner:rail-width'
export const RAIL_COLLAPSED_KEY = 'kc:task-runner:rail-collapsed'

/** Matches the rail's former fixed `w-[260px]`, so an existing user sees no jump
 * the first time they load the resizable version. */
export const DEFAULT_RAIL_WIDTH = 260
export const MIN_RAIL_WIDTH = 220
export const MAX_RAIL_WIDTH = 460
/** Width of the collapsed rail: a vertical strip showing only the app mark and
 * the name turned on its side. Dragging the rail well past its minimum snaps to
 * this instead of stopping at a stubborn wall. */
export const COLLAPSED_RAIL_WIDTH = 48

export function loadRailWidth(): number {
  return loadColumnWidth(RAIL_WIDTH_KEY, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, DEFAULT_RAIL_WIDTH)
}

export function loadRailCollapsed(): boolean {
  return loadColumnCollapsed(RAIL_COLLAPSED_KEY)
}
