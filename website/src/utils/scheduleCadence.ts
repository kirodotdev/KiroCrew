import { fmtTime, fmtUnit, fmtWeekday } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { surfaceMachineValue } from '../surfaces/registry'

import type { CronPrefill } from './schedulePresets'

/**
 * Render a preset's human-readable cadence from its schedule, rather than
 * storing it as a string.
 *
 * The stored form was English-frozen by construction: values like `6:00am` and
 * `Mondays` bake an en-US clock and weekday name into every locale, and a
 * translator retyping `6:00am` does not make it `06:00` in de-DE. Deriving it
 * routes both through `src/i18n/format.ts` (`Intl` under the hood), so the clock
 * follows the locale's own convention and the weekday comes from CLDR data. Only
 * the sentence FRAME is a catalog string; every value inside it is formatted.
 *
 * `fmtUnit` also removes a pluralization problem the catalog would otherwise
 * own: `Intl` selects the right unit form for the count and locale, so there is
 * no `_one`/`_few`/`_many` ladder to maintain per language.
 */

/** A fixed reference date used only to render a wall-clock `HH:MM` through Intl. */
function timeLabel(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number)
  if (!Number.isFinite(h) || !Number.isFinite(m)) return hhmm
  const d = new Date(2024, 0, 1, h, m, 0, 0)
  return fmtTime(d)
}

/** `0 8 * * *` -> `{ minute: 0, hour: 8, dow: '*' }`, or null when not a 5-field expression. */
function parseCron(expr: string): { minute: number; hour: number; dow: string } | null {
  const f = expr.trim().split(/\s+/)
  if (f.length !== 5) return null
  const minute = Number(f[0])
  const hour = Number(f[1])
  if (!Number.isInteger(minute) || !Number.isInteger(hour)) return null
  return { minute, hour, dow: f[4] }
}

/**
 * Cron day-of-week spellings that mean "the working week". These are cron
 * SYNTAX, not user-facing copy, so they go through `surfaceMachineValue` -- the
 * project's marker for strings that must never be translated.
 */
const WEEKDAY_RANGE = new Set<string>([
  surfaceMachineValue('1-5'),
  surfaceMachineValue('MON-FRI'),
  surfaceMachineValue('mon-fri'),
])

export function formatCadence(prefill: CronPrefill): string {
  if (prefill.schedMode === 'interval' && prefill.intVal && prefill.intUnit) {
    const unit = prefill.intUnit === 'minutes' ? 'minute' : prefill.intUnit === 'hours' ? 'hour' : 'day'
    return i18nT('utils.scheduleCadence.every_amount', { amount: fmtUnit(prefill.intVal, unit) })
  }

  if (prefill.schedMode === 'weekly' && prefill.weekDays?.length && prefill.weekTime) {
    const days = prefill.weekDays
    const time = timeLabel(prefill.weekTime)
    if (days.length === 1) {
      return i18nT('utils.scheduleCadence.weekly_on_day', { day: fmtWeekday(days[0], 'long'), time })
    }
    return i18nT('utils.scheduleCadence.weekly_on_days', {
      days: days.map(d => fmtWeekday(d, 'short')).join(', '),
      time,
    })
  }

  if (prefill.schedMode === 'cron' && prefill.cronExpr) {
    const c = parseCron(prefill.cronExpr)
    if (c) {
      const time = timeLabel(`${c.hour}:${String(c.minute).padStart(2, '0')}`)
      if (WEEKDAY_RANGE.has(c.dow)) return i18nT('utils.scheduleCadence.weekdays_at', { time })
      if (c.dow === '*') return i18nT('utils.scheduleCadence.daily_at', { time })
      const single = Number(c.dow)
      if (Number.isInteger(single) && single >= 1 && single <= 7) {
        return i18nT('utils.scheduleCadence.weekly_on_day', { day: fmtWeekday(single, 'long'), time })
      }
    }
    // An expression this helper does not model renders verbatim: a wrong
    // human label would be worse than the raw schedule the user can read.
    return prefill.cronExpr
  }

  return ''
}
