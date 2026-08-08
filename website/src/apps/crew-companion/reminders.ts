/**
 * Presentation helpers for reminders — sorting, repeat labels and the when-label
 * pair. All user-visible words go through the dashboard i18n catalog; the clock time
 * itself is formatted with Intl in the dashboard's active language, so a reminder
 * row never shows an English time beside translated chrome.
 */

import { i18next } from '../../i18n'
import { i18nT } from '../../i18n/t'
import { BREAK_MIN_MINS, BREAK_MAX_MINS } from './constants'
import type { Reminder } from './types'


/** Chronological, with already-fired one-offs sunk to the bottom rather than hidden. */
export function sortedReminders(list: Reminder[]): Reminder[] {
  return [...list].sort((a, b) => {
    if (!!a.done !== !!b.done) return a.done ? 1 : -1
    return Date.parse(a.fireAt) - Date.parse(b.fireAt)
  })
}

/** "every 2h" / "daily" — the repeat, not the next fire time. */
export function repeatLabel(mins: number): string {
  if (mins === 1440) return i18nT('apps.crewCompanion.time.repeat_daily')
  if (mins % 1440 === 0) return i18nT('apps.crewCompanion.time.repeat_days', { n: mins / 1440 })
  if (mins % 60 === 0) return i18nT('apps.crewCompanion.time.repeat_hours', { n: mins / 60 })
  return i18nT('apps.crewCompanion.time.repeat_minutes', { n: mins })
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate()
}

/** The dashboard's active BCP-47 tag, for Intl date/time formatting. */
function activeLocale(): string | undefined {
  return i18next.language || undefined
}

function clockLabel(d: Date): string {
  return d.toLocaleTimeString(activeLocale(), { hour: 'numeric', minute: '2-digit' })
}

/**
 * Human label pair for a reminder.
 *
 *   under an hour  -> relative only          ("in 45 min")
 *   later today    -> absolute + relative    ("3:00 PM" / "in 1h 20m")
 *   beyond today   -> day-qualified          ("tomorrow" / "Wed 3:00 PM")
 */
export function labelFor(fireAt: string, now: Date): { absLabel?: string; relLabel: string } {
  const at = new Date(Date.parse(fireAt))
  const diffMs = at.getTime() - now.getTime()
  const mins = Math.round(diffMs / 60_000)

  if (mins < 60) {
    return { relLabel: mins <= 0 ? i18nT('apps.crewCompanion.time.now') : i18nT('apps.crewCompanion.time.in_min', { n: mins }) }
  }
  if (sameDay(at, now)) {
    const h = Math.floor(mins / 60)
    const m = mins % 60
    return {
      absLabel: clockLabel(at),
      relLabel: m ? i18nT('apps.crewCompanion.time.in_hm', { h, m }) : i18nT('apps.crewCompanion.time.in_h', { h }),
    }
  }
  const days = Math.round((at.getTime() - now.getTime()) / 86_400_000)
  return {
    absLabel: days <= 1
      ? clockLabel(at)
      : `${at.toLocaleDateString(activeLocale(), { weekday: 'short' })} ${clockLabel(at)}`,
    relLabel: days <= 1 ? i18nT('apps.crewCompanion.time.tomorrow') : i18nT('apps.crewCompanion.time.in_days', { n: days }),
  }
}

/**
 * Parse a user-typed interval, returning null when it is not a usable number.
 * Returning null rather than a fallback matters: a bad value must leave the current
 * setting alone instead of silently resetting it.
 */
export function clampBreakMins(raw: string | number): number | null {
  const n = typeof raw === 'number' ? raw : Number(String(raw).trim())
  if (!Number.isFinite(n) || n <= 0) return null
  return Math.min(BREAK_MAX_MINS, Math.max(BREAK_MIN_MINS, Math.round(n)))
}


/**
 * Break-interval choices offered as one-tap presets. Both the panel and the
 * dashboard app page render this same list, so the two surfaces cannot drift.
 */

/**
 * Break-interval presets, floor and ceiling — the panel's own controls and the
 * backend's clamp must agree, so the numbers live here rather than in the UI.
 */
export const BREAK_PRESETS = [30, 45, 60, 90]
