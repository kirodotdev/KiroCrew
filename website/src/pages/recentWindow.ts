/**
 * Pure helpers for the ChatSidebar "Recent" filter.
 *
 * The recency window is stored as a single millisecond count; these functions
 * translate between that count, the amount/unit the custom picker shows, the
 * compact label the row/chip render, and the boundary predicate that decides
 * whether a session counts as recent. They live here — separate from the
 * component — so the core math is unit-testable without a full render, mirroring
 * the sibling `unreadDrain` extraction.
 */

export type RecentUnit = 'minutes' | 'hours' | 'days'

export const DEFAULT_RECENT_WINDOW_MS = 60 * 60 * 1000
export const RECENT_UNIT_MS: Record<RecentUnit, number> = {
  minutes: 60 * 1000,
  hours: 60 * 60 * 1000,
  days: 24 * 60 * 60 * 1000,
}
export const RECENT_WINDOW_PRESETS: { label: string; ms: number }[] = [
  { label: '1 hour', ms: 60 * 60 * 1000 },
  { label: '6 hours', ms: 6 * 60 * 60 * 1000 },
  { label: '1 day', ms: 24 * 60 * 60 * 1000 },
  { label: '1 week', ms: 7 * 24 * 60 * 60 * 1000 },
]
// Upper bound on how often the Recent filter re-evaluates recency while active.
export const RECENT_TICK_MS = 10 * 60 * 1000
// The custom picker accepts 1..9999 of the chosen unit.
export const RECENT_AMOUNT_MIN = 1
export const RECENT_AMOUNT_MAX = 9999

/** Split a window in ms into the largest whole unit that divides it evenly. */
export function decomposeRecentWindow(ms: number): { value: number; unit: RecentUnit } {
  if (ms % RECENT_UNIT_MS.days === 0) return { value: ms / RECENT_UNIT_MS.days, unit: 'days' }
  if (ms % RECENT_UNIT_MS.hours === 0) return { value: ms / RECENT_UNIT_MS.hours, unit: 'hours' }
  return { value: Math.max(1, Math.round(ms / RECENT_UNIT_MS.minutes)), unit: 'minutes' }
}

/** Compact label for a window, e.g. "1h", "30m", "2d". */
export function formatRecentWindow(ms: number): string {
  const { value, unit } = decomposeRecentWindow(ms)
  return `${value}${unit === 'days' ? 'd' : unit === 'hours' ? 'h' : 'm'}`
}

/** Clamp raw custom-amount input to a whole 1..9999; empty/invalid → the min. */
export function clampRecentAmount(rawValue: string | number): number {
  return Math.max(RECENT_AMOUNT_MIN, Math.min(RECENT_AMOUNT_MAX, Math.floor(Number(rawValue) || 0)))
}

/** Compute the window in ms for a custom amount + unit, clamping the amount. */
export function customRecentWindowMs(rawValue: string | number, unit: RecentUnit): number {
  return clampRecentAmount(rawValue) * RECENT_UNIT_MS[unit]
}

/**
 * How often the heartbeat should re-evaluate recency for a given window: ~1/10th
 * the window, but never faster than every 30s nor slower than RECENT_TICK_MS, so
 * a short custom window stays fresh without waking an idle tab every few seconds.
 */
export function recentTickIntervalMs(windowMs: number): number {
  return Math.min(RECENT_TICK_MS, Math.max(30_000, Math.round(windowMs / 10)))
}

/**
 * Whether a session's last-activity timestamp falls within the window relative
 * to `now`. A missing timestamp (or one that parses to NaN, e.g. '') is never
 * recent — the NaN comparison is `false`, but we short-circuit for clarity.
 */
export function isWithinRecentWindow(
  lastActivity: string | undefined,
  now: number,
  windowMs: number,
): boolean {
  if (!lastActivity) return false
  const ts = new Date(lastActivity).getTime()
  if (Number.isNaN(ts)) return false
  return now - ts <= windowMs
}
