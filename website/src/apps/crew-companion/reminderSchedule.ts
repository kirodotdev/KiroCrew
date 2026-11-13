/**
 * The hand-set schedule behind the reminder row's Change schedule editor: what the
 * editor opens with for an existing reminder, and the `{fireAt, everyMinutes}` it
 * saves. Local time throughout, the same clock the text parser reads.
 */
import type { Reminder } from './types'

/** Each Repeat choice and the interval it stores; `null` is one-time. */
export const REPEAT_MINUTES = { once: null, hourly: 60, daily: 1440, weekly: 10080 } as const
export type Repeat = keyof typeof REPEAT_MINUTES
export const REPEATS = Object.keys(REPEAT_MINUTES) as Repeat[]

export interface Schedule {
  /** `custom` keeps the reminder's own interval, one no Repeat choice matches. */
  repeat: Repeat | 'custom'
  /** Local clock time. Hourly reads only `minute`. */
  hour: number
  minute: number
  /** ISO weekday, 1 is Monday and 7 is Sunday. Weekly only. */
  weekday: number
}

const isoWeekday = (d: Date) => d.getDay() || 7

/** The editor's starting values: the reminder's next fire and its repeat. */
export function scheduleOf(r: Reminder): Schedule {
  const at = new Date(Date.parse(r.fireAt))
  const every = r.recurrence?.everyMinutes ?? null
  return {
    repeat: REPEATS.find((k) => REPEAT_MINUTES[k] === every) ?? 'custom',
    hour: at.getHours(),
    minute: at.getMinutes(),
    weekday: isoWeekday(at),
  }
}

/**
 * The schedule's first fire strictly after `now`, as the ISO instant the backend
 * stores. A one-time or custom reminder keeps `day`, its own date, unless that has passed.
 */
export function nextFireAt(s: Schedule, now: Date, day: Date = now): string {
  if (s.repeat === 'hourly') {
    const at = new Date(now)
    at.setMinutes(s.minute, 0, 0)
    // Elapsed hours, not setHours, and a loop: inside the repeated hour of a fall-back
    // change, setMinutes lands on the first pass of that hour, two hours back.
    while (at <= now) at.setTime(at.getTime() + 3_600_000)
    return at.toISOString()
  }
  const at = new Date(s.repeat === 'once' || s.repeat === 'custom' ? day : now)
  at.setHours(s.hour, s.minute, 0, 0)
  for (;;) {
    // The repeated hour of a fall-back change shows this clock time twice, and setHours
    // picks the first pass: take the second while it is still ahead.
    const again = new Date(at.getTime() + 3_600_000)
    if (at <= now && again > now && again.getHours() === s.hour && again.getMinutes() === s.minute) at.setTime(again.getTime())
    if (at > now && (s.repeat !== 'weekly' || isoWeekday(at) === s.weekday)) break
    // setDate, not +24h, so the first fire keeps the clock time across a daylight-saving change.
    // setHours again on each new day: a time inside a spring-forward gap normalizes an
    // hour later, and that shifted time must not carry over to the next day.
    at.setDate(at.getDate() + 1)
    at.setHours(s.hour, s.minute, 0, 0)
  }
  return at.toISOString()
}
