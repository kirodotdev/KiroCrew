import type { ReactNode } from 'react'
import { ShieldCheck, Moon, AlertTriangle, Sunrise, BookOpenCheck, Brain, Bug, DatabaseZap, FileWarning, FlaskConical, KeyRound, ListChecks, MessageSquareText, Rocket, ScrollText, Shield, Siren, Tag, Wrench } from 'lucide-react'

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
  /**
   * Suppress auto-delivery of the run transcript. Set on polling presets whose
   * prompts say "end silently" -- without it the saved job delivers
   * "_No response._" on every no-signal run, defeating the silence rule. Those
   * prompts deliver positive findings via send_message instead.
   */
  silent?: boolean
}

export interface SchedulePreset {
  id: string
  icon: ReactNode
  title: string
  description: string
  /**
   * NOTE: there is deliberately no stored `cadence` field. The label is
   * DERIVED from `prefill` by `formatCadence()` in `./scheduleCadence`, so the
   * clock and weekday follow the viewer's locale instead of freezing an en-US
   * rendering into every catalog. See that module for the reasoning.
   */
  /** Gallery section this preset belongs to. */
  category: PresetCategory
  /**
   * Featured presets surface on the Schedule page's empty state (the first
   * surface a new user sees). INVARIANT: never feature a preset with
   * `writes: true` -- a one-click unattended automation that pushes branches,
   * opens PRs, or edits issues does not belong in the highest-trust slot while
   * its guardrails are prompt text rather than an enforced deny rule. Write-
   * capable presets stay available in the gallery. A test pins this.
   */
  featured?: boolean
  /**
   * True when the preset's job writes to the user's repos or issue trackers.
   * Renders a "Writes to your repos" indicator on the card and an advisory
   * notice above the seeded create form.
   */
  writes?: boolean
  prefill: CronPrefill
}

/** Grouping for the template gallery. */
export type PresetCategory = 'hygiene' | 'quality' | 'security' | 'ops' | 'comms' | 'knowledge'

/**
 * Gallery section order and labels. The ids are stable data keys and are never
 * shown; the labels are localised through the same getter pattern the presets
 * use, so a language switch re-resolves them.
 */
export const PRESET_CATEGORIES: { id: PresetCategory; label: string }[] = [
  { id: 'quality', get label() { return i18nT('utils.schedulePresets.category_quality') } },
  { id: 'hygiene', get label() { return i18nT('utils.schedulePresets.category_hygiene') } },
  { id: 'security', get label() { return i18nT('utils.schedulePresets.category_security') } },
  { id: 'ops', get label() { return i18nT('utils.schedulePresets.category_ops') } },
  { id: 'comms', get label() { return i18nT('utils.schedulePresets.category_comms') } },
  { id: 'knowledge', get label() { return i18nT('utils.schedulePresets.category_knowledge') } },
]

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
    writes: true,
    category: 'hygiene',
    icon: <ShieldCheck size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.dependency_guardian_title') },
    get description() { return i18nT('utils.schedulePresets.dependency_guardian_description') },
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
    category: 'quality',
    featured: true,
    icon: <Moon size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.nightly_build_watch_title') },
    get description() { return i18nT('utils.schedulePresets.nightly_build_watch_description') },
    prefill: {
      name: 'Nightly Build Watch',
      get message() { return i18nT('utils.schedulePresets.nightly_build_watch_message') },
      schedMode: 'cron',
      cronExpr: '0 2 * * *',
    },
  },
  {
    id: 'error-digest',
    category: 'ops',
    featured: true,
    icon: <AlertTriangle size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.error_digest_title') },
    get description() { return i18nT('utils.schedulePresets.error_digest_description') },
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
    category: 'comms',
    featured: true,
    icon: <Sunrise size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.standup_brief_title') },
    get description() { return i18nT('utils.schedulePresets.standup_brief_description') },
    prefill: {
      name: 'Standup Brief',
      get message() { return i18nT('utils.schedulePresets.standup_brief_message') },
      schedMode: 'cron',
      cronExpr: '45 8 * * 1-5',
    },
  },
  {
    id: 'ci-failure-triage',
    writes: true,
    category: 'quality',
    icon: <Siren size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.ci_failure_triage_title') },
    get description() { return i18nT('utils.schedulePresets.ci_failure_triage_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.ci_failure_triage_title') },
      get message() { return i18nT('utils.schedulePresets.ci_failure_triage_message') },
      silent: true,
      schedMode: 'interval',
      intVal: 30,
      intUnit: 'minutes',
    },
  },
  {
    id: 'pr-review-followthrough',
    category: 'quality',
    icon: <MessageSquareText size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.pr_review_followthrough_title') },
    get description() { return i18nT('utils.schedulePresets.pr_review_followthrough_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.pr_review_followthrough_title') },
      get message() { return i18nT('utils.schedulePresets.pr_review_followthrough_message') },
      silent: true,
      schedMode: 'interval',
      intVal: 30,
      intUnit: 'minutes',
    },
  },
  {
    id: 'bug-intake-repro',
    writes: true,
    category: 'quality',
    icon: <Bug size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.bug_intake_repro_title') },
    get description() { return i18nT('utils.schedulePresets.bug_intake_repro_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.bug_intake_repro_title') },
      get message() { return i18nT('utils.schedulePresets.bug_intake_repro_message') },
      silent: true,
      schedMode: 'interval',
      intVal: 30,
      intUnit: 'minutes',
    },
  },
  {
    id: 'deploy-verification',
    category: 'ops',
    featured: true,
    icon: <Rocket size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.deploy_verification_title') },
    get description() { return i18nT('utils.schedulePresets.deploy_verification_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.deploy_verification_title') },
      get message() { return i18nT('utils.schedulePresets.deploy_verification_message') },
      silent: true,
      schedMode: 'interval',
      intVal: 30,
      intUnit: 'minutes',
    },
  },
  {
    id: 'stale-issue-triage',
    writes: true,
    category: 'hygiene',
    icon: <Tag size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.stale_issue_triage_title') },
    get description() { return i18nT('utils.schedulePresets.stale_issue_triage_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.stale_issue_triage_title') },
      get message() { return i18nT('utils.schedulePresets.stale_issue_triage_message') },
      silent: true,
      schedMode: 'cron',
      cronExpr: '0 8 * * *',
    },
  },
  {
    id: 'docs-drift',
    writes: true,
    category: 'hygiene',
    icon: <BookOpenCheck size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.docs_drift_title') },
    get description() { return i18nT('utils.schedulePresets.docs_drift_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.docs_drift_title') },
      get message() { return i18nT('utils.schedulePresets.docs_drift_message') },
      silent: true,
      schedMode: 'weekly',
      weekDays: [2],
      weekTime: '10:00',
    },
  },
  {
    id: 'weekly-changelog',
    writes: true,
    category: 'hygiene',
    icon: <ScrollText size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.weekly_changelog_title') },
    get description() { return i18nT('utils.schedulePresets.weekly_changelog_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.weekly_changelog_title') },
      get message() { return i18nT('utils.schedulePresets.weekly_changelog_message') },
      silent: true,
      schedMode: 'weekly',
      weekDays: [5],
      weekTime: '15:00',
    },
  },
  {
    id: 'merged-pr-checklist-review',
    category: 'quality',
    icon: <ListChecks size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.merged_pr_checklist_review_title') },
    get description() { return i18nT('utils.schedulePresets.merged_pr_checklist_review_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.merged_pr_checklist_review_title') },
      get message() { return i18nT('utils.schedulePresets.merged_pr_checklist_review_message') },
      silent: true,
      schedMode: 'cron',
      cronExpr: '30 8 * * *',
    },
  },
  {
    id: 'test-backfill-coverage',
    writes: true,
    category: 'quality',
    icon: <FlaskConical size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.test_backfill_coverage_title') },
    get description() { return i18nT('utils.schedulePresets.test_backfill_coverage_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.test_backfill_coverage_title') },
      get message() { return i18nT('utils.schedulePresets.test_backfill_coverage_message') },
      silent: true,
      schedMode: 'weekly',
      weekDays: [3],
      weekTime: '09:00',
    },
  },
  {
    id: 'lint-typecheck-regression',
    writes: true,
    category: 'quality',
    icon: <Wrench size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.lint_typecheck_regression_title') },
    get description() { return i18nT('utils.schedulePresets.lint_typecheck_regression_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.lint_typecheck_regression_title') },
      get message() { return i18nT('utils.schedulePresets.lint_typecheck_regression_message') },
      silent: true,
      schedMode: 'cron',
      cronExpr: '0 7 * * *',
    },
  },
  {
    id: 'weekly-vuln-scan',
    writes: true,
    category: 'security',
    icon: <Shield size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.weekly_vuln_scan_title') },
    get description() { return i18nT('utils.schedulePresets.weekly_vuln_scan_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.weekly_vuln_scan_title') },
      get message() { return i18nT('utils.schedulePresets.weekly_vuln_scan_message') },
      schedMode: 'weekly',
      weekDays: [1],
      weekTime: '08:00',
    },
  },
  {
    id: 'secret-scan',
    category: 'security',
    icon: <KeyRound size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.secret_scan_title') },
    get description() { return i18nT('utils.schedulePresets.secret_scan_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.secret_scan_title') },
      get message() { return i18nT('utils.schedulePresets.secret_scan_message') },
      silent: true,
      schedMode: 'cron',
      cronExpr: '0 6 * * *',
    },
  },
  {
    id: 'workflow-failure-autofile',
    writes: true,
    category: 'comms',
    icon: <FileWarning size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.workflow_failure_autofile_title') },
    get description() { return i18nT('utils.schedulePresets.workflow_failure_autofile_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.workflow_failure_autofile_title') },
      get message() { return i18nT('utils.schedulePresets.workflow_failure_autofile_message') },
      silent: true,
      schedMode: 'interval',
      intVal: 30,
      intUnit: 'minutes',
    },
  },
  {
    id: 'docs-reindex',
    category: 'knowledge',
    icon: <DatabaseZap size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.docs_reindex_title') },
    get description() { return i18nT('utils.schedulePresets.docs_reindex_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.docs_reindex_title') },
      get message() { return i18nT('utils.schedulePresets.docs_reindex_message') },
      silent: true,
      schedMode: 'cron',
      cronExpr: '0 3 * * *',
    },
  },
  {
    id: 'session-summary',
    category: 'knowledge',
    icon: <Brain size={ICON_SIZE} />,
    get title() { return i18nT('utils.schedulePresets.session_summary_title') },
    get description() { return i18nT('utils.schedulePresets.session_summary_description') },
    prefill: {
      get name() { return i18nT('utils.schedulePresets.session_summary_title') },
      get message() { return i18nT('utils.schedulePresets.session_summary_message') },
      silent: true,
      schedMode: 'cron',
      cronExpr: '30 17 * * 1-5',
    },
  },
]
