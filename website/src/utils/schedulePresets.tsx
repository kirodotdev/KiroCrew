import type { ReactNode } from 'react'
import { ShieldCheck, Moon, AlertTriangle, Sunrise } from 'lucide-react'

import { i18nT } from '../i18n/t'

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
  /**
   * Human-readable cadence shown on the card (mirrors the schedule prefill).
   *
   * NOT localised, deliberately. Three of the four values embed a clock time
   * (`6:00am`) or a weekday (`Mondays`), and baking either into a catalog string
   * would freeze an en-US time format into every locale — `6:00am` does not
   * become `06:00` in de-DE just because a translator retyped it. The real fix
   * is to DERIVE this field from `prefill` through the locale-formatting seam
   * (`src/i18n/format.ts` — `fmtTimeNumeric` for the clock, `fmtWeekday` for the
   * day name), which no cadence formatter exists for yet. Left English as one
   * coherent group rather than localising only `Every 6 hours`: a single
   * translated card among three English ones reads worse than four consistent
   * ones, and any key added now is orphaned the moment the formatter lands.
   */
  cadence: string
  prefill: CronPrefill
}

const ICON_SIZE = 22

/**
 * Four pre-canned schedules surfaced on the empty Schedule page. Clicking a
 * card opens the standard create flow with the prompt + schedule pre-filled;
 * the user reviews and saves like any other job.
 *
 * `title` / `description` / `prefill.message` are GETTERS, not values. This
 * table is evaluated once at module load, so a plain `i18nT()` call in it would
 * freeze whatever language was active at boot and never re-resolve on a language
 * switch. A getter moves the lookup to property ACCESS, which happens while
 * `SchedulePage` renders the cards and while `JobForm` seeds its state — both
 * per render. The `i18nT()` argument is a bare literal so
 * `scripts/check-i18n-keys.mjs` can still verify statically that the key exists.
 *
 * `prefill.name` stays English on purpose: it is written into the cron registry
 * as the job's stored identity, not chrome, and `eslint.i18n.config.js` exempts
 * the `name` property for exactly that reason.
 */
export const SCHEDULE_PRESETS: SchedulePreset[] = [
  {
    id: 'dependency-guardian',
    icon: <ShieldCheck size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.dependency_guardian_title') },
    get description() { return i18nT('utils.schedulePresets.dependency_guardian_description') },
    cadence: 'Weekly · Mondays 6:00am',
    prefill: {
      name: 'Dependency Guardian',
      get message() { return i18nT('utils.schedulePresets.dependency_guardian_message') },
      schedMode: 'weekly',
      weekDays: [1],
      weekTime: '06:00',
    },
  },
  {
    id: 'nightly-build-watch',
    icon: <Moon size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.nightly_build_watch_title') },
    get description() { return i18nT('utils.schedulePresets.nightly_build_watch_description') },
    cadence: 'Every 24 hours · 2:00am',
    prefill: {
      name: 'Nightly Build Watch',
      get message() { return i18nT('utils.schedulePresets.nightly_build_watch_message') },
      schedMode: 'cron',
      cronExpr: '0 2 * * *',
    },
  },
  {
    id: 'error-digest',
    icon: <AlertTriangle size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.error_digest_title') },
    get description() { return i18nT('utils.schedulePresets.error_digest_description') },
    cadence: 'Every 6 hours',
    prefill: {
      name: 'Error Digest',
      get message() { return i18nT('utils.schedulePresets.error_digest_message') },
      schedMode: 'interval',
      intVal: 6,
      intUnit: 'hours',
    },
  },
  {
    id: 'standup-brief',
    icon: <Sunrise size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.standup_brief_title') },
    get description() { return i18nT('utils.schedulePresets.standup_brief_description') },
    cadence: 'Every weekday · 8:45am',
    prefill: {
      name: 'Standup Brief',
      get message() { return i18nT('utils.schedulePresets.standup_brief_message') },
      schedMode: 'cron',
      cronExpr: '45 8 * * 1-5',
    },
  },
]
