/**
 * A recurring schedule as a card reads it: the Schedule page's own label
 * (`fmtCron`) plus the recurrence it leaves implicit.
 *
 * The Schedule column is a list of recurring jobs, so its compact `9:00 AM`
 * for a daily cron needs no "every day" there. A change card is a one-off
 * proposal beside reminders that DO run once, and there the bare clock read as
 * a single run. So the card never shows a bare clock: it adds the recurrence
 * (`9:00 AM · Every day`), and names the interval, weekly, monthly and yearly
 * shapes `fmtCron` declines or leaves ambiguous. Clock, weekday and month words
 * still come from `fmtCron`, so the two surfaces cannot drift apart; a shape
 * neither knows stays the raw expression, which is exact.
 */
import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'
import { expandDow, fmtCron } from '../utils/cronUtils'

const SEP = ' · '
const PLAIN = /^\d{1,2}$/
const STEP = /^\*\/(\d{1,2})$/

/** "Every 15 minutes" / "Every 2 hours", the unit's plural chosen by Intl. */
function everyInterval(n: number, unit: 'minute' | 'hour'): string {
  if (n === 1) {
    return unit === 'minute' ? i18nT('components.changeCards.cron_every_minute') : i18nT('components.changeCards.cron_every_hour')
  }
  return i18nT('components.changeCards.cron_every_interval', { interval: fmtNumber(n, { style: 'unit', unit, unitDisplay: 'long' }) })
}

export function fmtCardCron(expr: string): string {
  const raw = expr.trim()
  const p = raw.split(/\s+/)
  if (p.length !== 5) return fmtCron(expr)
  const [min, hr, dom, month, dow] = p
  const everyDate = dom === '*' && month === '*' && dow === '*'

  // Interval shapes have no clock for `fmtCron` to print.
  if (everyDate && hr === '*') {
    if (min === '*') return everyInterval(1, 'minute')
    const step = STEP.exec(min)
    if (step && +step[1] >= 1 && +step[1] <= 59) return everyInterval(+step[1], 'minute')
    if (min === '0') return everyInterval(1, 'hour')
    return raw
  }
  if (everyDate && min === '0') {
    const step = STEP.exec(hr)
    if (step && +step[1] >= 1 && +step[1] <= 23) return everyInterval(+step[1], 'hour')
  }

  // A bare day-of-month repeats monthly; `fmtCron` declines it for lack of a
  // translatable "day N", which the card has.
  if (dow === '*' && month === '*' && PLAIN.test(dom) && +dom >= 1 && +dom <= 31) {
    const clock = fmtCron(`${min} ${hr} * * *`)
    if (clock === `${min} ${hr} * * *`) return raw
    return `${clock}${SEP}${i18nT('components.changeCards.cron_monthly_on', { day: fmtNumber(+dom, { useGrouping: false }) })}`
  }

  const label = fmtCron(raw)
  if (label === raw) return raw
  const cut = label.indexOf(SEP)
  const clock = cut < 0 ? label : label.slice(0, cut)
  const qualifier = cut < 0 ? '' : label.slice(cut + SEP.length)
  // No qualifier: the expression restricts no day, so it runs daily.
  if (!qualifier) return `${clock}${SEP}${i18nT('components.changeCards.cron_every_day')}`
  if (dow !== '*') {
    // One weekday alone ("Mon") reads like a single date; a set of them
    // ("Mon-Fri", "Mon,Wed,Fri") already reads as repeating.
    return expandDow(dow).length === 1
      ? `${clock}${SEP}${i18nT('components.changeCards.cron_weekly_on', { days: qualifier })}`
      : label
  }
  // A month (with or without its day) is every year, not one date.
  const yearly = dom === '*'
    ? i18nT('components.changeCards.cron_every_day_in', { month: qualifier })
    : i18nT('components.changeCards.cron_yearly_on', { date: qualifier })
  return `${clock}${SEP}${yearly}`
}
