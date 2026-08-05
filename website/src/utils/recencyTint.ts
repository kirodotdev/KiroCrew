// Pure helpers for the sidebar "recency tint" — the graded accent stripe marking the
// most-recently-active sessions. Kept out of the component so they're unit-testable and
// so the tinted-session count stays a single configurable value.

// Default number of most-recently-active sessions to tint in the sidebar. Overridable via
// the server-side `dashboard.recent_tint_count` config (Display settings); 0 disables it.
export const RECENT_TINT_COUNT = 0

// Upper bound for the configurable tint count. The graded stripe hard-caps width/opacity at
// 7px/100%, so ranks beyond the 5 most-recent plateau at full intensity rather than fading —
// the extra slots still mark a session as recent. 50 comfortably covers a heavy multi-session
// sidebar without being unbounded.
export const MAX_RECENT_TINT_COUNT = 50

// Coerce a raw config value (possibly missing / non-numeric / out-of-range) into a valid
// tint count: rounds, clamps to [0, MAX_RECENT_TINT_COUNT], and falls back to the default.
export function clampTintCount(n: unknown): number {
  if (n == null) return RECENT_TINT_COUNT
  const v = typeof n === 'number' ? n : Number(n)
  if (!Number.isFinite(v)) return RECENT_TINT_COUNT
  return Math.min(MAX_RECENT_TINT_COUNT, Math.max(0, Math.round(v)))
}

/**
 * Rank the up-to-`count` most-recently-active sessions by `last_ts` (descending),
 * returning a key→rank map where 1 = most recent. `last_ts` is an ISO-8601 UTC string
 * (the last message of any role); sessions with a missing/unparseable `last_ts` are
 * excluded so an empty session never occupies a tint slot.
 */
export function computeRecentRank(
  slots: { key: string; last_ts?: string }[],
  count: number,
): Map<string, number> {
  const ranked = new Map<string, number>()
  slots
    .map(s => [s.key, Date.parse(s.last_ts || '') || 0] as [string, number])
    .filter(([, t]) => t > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, count)
    .forEach(([key], i) => ranked.set(key, i + 1))
  return ranked
}

/**
 * Graded recency-tint stripe for a session row, returned as an inset `box-shadow` value.
 * The least-recent tinted rank sits at the floor (MIN_W width / MIN_OP accent) and each
 * step up in recency adds a fixed increment (W_STEP px / OP_STEP %) up to a hard cap
 * (MAX_W / MAX_OP). Fixed steps keep each rank's look constant across counts — if the
 * configured count exceeds 5, the most-recent tints plateau at the cap rather than growing
 * unbounded. `color-mix` keeps it theme-aware.
 */
export function recencyTintShadow(rank: number, total: number): string {
  const MIN_W = 3, MAX_W = 7, W_STEP = 1
  const MIN_OP = 40, MAX_OP = 100, OP_STEP = 15
  const steps = total - rank // 0 for the least-recent tinted rank; grows with recency
  const width = Math.min(MAX_W, MIN_W + W_STEP * steps)
  const op = Math.min(MAX_OP, MIN_OP + OP_STEP * steps)
  return `inset ${width}px 0 0 color-mix(in srgb, var(--accent) ${op}%, transparent)`
}
