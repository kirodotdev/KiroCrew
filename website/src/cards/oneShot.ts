/**
 * A schedule card that runs ONCE: the gateway marks it `once: true` (on the
 * card or in its params) and carries an `at` param instead of `cron_expr`,
 * with the resolved run time in `next_run_at`. Such a card has no recurrence,
 * so it reads "Runs once" and a date, never a cron humanization or "Next run".
 */
import { i18nT } from '../i18n/t'
import { activeLocale, fmtDateFields, fmtRelative, toDate } from '../i18n/format'
import type { Card } from '../api/cards'

const SCHEDULE_KINDS = new Set(['schedule.create', 'schedule.update'])

type OneShotCard = Pick<Card, 'kind' | 'params'> & Partial<Pick<Card, 'once' | 'next_run_at' | 'timezone'>>

export function isOneShot(card: OneShotCard): boolean {
  if (!SCHEDULE_KINDS.has(card.kind)) return false
  if (card.once === true || card.params.once === true) return true
  const at = card.params.at
  return (typeof at === 'string' ? at.trim() !== '' : typeof at === 'number') && !card.params.cron_expr
}

/** The card's time zone: the gateway's resolved one, else the param. */
export function cardTimezone(card: OneShotCard): string | undefined {
  return card.timezone ?? (typeof card.params.timezone === 'string' ? card.params.timezone : undefined)
}

const DAY: Intl.DateTimeFormatOptions = { year: 'numeric', month: 'numeric', day: 'numeric' }

/** A local date-time with no offset, as a one-shot's `at` param is written. */
const WALL_TIME_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/

/** How far *timeZone*'s wall clock is ahead of UTC at instant *ms*, in ms. */
function zoneOffset(ms: number, timeZone: string): number {
  // A machine read, not a display: 'en-US' with a 24-hour cycle is named so
  // the parts are digits whatever the reader's language.
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric',
  }).formatToParts(ms)
  const n = (type: string) => Number(parts.find(p => p.type === type)?.value)
  const asUtc = Date.UTC(n('year'), n('month') - 1, n('day'), n('hour'), n('minute'), n('second'))
  return asUtc - Math.floor(ms / 1000) * 1000
}

/**
 * The instant a local wall time names IN *timeZone* (the gateway reads `at`
 * the same way), or null when *value* is not such a wall time or the zone is
 * unknown. `new Date('2026-10-07T09:00')` would read it in the BROWSER's zone
 * instead, so a reader whose machine is not in the card's zone saw another time.
 */
export function wallTimeIn(value: string, timeZone: string): number | null {
  const m = WALL_TIME_RE.exec(value.trim())
  if (!m) return null
  const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] ?? 0))
  try {
    const first = guess - zoneOffset(guess, timeZone)
    // Once more at the found instant, so a wall time across a DST change from
    // the guess still lands on its own offset.
    return guess - zoneOffset(first, timeZone)
  } catch {
    return null
  }
}

/** *value* as a date: a bare wall time is read in *timeZone*, anything else as is. */
function oneShotDate(value: string | number | null | undefined, timeZone?: string): Date | null {
  if (typeof value === 'string' && timeZone) {
    const ms = wallTimeIn(value, timeZone)
    if (ms !== null) return new Date(ms)
  }
  return toDate(value ?? null)
}

/**
 * When a one-shot runs, in the reader's language and the schedule's zone:
 * "Tomorrow, Oct 4 · 9:00 AM PDT". Today / tomorrow read relatively; any other
 * day names its weekday. `capitalize` is for a value that starts a row; a
 * value inside a sentence keeps the locale's own lowercase ("tomorrow").
 */
export function fmtOneShot(
  value: string | number | null | undefined,
  timeZone?: string,
  { capitalize = true, now = Date.now() }: { capitalize?: boolean; now?: number } = {},
): string | null {
  const d = oneShotDate(value, timeZone)
  if (!d) return null
  const tz = timeZone ? { timeZone } : {}
  const dayOf = (x: number) => fmtDateFields(x, { ...DAY, ...tz })
  const target = dayOf(d.getTime())
  const sameYear = fmtDateFields(d, { year: 'numeric', ...tz }) === fmtDateFields(now, { year: 'numeric', ...tz })
  let day: string
  if (target === dayOf(now)) day = fmtRelative(now, { now, unit: 'day', style: 'long' })
  else if (target === dayOf(now + 86_400_000)) day = fmtRelative(now + 86_400_000, { now, unit: 'day', style: 'long' })
  else day = fmtDateFields(d, { weekday: 'short', ...tz })
  if (capitalize) day = day.charAt(0).toLocaleUpperCase(activeLocale()) + day.slice(1)
  const date = fmtDateFields(d, { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }), ...tz })
  const time = fmtDateFields(d, { hour: 'numeric', minute: '2-digit', timeZoneName: 'short', ...tz })
  return i18nT('components.changeCards.schedule_once_when', { day, date, time })
}

/** The one-shot's run time: the gateway's resolved `next_run_at`, else `at`. */
export function oneShotWhen(card: OneShotCard, opts?: { capitalize?: boolean; now?: number }): string | null {
  const at = card.params.at
  const raw = card.next_run_at ?? (typeof at === 'string' || typeof at === 'number' ? at : null)
  return fmtOneShot(raw, cardTimezone(card), opts)
}

/**
 * When a one-shot runs, as a timestamp, or null for anything else. A run time
 * that has passed cannot be scheduled (the gateway answers `at_in_past`), so
 * the card has to know before the person presses the button, not after.
 */
export function oneShotRunAt(card: OneShotCard): number | null {
  if (!isOneShot(card)) return null
  const at = card.params.at
  const d = oneShotDate(card.next_run_at ?? (typeof at === 'string' || typeof at === 'number' ? at : null), cardTimezone(card))
  return d ? d.getTime() : null
}
