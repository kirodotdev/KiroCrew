/**
 * Pure helpers for the ChatSidebar stale-session collapse.
 *
 * Sessions whose last settled activity is older than a user-selectable
 * threshold collapse behind a per-container "Dormant sessions (N)" expander row,
 * independently at every tree level (each folder body plus the ungrouped
 * root). The threshold is stored as a single millisecond count (0 = feature
 * off); these functions own the preset table and the split predicate so the
 * core math is unit-testable without a render, mirroring the sibling
 * `recentWindow` extraction.
 */

const DAY_MS = 24 * 60 * 60 * 1000

/** Default threshold: sessions idle for more than a week collapse. Deliberately
 *  generous — the collapse exists to de-noise settled work, and a shorter
 *  default hid sessions people still considered current, making the list feel
 *  incomplete rather than tidy. */
export const DEFAULT_STALE_COLLAPSE_MS = 7 * DAY_MS

/** Menu presets. 0 disables the feature entirely (no expander rows render). */
export const STALE_COLLAPSE_PRESETS_MS: readonly number[] = [0, DAY_MS, 2 * DAY_MS, 7 * DAY_MS, 14 * DAY_MS]

/**
 * How often the sidebar re-evaluates staleness while the feature is on.
 * Staleness moves on a scale of days, so a slow heartbeat is enough — this
 * only matters for a tab left open across a threshold boundary.
 */
export const STALE_COLLAPSE_TICK_MS = 10 * 60 * 1000

export interface StaleSplit<T> {
  fresh: T[]
  stale: T[]
}

/**
 * Partition a container's session list into rows that stay visible (`fresh`)
 * and rows that collapse behind the expander (`stale`), preserving the input
 * order within both halves.
 *
 * A row is stale when it is not exempt AND its last activity is known AND that
 * activity is older than the threshold. A missing/unparseable timestamp
 * (`lastActivityMsOf` returning 0) keeps the row visible: never hide a row we
 * cannot date. `thresholdMs <= 0` means the feature is off — everything is
 * fresh.
 */
export function splitStaleSlots<T>(
  list: readonly T[],
  thresholdMs: number,
  now: number,
  lastActivityMsOf: (item: T) => number,
  isExempt: (item: T) => boolean,
): StaleSplit<T> {
  if (thresholdMs <= 0) return { fresh: [...list], stale: [] }
  const fresh: T[] = []
  const stale: T[] = []
  for (const item of list) {
    const ts = lastActivityMsOf(item)
    if (ts > 0 && now - ts > thresholdMs && !isExempt(item)) stale.push(item)
    else fresh.push(item)
  }
  return { fresh, stale }
}
