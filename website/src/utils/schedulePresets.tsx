import type { ReactNode } from 'react'
import { ShieldCheck, Moon, AlertTriangle, Sunrise } from 'lucide-react'

/**
 * Prefill payload for the "New Job" creation flow. Field names mirror
 * JobForm's internal schedule state so the form can seed itself directly.
 * weekDays use the grid convention (Mon=1 … Sun=7), matching JobForm's
 * DAY_NAMES / toggleDay ordering.
 */
export interface CronPrefill {
  name: string
  message: string
  schedMode: 'interval' | 'weekly' | 'cron'
  intVal?: number
  intUnit?: 'minutes' | 'hours' | 'days'
  weekDays?: number[]
  weekTime?: string
  cronExpr?: string
}

export interface SchedulePreset {
  id: string
  icon: ReactNode
  title: string
  description: string
  /** Human-readable cadence shown on the card (mirrors the schedule prefill). */
  cadence: string
  prefill: CronPrefill
}

const ICON_SIZE = 22

/**
 * Four pre-canned schedules surfaced on the empty Schedule page. Clicking a
 * card opens the standard create flow with the prompt + schedule pre-filled;
 * the user reviews and saves like any other job.
 */
export const SCHEDULE_PRESETS: SchedulePreset[] = [
  {
    id: 'dependency-guardian',
    icon: <ShieldCheck size={ICON_SIZE} />,
    title: 'Dependency Guardian',
    description: 'Upgrades packages, runs tests, and opens a PR only when green.',
    cadence: 'Weekly · Mondays 6:00am',
    prefill: {
      name: 'Dependency Guardian',
      message:
        'Check this project for outdated dependencies. Upgrade them, run the full test suite, and fix anything that breaks. Only open a pull request if all tests pass green — otherwise report what failed.',
      schedMode: 'weekly',
      weekDays: [1],
      weekTime: '06:00',
    },
  },
  {
    id: 'nightly-build-watch',
    icon: <Moon size={ICON_SIZE} />,
    title: 'Nightly Build Watch',
    description: 'Builds and tests main overnight; reports failures and likely fixes.',
    cadence: 'Every 24 hours · 2:00am',
    prefill: {
      name: 'Nightly Build Watch',
      message:
        'Build and test the main branch. If anything fails, report exactly what failed, the likely root cause, and a suggested fix.',
      schedMode: 'cron',
      cronExpr: '0 2 * * *',
    },
  },
  {
    id: 'error-digest',
    icon: <AlertTriangle size={ICON_SIZE} />,
    title: 'Error Digest',
    description: 'Clusters new production errors with a suspected cause for each.',
    cadence: 'Every 6 hours',
    prefill: {
      name: 'Error Digest',
      message:
        'Review new production errors since the last run. Cluster them by type, and for each cluster give a short summary and a suspected cause.',
      schedMode: 'interval',
      intVal: 6,
      intUnit: 'hours',
    },
  },
  {
    id: 'standup-brief',
    icon: <Sunrise size={ICON_SIZE} />,
    title: 'Standup Brief',
    description: 'Your commits, PRs, CI status, and blockers before standup.',
    cadence: 'Every weekday · 8:45am',
    prefill: {
      name: 'Standup Brief',
      message:
        "Summarize my recent commits, open pull requests, CI status, and any blockers for today's standup. Keep it concise and deliver it before the meeting.",
      schedMode: 'cron',
      cronExpr: '45 8 * * 1-5',
    },
  },
]
