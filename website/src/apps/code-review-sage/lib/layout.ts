// Column geometry + persistence keys for the Code Review Sage shell.
//
// Split out from format.ts so the layout constants sit next to each other and
// the resize hook's storage keys are namespaced per app (a shared key would let
// Issue Radar's column width leak into Sage's).

export const RAIL_WIDTH_KEY = 'kc:code-review-sage:rail-width'
export const RAIL_COLLAPSED_KEY = 'kc:code-review-sage:rail-collapsed'
export const LIST_WIDTH_KEY = 'kc:code-review-sage:list-width'

/** Wider than Issue Radar's rail (`w-72`), because this one carries the pull
 * request list that used to have a column of its own — PR titles need the room.
 * Still resizable and persisted, so a narrower preference sticks. */
export const DEFAULT_RAIL_WIDTH = 360
export const MIN_RAIL_WIDTH = 280
export const MAX_RAIL_WIDTH = 560
/** The narrow strip keeps navigation reachable while the detail pane owns the viewport. */
export const COLLAPSED_RAIL_WIDTH = 48

export const DEFAULT_LIST_WIDTH = 330
export const MIN_LIST_WIDTH = 260
export const MAX_LIST_WIDTH = 620

function loadWidth(key: string, min: number, max: number, fallback: number): number {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const n = Number(raw)
    if (!Number.isFinite(n)) return fallback
    return Math.min(max, Math.max(min, n))
  } catch {
    // Storage blocked (private mode, quota) — the default still renders.
    return fallback
  }
}

export function loadRailWidth(): number {
  return loadWidth(RAIL_WIDTH_KEY, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, DEFAULT_RAIL_WIDTH)
}

export function loadRailCollapsed(): boolean {
  try {
    return localStorage.getItem(RAIL_COLLAPSED_KEY) === '1'
  } catch {
    return false
  }
}

export function loadListWidth(): number {
  return loadWidth(LIST_WIDTH_KEY, MIN_LIST_WIDTH, MAX_LIST_WIDTH, DEFAULT_LIST_WIDTH)
}

/** Poll cadence while at least one run is live. Reviews advance on the order of
 * seconds per phase, so this is frequent enough to feel live without being
 * chatty; everything falls back to {@link IDLE_POLL_MS} once nothing is running. */
export const LIVE_POLL_MS = 3_000
/** Idle cadence: a run can still be started from another tab or a cron. */
export const IDLE_POLL_MS = 30_000
