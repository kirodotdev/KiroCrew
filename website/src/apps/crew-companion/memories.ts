/**
 * The pet's "memories" as display rows.
 *
 * `shouldShowStat` is what keeps a fresh install from rendering a wall of zeroes —
 * a stat with nothing in it is omitted rather than shown as 0. Wording comes from
 * the dashboard i18n catalog.
 */

import { i18next } from '../../i18n'
import { i18nT } from '../../i18n/t'
import type { CompanionStats, MemoryRow } from './types'
import { Bell, Clock, Moon, Sun, Wind } from 'lucide-react'


/** Calendar days from firstLaunch to today (day 1 is the first day). */
export function calcCompanionDays(firstLaunch: string): number {
  const start = new Date(firstLaunch)
  if (isNaN(start.getTime())) return 0
  const diffMs = Date.now() - start.getTime()
  return Math.max(0, Math.floor(diffMs / 86_400_000)) + 1
}

/** Whether a stat value carries anything worth showing. */
export function shouldShowStat(value: number | string): boolean {
  if (typeof value === 'number') return value > 0
  return value !== ''
}

/** Format cumulative companion seconds as a human duration, localized. */
export function formatCompanionTime(seconds: number): string {
  if (seconds < 3600) {
    const mins = Math.max(1, Math.floor(seconds / 60))
    return i18nT('apps.crewCompanion.memories.dur_min', { n: mins })
  }
  const hours = Math.floor(seconds / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  return mins === 0
    ? i18nT('apps.crewCompanion.memories.dur_hours', { h: hours })
    : i18nT('apps.crewCompanion.memories.dur_hm', { h: hours, m: mins })
}

/** Build the read-only Memories rows from the companion stats. */
export function memoryRows(stats: CompanionStats, petName: string): MemoryRow[] {
  const rows: MemoryRow[] = []
  const n = (v: number) => v.toLocaleString(i18next.language || undefined)
  // NOTE: never pass n() as i18next's `count`. Plural selection needs a NUMBER;
  // a formatted string makes resolution fail and renders the raw key on screen.

  if (shouldShowStat(stats.companionSeconds)) {
    let text = i18nT('apps.crewCompanion.memories.row_time', { time: formatCompanionTime(stats.companionSeconds) })
    if (shouldShowStat(stats.streak)) {
      // A separator, not a space: "for 63 h 18 min 3-day streak" read as one
      // run-on fact.
      text += ' \u00b7 ' + i18nT('apps.crewCompanion.memories.row_streak', { streak: n(stats.streak) })
    }
    rows.push({ icon: Clock, text })
  }
  if (shouldShowStat(stats.breathingSessions)) {
    rows.push({
      icon: Wind,
      text: i18nT('apps.crewCompanion.memories.row_breathing', { count: stats.breathingSessions, name: petName }),
    })
  }
  if (shouldShowStat(stats.remindersCreated)) {
    rows.push({ icon: Bell, text: i18nT('apps.crewCompanion.memories.row_reminders', { count: stats.remindersCreated }) })
  }
  if (shouldShowStat(stats.latestActiveTime)) {
    rows.push({ icon: Moon, text: i18nT('apps.crewCompanion.memories.row_latest', { time: stats.latestActiveTime }) })
  }
  if (shouldShowStat(stats.earliestActiveTime)) {
    rows.push({ icon: Sun, text: i18nT('apps.crewCompanion.memories.row_earliest', { time: stats.earliestActiveTime }) })
  }
  return rows
}
