/**
 * Timezone helpers for the Schedule page.
 *
 * Cron expressions store clock-time (hour/minute/day-of-week) in a named
 * IANA timezone — but JavaScript's `Date` only knows UTC and the browser's
 * local timezone. This module converts clock-time from one IANA zone
 * ("source") to the clock-time that represents the same instant in another
 * IANA zone ("target"), so the Schedule grid can render each job's slots
 * in a user-selected render timezone regardless of where the job was
 * authored.
 */

/** Current-week Monday in the given timezone (returns a UTC Date whose
 *  `toLocaleString(..., timeZone=tz)` parts are that Monday at 12:00).
 *
 *  LIMITATION: The noon-UTC anchor is safe for IANA zones whose offset
 *  falls within ±13:59 from UTC. Zones at UTC+14 (Pacific/Kiritimati,
 *  Pacific/Apia during DST) may misalign by one calendar day because
 *  noon UTC + 14h crosses into the following day. Not currently in
 *  scope for the Schedule UI. */
function mondayInTz(tz: string, now: Date = new Date()): Date {
  // Read the current wall-clock weekday in `tz` without bouncing through
  // the browser's local TZ.
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    weekday: 'short',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const weekdayStr = parts.find(p => p.type === 'weekday')?.value || 'Mon'
  const year = parseInt(parts.find(p => p.type === 'year')?.value || '2026')
  const month = parseInt(parts.find(p => p.type === 'month')?.value || '1')
  const day = parseInt(parts.find(p => p.type === 'day')?.value || '1')
  const WEEKDAY_TO_INDEX: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }
  const jsDow = WEEKDAY_TO_INDEX[weekdayStr] ?? 1
  const mondayOffset = jsDow === 0 ? -6 : 1 - jsDow
  // Anchor at 12:00 UTC so the calendar day is stable when read back in
  // any common IANA zone (offsets from UTC-12 to ~UTC+12 preserve the
  // calendar day at noon UTC; extreme +14 zones are not currently used
  // by KiroCrew and fall outside scope of the Schedule UI).
  const anchor = new Date(Date.UTC(year, month - 1, day, 12))
  anchor.setUTCDate(anchor.getUTCDate() + mondayOffset)
  return anchor
}

/** Convert a wall-clock time (day-of-week, hour, minute) expressed in the
 *  source IANA zone to the wall-clock time at the same instant in the
 *  target IANA zone.
 *
 *  @param srcDow cron DOW (0=Sun..6=Sat)
 *  @returns `{ dow, hour, minute }` in the target zone — `dow` uses the
 *           same 0=Sun..6=Sat convention.
 */
export function convertCronTime(
  srcDow: number,
  srcHour: number,
  srcMinute: number,
  srcTz: string,
  targetTz: string,
  now: Date = new Date(),
): { dow: number; hour: number; minute: number } {
  // LIMITATION: Assumes IANA-zone UTC offsets fall within ±13:59. Zones
  // at UTC+14 (Pacific/Kiritimati, Pacific/Apia DST) may produce
  // off-by-one-day results because the noon-UTC week anchor in
  // `mondayInTz` can roll into the next calendar day at +14h offsets.
  // Not currently in scope for the Schedule UI; revisit if KiroCrew
  // adds support for those zones.

  // Fast path: same zone (and both non-empty) → no arithmetic needed.
  if (srcTz && targetTz && srcTz === targetTz) {
    return { dow: srcDow, hour: srcHour, minute: srcMinute }
  }

  // 1. Anchor the current week's Monday in the source zone.
  const monday = mondayInTz(srcTz || 'UTC', now)
  // 2. Advance to the target DOW in the source zone (srcDow uses cron's
  //    0=Sun..6=Sat convention; Monday is index 1).
  const dayOffset = srcDow === 0 ? 6 : srcDow - 1
  const sourceDay = new Date(monday)
  sourceDay.setUTCDate(monday.getUTCDate() + dayOffset)

  // 3. Find the UTC instant whose wall-clock time in `srcTz` equals
  //    (sourceDay's Y/M/D in srcTz, srcHour:srcMinute). Using fixed-point
  //    iteration: start by assuming srcTz-offset ≈ 0, then refine.
  const srcYmd = new Intl.DateTimeFormat('en-US', {
    timeZone: srcTz || 'UTC',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(sourceDay)
  const y = parseInt(srcYmd.find(p => p.type === 'year')?.value || '2026')
  const m = parseInt(srcYmd.find(p => p.type === 'month')?.value || '1')
  const d = parseInt(srcYmd.find(p => p.type === 'day')?.value || '1')

  // Start from a UTC instant and iterate twice — converges for any IANA
  // zone including fractional UTC offsets (e.g. Asia/Kolkata = UTC+5:30).
  let utcMs = Date.UTC(y, m - 1, d, srcHour, srcMinute)
  for (let i = 0; i < 2; i++) {
    const offsetMin = tzOffsetMinutes(srcTz || 'UTC', new Date(utcMs))
    utcMs = Date.UTC(y, m - 1, d, srcHour, srcMinute) - offsetMin * 60_000
  }
  const instant = new Date(utcMs)

  // 4. Read the wall-clock in the target zone.
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: targetTz || 'UTC',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(instant)
  const weekdayStr = parts.find(p => p.type === 'weekday')?.value || 'Sun'
  const hourStr = parts.find(p => p.type === 'hour')?.value || '0'
  const minuteStr = parts.find(p => p.type === 'minute')?.value || '0'
  const WEEKDAY_TO_DOW: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }

  return {
    dow: WEEKDAY_TO_DOW[weekdayStr] ?? 0,
    hour: parseInt(hourStr),
    minute: parseInt(minuteStr),
  }
}

/** Offset in minutes between UTC and the given IANA zone at the given
 *  instant. A zone ahead of UTC (e.g. Europe/Berlin) returns a positive
 *  value; a zone behind UTC (e.g. America/New_York) returns negative. */
export function tzOffsetMinutes(tz: string, at: Date = new Date()): number {
  if (!tz || tz === 'UTC') return 0
  // Format the instant's parts in the target TZ and also in UTC, then
  // compare. This is the stable way to read an IANA offset without
  // relying on Intl.supportedValuesOf (Safari < 17).
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  })
  const parts = fmt.formatToParts(at)
  const lookup = Object.fromEntries(parts.map(p => [p.type, p.value])) as Record<string, string>
  const y = parseInt(lookup.year)
  const m = parseInt(lookup.month)
  const d = parseInt(lookup.day)
  const h = parseInt(lookup.hour)
  const asUtc = Date.UTC(y, m - 1, d, h, parseInt(lookup.minute), parseInt(lookup.second))
  return Math.round((asUtc - at.getTime()) / 60_000)
}

/** Now-of-day as fractional hours in the given IANA zone. */
export function hourFractionInTz(tz: string, now: Date = new Date()): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz || 'UTC',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(now)
  const hourStr = parts.find(p => p.type === 'hour')?.value || '0'
  const minuteStr = parts.find(p => p.type === 'minute')?.value || '0'
  const h = parseInt(hourStr)
  return h + parseInt(minuteStr) / 60
}

/** Cron DOW of "today" in the given IANA zone (0=Sun..6=Sat). */
export function todayCronDowInTz(tz: string, now: Date = new Date()): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz || 'UTC',
    weekday: 'short',
  }).formatToParts(now)
  const weekdayStr = parts.find(p => p.type === 'weekday')?.value || 'Sun'
  const WEEKDAY_TO_DOW: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }
  return WEEKDAY_TO_DOW[weekdayStr] ?? 0
}

/** Short, user-readable offset string, e.g. "UTC−4" or "UTC+05:30". */
export function formatTzOffset(tz: string, at: Date = new Date()): string {
  const mins = tzOffsetMinutes(tz, at)
  if (mins === 0) return 'UTC'
  const sign = mins >= 0 ? '+' : '−'
  const abs = Math.abs(mins)
  const h = Math.floor(abs / 60)
  const m = abs % 60
  return m === 0 ? `UTC${sign}${h}` : `UTC${sign}${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`
}
