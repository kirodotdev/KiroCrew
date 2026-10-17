/**
 * The Telemetry panel's reporting window: the presets, the custom date range, and
 * the query string both turn into.
 *
 * Pure functions only, so the arithmetic is tested without rendering the panel.
 * The backend (`resolve_window` in `dashboard/handlers/usage.py`) clamps whatever
 * arrives as a backstop; the custom range is also kept inside the ceiling here,
 * so the days a reader picks are the days that get measured.
 */

/** Rolling windows ending now. `90d` is the backend's `MAX_WINDOW_DAYS`. */
export const RANGE_PRESETS = ['24h', '7d', '14d', '30d', '90d'] as const
export type RangePreset = (typeof RANGE_PRESETS)[number]
/**
 * `default` asks for no window at all, so each card keeps the window it has on
 * main (spend 7 days, the OTEL and context cards 14). It is where the control
 * sits until the reader picks; the first pick then drives every card.
 */
export type RangeChoice = 'default' | RangePreset | 'custom'
export const RANGE_CHOICES: readonly RangeChoice[] = ['default', ...RANGE_PRESETS, 'custom']
export const DEFAULT_RANGE: RangeChoice = 'default'

export const PRESET_DAYS: Record<RangePreset, number> = {
  '24h': 1,
  '7d': 7,
  '14d': 14,
  '30d': 30,
  '90d': 90,
}

/** What the API is asked for: a rolling length, or a fixed `[since, until)` in epoch seconds. */
export type RangeQuery = { days: number } | { since: number; until: number }

const DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/

/** A date input's value (`YYYY-MM-DD`) for the LOCAL calendar day of `d`. */
export function localDay(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** Local midnight of a `YYYY-MM-DD` value, or null when it is not a real date. */
export function parseLocalDay(value: string): Date | null {
  const m = DAY_RE.exec(value)
  if (!m) return null
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  // `new Date(2026, 1, 31)` rolls into March; a rolled date is not the one typed.
  return localDay(d) === value ? d : null
}

/** The backend's `MAX_WINDOW_DAYS`, used until a payload reports its own. */
export const MAX_RANGE_DAYS = 90

/**
 * The earliest day a custom range may start on: `maxDays - 1` days before today,
 * so start through today is at most `maxDays` inclusive dates. One day earlier
 * would be `maxDays + 1` dates, and the gateway's clamp (`now - maxDays` days)
 * would cut into that first day while its label still named it whole.
 *
 * The gateway's floor is `maxDays * 24h`, not calendar days: a DST fall-back in
 * the span adds an hour, so late in the evening that day's midnight can sit
 * before the floor. It then moves up a day, until its midnight is inside.
 */
export function earliestDay(maxDays: number, now: Date = new Date()): Date {
  const floor = now.getTime() - maxDays * 86_400_000
  let back = maxDays - 1
  let day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - back)
  while (back > 0 && day.getTime() < floor) {
    back -= 1
    day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - back)
  }
  return day
}

/**
 * The custom range's dates, with the default last-week span filling a blank end.
 * A start before {@link earliestDay} moves up to it (a typed date ignores the
 * input's `min`), and an end before the start moves up to the start.
 */
export function customDays(
  since: string,
  until: string,
  now: Date = new Date(),
): { since: string; until: string } {
  let end = parseLocalDay(until) ?? new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const fallbackStart = new Date(end.getFullYear(), end.getMonth(), end.getDate() - 6)
  let start = parseLocalDay(since) ?? fallbackStart
  const earliest = earliestDay(MAX_RANGE_DAYS, now)
  if (start < earliest) start = earliest
  if (end < start) end = start
  return { since: localDay(start), until: localDay(end) }
}

/**
 * The query for a choice: `null` (no window) for `default`, a rolling length for
 * a preset. A custom range runs from the start day's local midnight
 * to the midnight AFTER the end day, so both picked days are whole and inclusive.
 * The bounds go out as epoch seconds, not dates, so the gateway reads the days in
 * the browser's timezone rather than its own.
 */
export function rangeQuery(
  choice: RangeChoice,
  since: string,
  until: string,
  now: Date = new Date(),
): RangeQuery | null {
  // `default` names no window, so the gateway answers with main's per-card ones.
  if (choice === 'default') return null
  if (choice !== 'custom') return { days: PRESET_DAYS[choice] }
  const days = customDays(since, until, now)
  const start = parseLocalDay(days.since) as Date
  const endDay = parseLocalDay(days.until) as Date
  const end = new Date(endDay.getFullYear(), endDay.getMonth(), endDay.getDate() + 1)
  return { since: start.getTime() / 1000, until: end.getTime() / 1000 }
}

/**
 * The query for the window a payload REPORTS it measured, so a second fetch
 * (the per-session turn drill-down) reads exactly those bounds. Re-sending the
 * request instead would let each reader re-clamp a future `until` to its own
 * "now" and drift past the aggregate's end. Null when the payload names no
 * window.
 */
export function measuredQuery(w: {
  window_days?: number
  window_rolling?: boolean
  window_start?: string
  window_end?: string
}): RangeQuery | null {
  if (w.window_rolling === false) {
    const start = Date.parse(w.window_start ?? '')
    const end = Date.parse(w.window_end ?? '')
    if (!Number.isFinite(start) || !Number.isFinite(end)) return null
    return { since: start / 1000, until: end / 1000 }
  }
  return typeof w.window_days === 'number' && w.window_days > 0 ? { days: w.window_days } : null
}

/** The query string (no leading `?`) for a range; empty for no window. */
export function rangeSearch(q: RangeQuery | null): string {
  if (!q) return ''
  const p = new URLSearchParams()
  if ('days' in q) p.set('days', String(q.days))
  else {
    p.set('since', String(q.since))
    p.set('until', String(q.until))
  }
  return p.toString()
}

/**
 * The last instant a `[start, end)` window covers, for an inclusive end label.
 * A range ending at midnight is labelled with the day before that midnight.
 */
export function inclusiveEnd(endIso: string): Date {
  return new Date(new Date(endIso).getTime() - 1)
}
