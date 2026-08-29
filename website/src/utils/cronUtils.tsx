/** Shared cron formatting utilities used by SchedulePage and JobForm */
import { Save, Plus } from 'lucide-react'
import { activeLocale, fmtDateFields, fmtNumber, fmtWeekday } from '../i18n/format'
import type { CronJob } from '../types'
import { i18nT } from '../i18n/t'

export const TH_CLS = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'
export const TD_CLS = 'px-2.5 py-2 border-b border-border text-sm'

/** Render table header cells from column definitions */
export function renderThCells(cols: { h: string; w: string }[]) {
  return cols.map(c => <th key={c.h} className={`${TH_CLS} ${c.w}`}>{c.h}</th>)
}

const DOW_NAMES: Record<string, number> = { SUN: 0, MON: 1, TUE: 2, WED: 3, THU: 4, FRI: 5, SAT: 6 }

/** Resolve a single token (numeric or named) to a cron DOW number, or -1 if invalid */
function parseDowToken(t: string): number {
  if (t === '') return -1
  const named = DOW_NAMES[t.toUpperCase()]
  if (named !== undefined) return named
  if (isNaN(+t)) return -1
  return +t  // preserve raw value (0-7); caller normalizes with %7
}

/** Expand a cron dow field (e.g. "1-5", "MON-FRI", "0,6", "MON,WED,FRI") into an array of individual numbers */
export function expandDow(dow: string): number[] {
  return [...new Set(dow.split(',').flatMap(part => {
    const m = part.match(/^([A-Za-z0-9]+)-([A-Za-z0-9]+)$/)
    if (!m) { const v = parseDowToken(part); return v < 0 ? [] : [v % 7] }
    const start = parseDowToken(m[1]), end = parseDowToken(m[2])
    if (start < 0 || end < 0) return []
    const nums: number[] = []
    if (start > end) {
      for (let i = start; i <= 6; i++) nums.push(i % 7)
      for (let i = 0; i <= end; i++) nums.push(i % 7)
    } else {
      for (let i = start; i <= end; i++) nums.push(i % 7)
    }
    return nums
  }))]
}

/** A cron minute/hour pair as a locale-formatted clock time, or null if either
 *  field is not a plain in-range number.
 *
 *  The fields are rendered as a WALL CLOCK, with no timezone conversion: the
 *  scheduler evaluates a cron expression in the job's own timezone, so the
 *  stored hour already IS the local hour. A fixed UTC reference date pinned with
 *  `timeZone: 'UTC'` keeps the host zone from shifting the digits, exactly as
 *  `fmtWeekday` does for weekday names. 12- vs 24-hour is the locale's choice. */
function cronClock(minute: string, hour: string): string | null {
  if (!/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour)) return null
  const m = +minute, h = +hour
  if (m > 59 || h > 23) return null
  return fmtDateFields(new Date(Date.UTC(2024, 0, 1, h, m)), {
    hour: 'numeric', minute: '2-digit', timeZone: 'UTC',
  })
}

/** A cron month number as a locale-formatted short month name, or null. */
function cronMonth(month: string): string | null {
  if (!/^\d{1,2}$/.test(month) || +month < 1 || +month > 12) return null
  return fmtDateFields(new Date(Date.UTC(2024, +month - 1, 1)), {
    month: 'short', timeZone: 'UTC',
  })
}

/** A cron month + day-of-month pair in the LOCALE's own field order — `Feb 30`
 *  in en, `2月30日` in ja, `30. Feb` in de.
 *
 *  Composed from `formatToParts` on a date carrying the RIGHT MONTH but a SAFE
 *  DAY (the 15th, which every month has), substituting only the day. Two traps
 *  make the obvious spellings wrong:
 *
 *  - Formatting the real date cannot work, because cron can name a day that does
 *    not exist: `0 0 30 2 *` asks for February 30, and `Date.UTC(2024, 1, 30)`
 *    rolls over to March 1 — a confidently WRONG month.
 *  - Substituting a pre-formatted month NAME cannot work either. Under
 *    `{month: 'short'}` a CJK locale returns the month as a bare number with its
 *    `月` as a separate LITERAL part, so injecting the full `2月` yields `2月月30日`.
 *    Letting the formatter render the month and replacing only the day keeps
 *    whatever shape the locale uses. */
function cronMonthDay(month: number, day: number): string {
  // `fmtNumber`, not `String(day)`: substituting ASCII gave bn `30 ফেব` for
  // `৩০ ফেব`. `useGrouping: false` keeps a bare day out of thousands form.
  const localizedDay = fmtNumber(day, { useGrouping: false })
  const parts = new Intl.DateTimeFormat(activeLocale(), {
    month: 'short', day: 'numeric', timeZone: 'UTC',
  }).formatToParts(new Date(Date.UTC(2024, month - 1, 15)))
  // A locale whose short-date pattern omits the day would silently drop it; fall
  // back to a space-joined pair rather than render a half label.
  if (!parts.some(p => p.type === 'day')) {
    return `${cronMonth(String(month)) ?? localizedDay} ${localizedDay}`
  }
  return parts.map(p => (p.type === 'day' ? localizedDay : p.value)).join('')
}

/** A cron day-of-week field as locale-formatted names; `''` when it restricts
 *  nothing, or null when no honest short form exists.
 *
 *  Built on `expandDow`, so named tokens (`MON-FRI`) and wrap-around ranges
 *  (`5-1`) resolve for free, and Sunday works spelled 0 or 7.
 *
 *  Three outcomes, because `JobForm`'s weekly picker emits a comma LIST from its
 *  toggle buttons and every shape below is something a user can produce without
 *  typing cron:
 *
 *  - A set covering all seven days restricts nothing, so it yields `''` and the
 *    caller renders the bare clock. `Sun-Sat` would be a second spelling of
 *    "daily" sitting in the same column as a `*` job's.
 *  - A CONTIGUOUS set collapses to its endpoints (`Mon-Fri`) however it was
 *    spelled -- five ticked weekdays arrive as `1,2,3,4,5`.
 *  - Up to three names list out (`Mon,Wed,Fri`), which covers the gappy sets the
 *    toggles produce. Past that, null: the raw expression is shorter than the
 *    names AND exact, where a fabricated range would assert days the set omits. */
function cronDows(field: string): string | null {
  const name = (d: number) => fmtWeekday(d === 0 ? 7 : d)
  const expanded = expandDow(field)
  if (expanded.length === 0 || field.includes('/')) return null
  if (expanded.length >= 7) return ''
  // A literal range keeps its own endpoints, which carry the author's wrap-around
  // direction (`5-1` is Fri..Mon, whose ascending sort would read Sun-Sat).
  if (field.includes('-')) {
    return field.includes(',')
      ? null // mixed range + list: no single pair of endpoints describes it
      : `${name(expanded[0])}-${name(expanded[expanded.length - 1])}`
  }
  const sorted = [...expanded].sort((a, b) => a - b)
  const contiguous = sorted.length >= 3
    && sorted.every((d, i) => i === 0 || d === sorted[i - 1] + 1)
  if (contiguous) return `${name(sorted[0])}-${name(sorted[sorted.length - 1])}`
  return expanded.length <= 3 ? expanded.map(name).join(',') : null
}

/**
 * A cron expression as a COMPACT label for a narrow column: `12:00 AM · Feb 30`,
 * `9:00 AM · Mon-Fri`, `12:00 AM`.
 *
 * Every word comes from `Intl` via `fmtWeekday` / `fmtDateFields`, so the label
 * translates with the dashboard instead of pinning English into it. That is also
 * why an unhandled shape returns the RAW EXPRESSION rather than prose: the raw
 * form needs no catalog entry, is shorter than the sentence it replaces, and is
 * exactly what the user typed.
 *
 * A restricted day-of-month AND day-of-week is deliberately NOT combined. Cron
 * ORs those two fields, so `0 0 1 * 1` fires on the 1st *and* every Monday — the
 * previous spelling of this function rendered that as `Mon 00:00 (days 1)`,
 * asserting an AND that is not there. It now takes the raw-expression path.
 */
export function fmtCron(expr: string): string {
  try {
    const raw = expr.trim()
    const p = raw.split(/\s+/)
    if (p.length !== 5) return expr
    const [min, hr, dom, month, dow] = p
    const clock = cronClock(min, hr)
    if (clock === null) return raw
    // null = no honest short form, fall back to the expression. '' = the field
    // restricts nothing, so the clock alone already says it.
    const withQualifier = (q: string | null) =>
      q === null ? raw : q === '' ? clock : `${clock} · ${q}`
    if (dow !== '*') {
      // OR-semantics: only a day-of-week alone can be joined to the clock.
      return dom === '*' && month === '*' ? withQualifier(cronDows(dow)) : raw
    }
    if (month !== '*') {
      const monthName = cronMonth(month)
      if (monthName === null) return raw
      if (dom === '*') return withQualifier(monthName)
      return /^\d{1,2}$/.test(dom) && +dom >= 1 && +dom <= 31
        ? withQualifier(cronMonthDay(+month, +dom))
        : raw
    }
    // A bare day-of-month has no Intl rendering and would need an untranslatable
    // "day N", so it takes the raw expression too.
    return dom === '*' ? clock : raw
  } catch { return expr }
}

/**
 * The Schedule column's value for a job — ONE definition, because the cell and
 * the column's sort comparator must agree. They did not before: the cell showed
 * a compact label while the comparator keyed on the verbose backend string, so
 * sorting ordered rows by text the user could not see.
 *
 * A cron job renders through `fmtCron` over the payload's `cron_expr`. An
 * interval or one-shot has no `cron_expr` and its backend string ("every 600s")
 * is already compact, so that is kept as-is.
 */
export function scheduleLabel(j: CronJob): string {
  return j.cron_expr ? fmtCron(j.cron_expr) : (j.schedule || '')
}

/**
 * Minutes-since-midnight of a job's rendered clock, or null when it has none.
 *
 * The Schedule column's sort needs this because the LABEL sorts wrongly as text:
 * `1:00 PM` collates before `9:00 AM`, so ordering by the visible string — which
 * is what this PR set out to do — still produced an order no reader expects.
 *
 * Null covers both rows with no clock to compare: a non-cron job, and a cron
 * shape `fmtCron` declines, which renders as its raw expression. The fields of a
 * declined shape may still parse, so the check is whether `fmtCron` actually
 * produced a label rather than whether the digits look like a time.
 */
export function scheduleMinutes(j: CronJob): number | null {
  if (!j.cron_expr) return null
  const expr = j.cron_expr.trim()
  const [min, hr, ...rest] = expr.split(/\s+/)
  if (rest.length !== 3) return null
  if (!/^\d{1,2}$/.test(min) || !/^\d{1,2}$/.test(hr)) return null
  if (+min > 59 || +hr > 23) return null
  return fmtCron(expr) === expr ? null : +hr * 60 + +min
}

/** Save/Create button label with icon — shared by JobForm and SchedulePage */
export function SaveCreateLabel({ isEdit, saving }: { isEdit: boolean; saving: boolean }) {
  return (
    <span className="flex items-center gap-1.5">
      {isEdit ? <Save size={14} /> : <Plus size={14} />}
      {saving ? i18nT('utils.cronUtils.saving') : (isEdit ? i18nT('utils.cronUtils.save') : i18nT('utils.cronUtils.create'))}
    </span>
  )
}
