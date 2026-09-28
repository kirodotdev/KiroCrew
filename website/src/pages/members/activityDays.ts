/**
 * Pure helpers behind the Crew Members drawer's "Recent activity" block: fold
 * a member's activity log into calendar days and label them. No React, no IO,
 * so the day-boundary, cap and label edge cases are unit-testable without
 * rendering the 1800-line page.
 */
import type { MemberActivityEntry } from '../../api/client'
import { fmtDateFields, fmtRelative } from '../../i18n/format'
import { localDaysAgo } from '../chat/sessionOrder'

/** One calendar day of a member's activity log, folded for the drawer. */
export type ActivityDay = {
  /** Local midnight, epoch seconds. */
  dayStart: number
  chats: number
  routed: number
  /** Distinct project paths, first-seen order. */
  projects: string[]
  /** The day's entries, oldest first. */
  entries: MemberActivityEntry[]
  /** True when this day holds the oldest entry the server returned AND the
   *  server capped the list: older events may exist, so the counts are floors. */
  isFloor: boolean
}

/** Fold entries into local calendar days, newest day first. Local midnight is
 *  the boundary — the same "today" the drawer's stat card counts against. */
export function groupActivityDays(entries: readonly MemberActivityEntry[], capped: boolean): ActivityDay[] {
  const days = new Map<number, ActivityDay>()
  let oldestTs = Infinity
  for (const e of entries) {
    const d = new Date(e.ts * 1000)
    d.setHours(0, 0, 0, 0)
    const dayStart = d.getTime() / 1000
    let day = days.get(dayStart)
    if (!day) {
      day = { dayStart, chats: 0, routed: 0, projects: [], entries: [], isFloor: false }
      days.set(dayStart, day)
    }
    if (e.via === 'select_crew') day.routed += 1
    else day.chats += 1
    if (e.project && !day.projects.includes(e.project)) day.projects.push(e.project)
    day.entries.push(e)
    if (e.ts < oldestTs) oldestTs = e.ts
  }
  return [...days.values()]
    .sort((a, b) => b.dayStart - a.dayStart)
    .map((d) => ({
      ...d,
      entries: [...d.entries].sort((a, b) => a.ts - b.ts),
      // The cap truncates the OLD end of the log, so only the day holding the
      // oldest returned entry can be missing events. Mirrors the stat cards'
      // `todayIsFloor` / `weekIsFloor`: a count the drawer cannot know exactly
      // is shown as a floor, never asserted.
      isFloor: capped && d.entries.some((e) => e.ts === oldestTs),
    }))
}

/** A project path's last segment — `/…/crew/workspace` reads as `workspace`.
 *  The full path stays in the hover title. */
export function projectLabel(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || path
}

/** Label for one activity day (local midnight, epoch seconds): the nearest
 *  three days as the locale words them (today / yesterday / 前天), the rest of
 *  the week by weekday, anything older by date. */
export function activityDayLabel(dayStart: number, now: Date = new Date()): string {
  // Civil-day arithmetic from the shared helper, not raw seconds: two local
  // midnights are 23h or 25h apart across a DST change.
  const diffDays = localDaysAgo(now, new Date(dayStart * 1000))
  if (diffDays <= 2) {
    // fmtRelative re-derives the count by truncating raw seconds, so hand it an
    // instant exactly `diffDays` civil days back rather than the real midnight —
    // otherwise the 23h day would come back as "today".
    const today = new Date(now)
    today.setHours(0, 0, 0, 0)
    return fmtRelative(today.getTime() - diffDays * 86_400_000, { now: today, unit: 'day', style: 'long' })
  }
  if (diffDays <= 6) return fmtDateFields(dayStart, { weekday: 'short' })
  return fmtDateFields(dayStart, { month: 'short', day: 'numeric' })
}

/** Mark a translated count phrase as a floor: `2 chats` -> `2+ chats`. The
 *  phrase is rendered for `shown` (the caller passes `floor + 1`, so a floor of
 *  one still reads in the plural — "1+ chats", not "1+ chat"), and the digits of
 *  `shown` are replaced by `<floor>+`. The catalogs interpolate `{{count}}` as
 *  a plain number with no literal digits ahead of it, so the first occurrence
 *  of the digits is the number being qualified. */
export function floorCountText(text: string, shown: number, floor: number): string {
  const digits = String(shown)
  const at = text.indexOf(digits)
  return at < 0 ? text : text.slice(0, at) + String(floor) + '+' + text.slice(at + digits.length)
}
