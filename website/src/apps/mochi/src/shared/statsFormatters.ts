import { i18next } from '../../../../i18n'
import { i18nT } from '../../../../i18n/t'

/**
 * The active UI locale as a BCP-47 tag.
 *
 * Read at call time rather than passed in: these helpers used to take a `lang`
 * argument threaded down from a component, which existed only to feed a
 * `lang === 'zh' ? … : …` ternary — so every duration and date in this file knew
 * exactly two languages and silently fell back to English for the other eight.
 */
function locale(): string {
  return i18next.language || 'en'
}

/**
 * A count with its unit, localized.
 *
 * `Intl` rather than catalog keys on purpose: unit names, their placement, and
 * whether a space precedes them are locale data, not copy. Hard-coding one
 * language's unit against English gets those two right and the other eight
 * wrong, and
 * adding `unit_second`-style keys per locale would re-derive, by hand, a table
 * the platform already ships.
 */
function unit(value: number, name: 'second' | 'minute' | 'hour'): string {
  return new Intl.NumberFormat(locale(), {
    style: 'unit',
    unit: name,
    unitDisplay: 'narrow',
  }).format(value)
}

/** Format thinking time as a human-readable string (floor-rounded) */
export function formatThinkingTime(seconds: number): string {
  let time: string
  if (seconds < 60) {
    time = unit(seconds, 'second')
  } else if (seconds < 3600) {
    time = unit(Math.floor(seconds / 60), 'minute')
  } else {
    const hours = Math.floor(seconds / 3600)
    const mins = Math.floor((seconds % 3600) / 60)
    time = mins > 0 ? `${unit(hours, 'hour')} ${unit(mins, 'minute')}` : unit(hours, 'hour')
  }
  return i18nT('apps.mochi.stats.thinking_time', { time })
}

/** Get top N moods sorted by count with percentages */
export function getTopMoods(moods: Record<string, number>, limit = 3): Array<{ mood: string; count: number; percent: number }> {
  const entries = Object.entries(moods).filter(([, c]) => c > 0)
  if (entries.length === 0) return []
  const total = entries.reduce((sum, [, c]) => sum + c, 0)
  return entries
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([mood, count]) => ({
      mood,
      count,
      percent: Math.round((count / total) * 100),
    }))
}

/** Calculate companion days from firstLaunch to today (calendar days) */
export function calcCompanionDays(firstLaunch: string): number {
  const start = new Date(firstLaunch)
  if (isNaN(start.getTime())) return 0
  const now = new Date()
  const diffMs = now.getTime() - start.getTime()
  return Math.max(0, Math.floor(diffMs / (1000 * 60 * 60 * 24))) + 1 // +1 because day 1 is the first day
}

/** Format companionSeconds as a human-readable duration */
export function formatCompanionTime(seconds: number): string {
  if (seconds < 3600) return unit(Math.max(1, Math.floor(seconds / 60)), 'minute')
  const hours = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  if (mins === 0) return unit(hours, 'hour')
  return `${unit(hours, 'hour')} ${unit(mins, 'minute')}`
}

/** Check if a stat value should be displayed (non-zero, non-empty) */
export function shouldShowStat(value: number | string): boolean {
  if (typeof value === 'number') return value > 0
  return value !== ''
}

/** Format a YYYY-MM-DD date string for display (localized) */
export function formatDate(dateStr: string): string {
  if (!dateStr) return ''
  const parts = dateStr.split('-')
  if (parts.length !== 3) return dateStr
  const [year, month, day] = parts.map(p => parseInt(p, 10))
  if ([year, month, day].some(Number.isNaN)) return dateStr
  // Month/day only, and ordered by the locale rather than by us: `${m}/${d}` is
  // wrong in every locale that puts the day first, which the previous
  // Chinese-or-slash branch shipped to all eight other languages.
  return new Intl.DateTimeFormat(locale(), { month: 'numeric', day: 'numeric' })
    .format(new Date(year, month - 1, day))
}
