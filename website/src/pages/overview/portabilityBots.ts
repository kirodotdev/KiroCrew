/**
 * What an instance import reports about each Bot, in the reader's words.
 *
 * The import summary carries one row per imported or skipped Bot (`bots`, at
 * most 200, `bots_more` for the rest), the member-thread chats it withheld
 * (`bot_transcripts_orphaned`, `sessions_member_withheld`) and the counts the
 * archive's manifest could not list. Statuses, reasons and notices are the
 * closed vocabulary of `src/kiro_crew/portability_bot_types.py`; a value outside
 * it is never rendered, so archive text cannot reach the page as a key.
 */
import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'

/** The parts of one Bot an import restores, in the order the summary lists them. */
export const BOT_PARTS = ['roster', 'binding', 'crewmate_file', 'dashboard', 'briefing'] as const
export type BotPart = (typeof BOT_PARTS)[number]

/** Statuses that leave a part whole. Any other status, an unknown one included, is reported as missing. */
export const WHOLE_STATUSES: ReadonlySet<string> = new Set([
  'added', 'imported', 'rebuilt', 'written', 'restored', 'kept', 'not_needed', 'none',
])

/** The catalog key that names each part in a "missing" line. */
const PART_KEYS: Record<BotPart, string> = {
  roster: 'pages.overview.portabilityTab.bot_part_roster',
  binding: 'pages.overview.portabilityTab.bot_part_binding',
  crewmate_file: 'pages.overview.portabilityTab.bot_part_crewmate_file',
  dashboard: 'pages.overview.portabilityTab.bot_part_dashboard',
  briefing: 'pages.overview.portabilityTab.bot_part_briefing',
}

/** A dashboard that installed but whose page could not be rebuilt is named as its page. */
const DASHBOARD_PAGE_KEY = 'pages.overview.portabilityTab.bot_part_dashboard_page'

/** The catalog key that says each reason in words. */
export const BOT_REASON_KEYS = {
  name_taken: 'pages.overview.portabilityTab.bot_reason_name_taken',
  slug_taken: 'pages.overview.portabilityTab.bot_reason_slug_taken',
  member_id_taken: 'pages.overview.portabilityTab.bot_reason_member_id_taken',
  store_taken: 'pages.overview.portabilityTab.bot_reason_store_taken',
  store_not_carried: 'pages.overview.portabilityTab.bot_reason_store_not_carried',
  store_not_owned: 'pages.overview.portabilityTab.bot_reason_store_not_owned',
  workspace_taken: 'pages.overview.portabilityTab.bot_reason_workspace_taken',
  config_rejected: 'pages.overview.portabilityTab.bot_reason_config_rejected',
  no_member_id: 'pages.overview.portabilityTab.bot_reason_no_member_id',
  member_id_ambiguous: 'pages.overview.portabilityTab.bot_reason_member_id_ambiguous',
  slug_collision: 'pages.overview.portabilityTab.bot_reason_slug_collision',
  binding_exists_unusable: 'pages.overview.portabilityTab.bot_reason_binding_exists_unusable',
  archive_binding_disagrees: 'pages.overview.portabilityTab.bot_reason_archive_binding_disagrees',
  transcript_predates_import: 'pages.overview.portabilityTab.bot_reason_transcript_predates_import',
  write_failed: 'pages.overview.portabilityTab.bot_reason_write_failed',
  unsafe_name: 'pages.overview.portabilityTab.bot_reason_unsafe_name',
  user_agent_named: 'pages.overview.portabilityTab.bot_reason_user_agent_named',
  foreign_owner: 'pages.overview.portabilityTab.bot_reason_foreign_owner',
  unreadable_record: 'pages.overview.portabilityTab.bot_reason_unreadable_record',
  artifact_missing: 'pages.overview.portabilityTab.bot_reason_artifact_missing',
  artifact_rejected: 'pages.overview.portabilityTab.bot_reason_artifact_rejected',
  digest_mismatch: 'pages.overview.portabilityTab.bot_reason_digest_mismatch',
  held_by_other_bot: 'pages.overview.portabilityTab.bot_reason_held_by_other_bot',
  another_package_of_this_bot: 'pages.overview.portabilityTab.bot_reason_another_package_of_this_bot',
} as const
export type BotReason = keyof typeof BOT_REASON_KEYS

/** The catalog key of each notice line; each takes the Bot's name. */
export const BOT_NOTICE_KEYS = {
  starting_abilities_only: 'pages.overview.portabilityTab.bot_notice_starting_abilities_only',
  rules_not_carried: 'pages.overview.portabilityTab.bot_notice_rules_not_carried',
  goals_not_carried: 'pages.overview.portabilityTab.bot_notice_goals_not_carried',
} as const
export type BotNotice = keyof typeof BOT_NOTICE_KEYS

/** One row of the summary's `bots`, with everything outside the vocabulary dropped. */
export interface BotRow {
  bot: string
  slug: string
  /** The status of each part the row names. */
  parts: Partial<Record<BotPart, string>>
  /** Every reason of the row, merged: what an older backend's row carries alone. */
  reasons: BotReason[]
  /**
   * The reason of each part's status (`part_reasons`), or null for a row from a
   * backend that sends none; a part with no known reason is absent.
   */
  partReasons: Partial<Record<BotPart, BotReason>> | null
  /** Whether the installed Bot list holds the Bot (`in_roster`), or null when not sent. */
  inRoster: boolean | null
  notices: BotNotice[]
}

/** One member-thread chat the import withheld, from `bot_transcripts_orphaned`. */
export interface OrphanChat {
  slug: string
  /** Names of Bots on this install whose short name the chat carries. */
  candidates: string[]
}

const isReason = (v: unknown): v is BotReason => typeof v === 'string' && Object.hasOwn(BOT_REASON_KEYS, v)
const isNotice = (v: unknown): v is BotNotice => typeof v === 'string' && Object.hasOwn(BOT_NOTICE_KEYS, v)

/** Known values only, repeats dropped, in first-seen order. */
function distinct<T extends string>(values: unknown[], keep: (v: unknown) => v is T): T[] {
  return [...new Set(values.filter(keep))]
}

/**
 * The row's reasons: a list of reason codes, or a map of part to reason code.
 * Either shape reads the same; a code outside the vocabulary is dropped.
 */
function rowReasons(raw: unknown): BotReason[] {
  if (Array.isArray(raw)) return distinct(raw, isReason)
  if (raw && typeof raw === 'object') return distinct(Object.values(raw), isReason)
  return isReason(raw) ? [raw] : []
}

/** The row's `part_reasons` map, known parts and reasons only; null when it is not a map. */
function partReasonMap(raw: unknown): Partial<Record<BotPart, BotReason>> | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const map = raw as Record<string, unknown>
  const out: Partial<Record<BotPart, BotReason>> = {}
  for (const part of BOT_PARTS) {
    const reason = map[part]
    if (isReason(reason)) out[part] = reason
  }
  return out
}

/** The summary's `bots` rows; a row without a Bot name is dropped. */
export function botRows(raw: unknown): BotRow[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap(row => {
    if (!row || typeof row !== 'object') return []
    const r = row as Record<string, unknown>
    if (typeof r.bot !== 'string' || !r.bot) return []
    const parts: Partial<Record<BotPart, string>> = {}
    for (const part of BOT_PARTS) {
      const status = r[part]
      if (typeof status === 'string' && status) parts[part] = status
    }
    return [{
      bot: r.bot,
      slug: typeof r.slug === 'string' ? r.slug : '',
      parts,
      reasons: rowReasons(r.reasons),
      partReasons: partReasonMap(r.part_reasons),
      inRoster: typeof r.in_roster === 'boolean' ? r.in_roster : null,
      notices: Array.isArray(r.notices) ? distinct(r.notices, isNotice) : [],
    }]
  })
}

/** The parts of a row whose status is not a whole one. */
export function missingParts(row: BotRow): BotPart[] {
  return BOT_PARTS.filter(part => {
    const status = row.parts[part]
    return status !== undefined && !WHOLE_STATUSES.has(status)
  })
}

/** A Bot is whole when no part it names has a not-whole status. */
export function isWholeBot(row: BotRow): boolean {
  return missingParts(row).length === 0
}

/** The summary's `bot_transcripts_orphaned` rows; a row without a slug is dropped. */
export function orphanChats(raw: unknown): OrphanChat[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap(row => {
    if (!row || typeof row !== 'object') return []
    const { slug, candidates } = row as { slug?: unknown; candidates?: unknown }
    if (typeof slug !== 'string' || !slug) return []
    const names = Array.isArray(candidates)
      ? [...new Set(candidates.filter((n): n is string => typeof n === 'string' && n !== ''))]
      : []
    return [{ slug, candidates: names }]
  })
}

/** A non-negative integer from the summary, or 0. */
function count(raw: unknown): number {
  return typeof raw === 'number' && Number.isInteger(raw) && raw > 0 ? raw : 0
}

/** An exception class name, the only detail `bots_failed` carries. */
const ERROR_CLASS = /^[A-Za-z_][A-Za-z0-9_.]{0,99}$/

/**
 * The summary's `bots_failed`: null when the Bot restore did not fail as a whole,
 * the exception class name when it did, or '' when it failed with a value that is
 * not a class name (shown without detail rather than as arbitrary text).
 */
function restoreFailure(raw: unknown): string | null {
  if (raw === undefined || raw === null || raw === false) return null
  return typeof raw === 'string' && ERROR_CLASS.test(raw) ? raw : ''
}

/**
 * Roster reasons that mean the Bot was not added to this install, for a row not known
 * to be in the Bot list (`in_roster` false or not sent). Any other roster reason
 * (`store_not_owned`, `unreadable_record`) then reads as "could not be set up".
 */
const NOT_ADDED_REASONS: ReadonlySet<BotReason> = new Set<BotReason>([
  'name_taken', 'slug_taken', 'member_id_taken', 'store_taken', 'store_not_carried', 'workspace_taken',
  'config_rejected', 'foreign_owner', 'held_by_other_bot', 'user_agent_named', 'write_failed',
  'no_member_id',
])

/**
 * Agent-file reasons that leave a file this import did not write in place: another
 * Bot's (`held_by_other_bot`) or another data home's (`foreign_owner`).
 */
const AGENT_FILE_IN_PLACE_REASONS: ReadonlySet<BotReason> = new Set<BotReason>([
  'held_by_other_bot', 'foreign_owner',
])

/**
 * Agent-file reasons with which no file was in place, for a row without
 * `part_reasons`: a merged `held_by_other_bot` next to one of them is another part's.
 */
const NO_AGENT_FILE_REASONS: ReadonlySet<BotReason> = new Set<BotReason>([
  'unsafe_name', 'user_agent_named', 'write_failed',
])

/**
 * Whether the Bot runs on an agent file this import did not write: its crewmate
 * file was `not_written` because one another Bot or another data home owns was in
 * place, so the Bot now runs with whatever that file allows. Read off the agent
 * file's own reason; a row without `part_reasons` (an older backend) is read off its
 * merged reasons, where `foreign_owner` is only ever the agent file's (the roster
 * case is excluded first) and `held_by_other_bot` counts only when no reason says
 * the agent file had none in place.
 */
export function runsOnHeldAgentFile(row: BotRow): boolean {
  if (row.parts.crewmate_file !== 'not_written' || missingParts(row).includes('roster')) return false
  if (row.partReasons) {
    const reason = row.partReasons.crewmate_file
    return reason !== undefined && AGENT_FILE_IN_PLACE_REASONS.has(reason)
  }
  if (row.reasons.includes('foreign_owner')) return true
  return row.reasons.includes('held_by_other_bot')
    && !row.reasons.some(reason => NO_AGENT_FILE_REASONS.has(reason))
}

function reasonText(reasons: BotReason[]): string {
  return fmtList(reasons.map(reason => i18nT(BOT_REASON_KEYS[reason])))
}

/** The reasons of *parts*, in part order and without repeats; the merged list without `part_reasons`. */
function reasonsOf(row: BotRow, parts: BotPart[]): BotReason[] {
  if (!row.partReasons) return row.reasons
  const map = row.partReasons
  return [...new Set(parts.flatMap(part => (map[part] ? [map[part]] : [])))]
}

/**
 * The line for a Bot whose place in the Bot list is missing. Only `in_roster: true`
 * is known: `false` is also sent when the installed Bot list could not be read, so
 * then, as for a backend that sends no `in_roster`, "not added" is said only for a
 * roster reason that means it (`NOT_ADDED_REASONS`) and any other reads as "could not
 * be set up", which is true either way.
 */
function rosterLine(row: BotRow): string {
  const name = row.bot
  const reasons = reasonsOf(row, ['roster'])
  if (row.inRoster === true) {
    // In the Bot list, so never "not added": it is there but could not be set up.
    return reasons.length
      ? i18nT('pages.overview.portabilityTab.import_bot_not_set_up', { name, reasons: reasonText(reasons) })
      : i18nT('pages.overview.portabilityTab.import_bot_not_set_up_no_reason', { name })
  }
  const notAdded = reasons.filter(reason => NOT_ADDED_REASONS.has(reason))
  if (reasons.length && !notAdded.length) {
    return i18nT('pages.overview.portabilityTab.import_bot_not_set_up', { name, reasons: reasonText(reasons) })
  }
  return notAdded.length
    ? i18nT('pages.overview.portabilityTab.import_bot_not_added', { name, reasons: reasonText(notAdded) })
    : i18nT('pages.overview.portabilityTab.import_bot_not_added_no_reason', { name })
}

/** The line for a Bot that is not whole, or null for a whole one. */
export function notWholeLine(row: BotRow): string | null {
  // An agent file held by another Bot has its own line (`heldAgentFileLine`).
  const held = runsOnHeldAgentFile(row)
  const missing = missingParts(row).filter(part => !(held && part === 'crewmate_file'))
  if (!missing.length) return null
  const name = row.bot
  if (missing.includes('roster')) return rosterLine(row)
  const parts = fmtList(missing.map(part => i18nT(
    part === 'dashboard' && row.parts.dashboard === 'page_not_rebuilt' ? DASHBOARD_PAGE_KEY : PART_KEYS[part],
  )))
  // The reasons of the parts this line names, never a reason another part gave.
  const reasons = reasonsOf(row, missing)
  return reasons.length
    ? i18nT('pages.overview.portabilityTab.import_bot_incomplete_reasons', { name, parts, reasons: reasonText(reasons) })
    : i18nT('pages.overview.portabilityTab.import_bot_incomplete', { name, parts })
}

/** The line for a Bot that runs on another Bot's agent file, or null (`runsOnHeldAgentFile`). */
export function heldAgentFileLine(row: BotRow): string | null {
  return runsOnHeldAgentFile(row)
    ? i18nT('pages.overview.portabilityTab.import_bot_agent_file_held', { name: row.bot })
    : null
}

/** One line per notice the row carries. */
export function noticeLines(row: BotRow): string[] {
  return row.notices.map(notice => i18nT(BOT_NOTICE_KEYS[notice], { name: row.bot }))
}

/**
 * Every line the import summary's Bot keys produce. A Bot that is not whole is a
 * failure (`errors`); a notice, the withheld chats and the counts are caveats
 * (`warnings`). A whole Bot with no notice adds nothing.
 */
export function botImportLines(summary: Record<string, unknown>): { errors: string[]; warnings: string[] } {
  const rows = botRows(summary.bots)
  const errors = rows.flatMap(row => [notWholeLine(row), heldAgentFileLine(row)].filter(
    (line): line is string => line !== null,
  ))
  const failed = restoreFailure(summary.bots_failed)
  if (failed !== null) {
    errors.unshift(failed
      ? i18nT('pages.overview.portabilityTab.import_bots_failed', { error: failed })
      : i18nT('pages.overview.portabilityTab.import_bots_failed_no_detail'))
  }
  const warnings = rows.flatMap(noticeLines)
  const more = count(summary.bots_more)
  if (more) warnings.push(i18nT('pages.overview.portabilityTab.import_bots_more', { bots: more }))

  const orphans = orphanChats(summary.bot_transcripts_orphaned)
  const withheld = count(summary.sessions_member_withheld)
  if (orphans.length) {
    const names = [...new Set(orphans.map(o => o.candidates.length
      ? i18nT('pages.overview.portabilityTab.bot_chat_orphan_matches', { slug: o.slug, names: fmtList(o.candidates) })
      : o.slug))]
    const unlisted = Math.max(0, withheld - orphans.length)
    const chats = fmtList([...names, ...(unlisted ? [i18nT('app.n_more', { count: unlisted })] : [])])
    warnings.push(i18nT('pages.overview.portabilityTab.import_bot_chats_orphaned', { chats }))
  } else if (withheld) {
    warnings.push(i18nT('pages.overview.portabilityTab.import_bot_chats_withheld', { withheld }))
  }

  if (summary.bot_dashboards_not_carried === true) {
    warnings.push(i18nT('pages.overview.portabilityTab.import_bot_dashboards_not_carried'))
  }
  const recordsMore = count(summary.member_records_not_carried_more)
  if (recordsMore) warnings.push(i18nT('pages.overview.portabilityTab.import_bot_records_more', { bots: recordsMore }))
  const abilitiesMore = count(summary.crewmate_files_customised_more)
  if (abilitiesMore) warnings.push(i18nT('pages.overview.portabilityTab.import_bot_abilities_more', { bots: abilitiesMore }))
  return { errors, warnings }
}
