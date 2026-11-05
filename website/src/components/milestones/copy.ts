/**
 * The milestone text, as a fixed map from the server's row id to i18n keys.
 *
 * The server sends ids only (RFC "Milestones for New-User Feature Discovery",
 * 4.1): every word a person sees, including the starter prompt that lands in
 * the composer, comes from this map and the i18n catalog. A row whose id is not
 * here is not drawn at all, so a newer gateway cannot put text on screen that
 * this bundle never reviewed. The keys are literals, not assembled, so the
 * i18n key gate can check every one of them.
 */
import { i18nT } from '../../i18n/t'
import { crewModeEnabled } from '../../lib/crewMode'

export interface FeatureCopy {
  /** Past tense, for what was reached: notices. */
  title: string
  /** Verb first, for a row still to try: Try today. */
  action: string
  blurb: string
  starterPrompt: string
}

export const FEATURE_COPY: Readonly<Record<string, FeatureCopy>> = {
  widgets: {
    title: 'milestone.widgets.title',
    action: 'milestone.widgets.action',
    blurb: 'milestone.widgets.blurb',
    starterPrompt: 'milestone.widgets.starter_prompt',
  },
  'scheduled-task': {
    title: 'milestone.scheduled_task.title',
    action: 'milestone.scheduled_task.action',
    blurb: 'milestone.scheduled_task.blurb',
    starterPrompt: 'milestone.scheduled_task.starter_prompt',
  },
  mate: {
    title: 'milestone.mate.title',
    action: 'milestone.mate.action',
    blurb: 'milestone.mate.blurb',
    starterPrompt: 'milestone.mate.starter_prompt',
  },
  'parallel-chats': {
    title: 'milestone.parallel_chats.title',
    action: 'milestone.parallel_chats.action',
    blurb: 'milestone.parallel_chats.blurb',
    starterPrompt: 'milestone.parallel_chats.starter_prompt',
  },
  slack: {
    title: 'milestone.slack.title',
    action: 'milestone.slack.action',
    blurb: 'milestone.slack.blurb',
    starterPrompt: 'milestone.slack.starter_prompt',
  },
  'pr-watch': {
    title: 'milestone.pr_watch.title',
    action: 'milestone.pr_watch.action',
    blurb: 'milestone.pr_watch.blurb',
    starterPrompt: 'milestone.pr_watch.starter_prompt',
  },
  'crew-mode': {
    title: 'milestone.crew_mode.title',
    action: 'milestone.crew_mode.action',
    blurb: 'milestone.crew_mode.blurb',
    starterPrompt: 'milestone.crew_mode.starter_prompt',
  },
}

export interface UsageCopy {
  title: string
  /** Takes `count`: the tier's threshold ("100 chats", "7-day streak"). */
  tier: string
}

export const USAGE_COPY: Readonly<Record<string, UsageCopy>> = {
  'chats-opened': { title: 'milestone.chats_opened.title', tier: 'milestone.chats_opened.tier' },
  'streak-best': { title: 'milestone.streak_best.title', tier: 'milestone.streak_best.tier' },
}

/**
 * "Getting started", in order: a first finished result (the widgets row) and a
 * morning brief (the scheduled-task row, whose starter prefills a daily
 * brief). Their own titles; the blurb and starter stay the row's.
 */
export const GETTING_STARTED_IDS: readonly string[] = ['widgets', 'scheduled-task']

export const GETTING_STARTED_TITLE: Readonly<Record<string, string>> = {
  widgets: 'milestones.getting_started_result',
  'scheduled-task': 'milestones.getting_started_brief',
}

/** `regular`: a quiet line icon. `advanced`: the same icon, colored. */
export type BadgeKind = 'regular' | 'advanced'

/**
 * Which reached tiers put a badge on the wall, and how it looks. The top tiers
 * (1000 chats, a 100-day streak) get their own style in Ship 2, so they have
 * no badge here yet; their progress bar still runs to them.
 */
export const BADGE_TIERS: Readonly<Record<string, Readonly<Record<number, BadgeKind>>>> = {
  'chats-opened': { 10: 'regular', 100: 'advanced' },
  'streak-best': { 3: 'regular', 7: 'regular', 30: 'advanced' },
}

/**
 * The only rows that may send their starter prompt without the user pressing
 * send (RFC 4.5, D1). The server marks them `send: "send"` too; this list is
 * the client's own copy of the rule, so a server that marks any other row
 * `send` still only fills the composer.
 */
export const DIRECT_SEND_ROWS: ReadonlySet<string> = new Set(['widgets', 'parallel-chats'])

/**
 * The rows "Try today" may pick (RFC 8.3): ones a person can finish in a few
 * minutes. `pr-watch` (needs a PR link and waits on CI) and `crew-mode` (spend
 * has no upper bound) stay in the full list but are never picked.
 */
export const QUICK_ROWS: ReadonlySet<string> = new Set(['widgets', 'parallel-chats', 'mate', 'scheduled-task', 'slack'])

/**
 * One notice id as a line of text: a feature row (`widgets`) or a tier
 * (`chats-opened:10`). `null` for an id this bundle has no text for.
 */
export function noticeTitle(id: string): string | null {
  const feature = FEATURE_COPY[id]
  // The Crew Mode row is not announced while the Crew Mode preview is off.
  if (feature) return id === 'crew-mode' && !crewModeEnabled() ? null : i18nT(feature.title)
  const sep = id.lastIndexOf(':')
  if (sep <= 0) return null
  const usage = USAGE_COPY[id.slice(0, sep)]
  const count = Number(id.slice(sep + 1))
  if (!usage || !Number.isInteger(count) || count <= 0) return null
  return i18nT(usage.tier, { count })
}

/** The row id a notice id belongs to (`chats-opened:10` -> `chats-opened`). */
export function noticeRow(id: string): string {
  const sep = id.lastIndexOf(':')
  return sep > 0 ? id.slice(0, sep) : id
}
