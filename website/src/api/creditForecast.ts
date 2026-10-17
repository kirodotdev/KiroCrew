/**
 * Projects when the Kiro credit plan runs out before its reset (issue #17112).
 *
 * Two inputs, each used for what it can actually say:
 *
 * - The account reading (`used` / `limit` / `resets`) gives the PACE: credits
 *   used this cycle divided by the days the cycle has run. It is the account's
 *   whole spend, every client included.
 * - This gateway's per-day credits (the `daily_history` rows of `/api/usage/kiro`,
 *   read through `providerUsageQuery`) give the SHAPE
 *   of a week: how much heavier a Tuesday is than a Sunday. It is NOT used for
 *   the pace, because it only sees turns this gateway ran -- an IDE signed in
 *   to the same account is invisible to it, so projecting from its numbers
 *   would push the run-out date later than the account's real one.
 *
 * Pure: every input, including "now", is a parameter, so the tests pin it.
 */

/** Days of local history needed before the weekday shape is trusted. */
export const MIN_SHAPE_DAYS = 14
/** How far back the shape looks (matches the gateway's window). */
export const SHAPE_WINDOW_DAYS = 28
const DAY_MS = 86_400_000

export type CreditForecast =
  | { kind: 'lasts' }
  | { kind: 'runs-out'; date: Date }

export interface CreditForecastInput {
  /** Plan credits used this cycle. */
  used: number
  /** Plan credit limit. */
  limit: number
  /** Reset date, `YYYY-MM-DD` (the shape both usage sources emit). */
  resets: string | undefined
  /** Bonus grants still holding credits are drawn down BEFORE the plan. */
  bonusRemaining: number
  /** `{YYYY-MM-DD: credits}` per local day from this gateway's usage rows. */
  dailyCredits: Record<string, number> | undefined
  now: Date
}

function localDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

function parseDay(value: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!m) return null
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  return Number.isNaN(d.getTime()) ? null : d
}

function dayKey(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)
}

/** The cycle start: one calendar month before the reset (plans reset monthly). */
function cycleStart(reset: Date): Date {
  const start = new Date(reset.getFullYear(), reset.getMonth() - 1, reset.getDate())
  // A 31st reset whose previous month is shorter rolls into the next month;
  // clamp it to that month's last day instead.
  if (start.getMonth() === reset.getMonth()) {
    return new Date(reset.getFullYear(), reset.getMonth(), 0)
  }
  return start
}

/**
 * Relative weight per weekday (index = `Date.getDay()`), mean 1. Flat (all 1)
 * until the history covers {@link MIN_SHAPE_DAYS} complete days, or when it
 * holds no spend at all -- the simple-average fallback the issue asks for.
 *
 * The window starts at the earliest day with a row, not 28 days back: days
 * before this gateway ever ran a turn are unknown, not zero, and counting them
 * as zero would flatten the shape for a new install.
 */
export function weekdayWeights(
  dailyCredits: Record<string, number> | undefined,
  today: Date,
): number[] {
  const flat = [1, 1, 1, 1, 1, 1, 1]
  if (!dailyCredits) return flat
  const end = addDays(localDay(today), -1) // today is still partial
  let start = addDays(end, -(SHAPE_WINDOW_DAYS - 1))
  let earliest: Date | null = null
  for (const key of Object.keys(dailyCredits)) {
    const d = parseDay(key)
    if (d && (!earliest || d < earliest)) earliest = d
  }
  if (!earliest) return flat
  if (earliest > start) start = earliest
  const sums = [0, 0, 0, 0, 0, 0, 0]
  const counts = [0, 0, 0, 0, 0, 0, 0]
  let total = 0
  let days = 0
  for (let d = start; d <= end; d = addDays(d, 1)) {
    const raw = dailyCredits[dayKey(d)]
    const v = typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? raw : 0
    sums[d.getDay()] += v
    counts[d.getDay()] += 1
    total += v
    days += 1
  }
  if (days < MIN_SHAPE_DAYS || total <= 0) return flat
  const mean = total / days
  return sums.map((s, i) => (counts[i] > 0 ? s / counts[i] / mean : 1))
}

/**
 * The run-out projection, or `null` when there is nothing honest to say: no
 * reset date, a plan already used up (the modal's overage rows cover that),
 * bonus credits still being spent (the plan counter barely moves while they
 * drain, so its pace reads near zero), or less than a day of cycle behind us.
 */
export function forecastCreditRunOut(input: CreditForecastInput): CreditForecast | null {
  const { used, limit, resets, bonusRemaining, dailyCredits, now } = input
  if (!resets || !(limit > 0) || !Number.isFinite(used) || used < 0) return null
  if (bonusRemaining > 0) return null
  const remaining = limit - used
  if (remaining <= 0) return null
  const reset = parseDay(resets)
  if (!reset) return null
  const today = localDay(now)
  if (reset <= today) return null
  const elapsedDays = (now.getTime() - cycleStart(reset).getTime()) / DAY_MS
  if (elapsedDays < 1) return null
  const pace = used / elapsedDays
  if (!(pace > 0)) return { kind: 'lasts' }

  const weights = weekdayWeights(dailyCredits, now)
  // The share of today still ahead, so a morning projection counts the rest
  // of today and an evening one barely does.
  const dayFraction = 1 - (now.getTime() - today.getTime()) / DAY_MS
  let spent = 0
  for (let d = today; d < reset; d = addDays(d, 1)) {
    const share = d.getTime() === today.getTime() ? dayFraction : 1
    spent += pace * weights[d.getDay()] * share
    if (spent >= remaining) return { kind: 'runs-out', date: d }
  }
  return { kind: 'lasts' }
}
