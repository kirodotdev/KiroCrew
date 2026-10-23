/**
 * What a card changes, as the gateway computed it, in ONE form per row: a list
 * edit is its added / removed items as +/− chips, a scalar is old → new. A
 * list row also carries the whole list before and after; printing that beside
 * the chips said the same change twice, so the chips alone stand for it.
 */
import { i18nT } from '../i18n/t'
import { effortLabel } from '../lib/effort'
import { fmtList } from '../i18n/format'
import type { CardChange } from '../api/cards'
import { CardRow } from './kindBodies'
import { fmtCardCron } from './scheduleText'

/** Localized labels for the card fields the gateway names in `field`. */
export const FIELD_LABELS: Record<string, () => string> = {
  value: () => i18nT('components.changeCards.field_value'),
  name: () => i18nT('components.changeCards.field_name'),
  message: () => i18nT('components.changeCards.field_message'),
  cron_expr: () => i18nT('components.changeCards.field_cron_expr'),
  at: () => i18nT('components.changeCards.field_at'),
  timezone: () => i18nT('components.changeCards.field_timezone'),
  goal: () => i18nT('components.changeCards.field_goal'),
  key_results: () => i18nT('components.changeCards.field_key_results'),
  auto_approve: () => i18nT('components.changeCards.field_auto_approve'),
  enabled: () => i18nT('components.changeCards.field_enabled'),
  display_name: () => i18nT('components.changeCards.field_display_name'),
  description: () => i18nT('components.changeCards.field_description'),
  model: () => i18nT('components.changeCards.field_model'),
  reasoning_effort: () => i18nT('components.changeCards.field_reasoning_effort'),
  approval_mode: () => i18nT('components.changeCards.field_approval_mode'),
  sender: () => i18nT('components.changeCards.field_sender'),
  not_included: () => i18nT('components.changeCards.field_not_included'),
  not_imported: () => i18nT('components.changeCards.field_not_imported'),
  template_auto_approve: () => i18nT('components.changeCards.field_template_auto_approve'),
  rules: () => i18nT('components.changeCards.field_rules'),
  model_hint: () => i18nT('components.changeCards.field_model_hint'),
  import_skills: () => i18nT('components.changeCards.field_import_skills'),
  import_crons: () => i18nT('components.changeCards.field_import_crons'),
  dashboard_template: () => i18nT('components.changeCards.field_dashboard_template'),
  carried_skills: () => i18nT('components.changeCards.field_carried_skills'),
  carried_scripts: () => i18nT('components.changeCards.field_carried_scripts'),
  carried_avatar: () => i18nT('components.changeCards.field_carried_avatar'),
}

/** The import rows whose text the crewmate reads every turn: shown whole, line breaks kept. */
const FULL_TEXT_FIELDS: ReadonlySet<string> = new Set(['rules', 'description', 'import_crons'])

/** One imported cron: its name, schedule and that it lands paused, then the
 *  full message it sends (or its script) and its time zone, so two crons that
 *  would do different things never read the same. */
function importCronText(job: unknown): string {
  const o = (job && typeof job === 'object' ? job : {}) as {
    name?: unknown; cron_expr?: unknown; message?: unknown; script?: unknown; timezone?: unknown
  }
  const when = typeof o.cron_expr === 'string' ? fmtCardCron(o.cron_expr) : ''
  const lines = [
    typeof o.message === 'string' ? o.message : '',
    typeof o.script === 'string' ? o.script : '',
    typeof o.timezone === 'string' && o.timezone ? i18nT('components.changeCards.import_cron_timezone', { tz: o.timezone }) : '',
  ].filter(Boolean)
  return i18nT('components.changeCards.import_cron_paused', { name: String(o.name ?? ''), when, detail: lines.join('\n') }).trimEnd()
}

/** One skill reference: the skill and the app it comes from. */
function importSkillText(ref: unknown): string {
  const o = (ref && typeof ref === 'object' ? ref : {}) as { name?: unknown; source?: unknown }
  const app = typeof o.source === 'string' && o.source.startsWith('app:') ? o.source.slice(4) : ''
  return i18nT('components.changeCards.import_skill_app', { name: String(o.name ?? ''), app })
}

/** One carried skill: its name and how many files it brings (each shown in full below). */
function carriedSkillText(skill: unknown): string {
  const o = (skill && typeof skill === 'object' ? skill : {}) as { name?: unknown; files?: unknown }
  const count = Array.isArray(o.files) ? o.files.length : 0
  return i18nT('components.changeCards.carried_skill_files', { name: String(o.name ?? ''), count })
}

/** One inherited MCP server, said as what it reaches: a service's host or a local program. */
function reachText(r: unknown): string {
  const o = (r && typeof r === 'object' ? r : {}) as { name?: unknown; host?: unknown; command?: unknown }
  const name = String(o.name ?? '')
  if (typeof o.host === 'string' && o.host) return i18nT('components.changeCards.reach_remote', { name, host: o.host })
  if (typeof o.command === 'string' && o.command) return i18nT('components.changeCards.reach_local', { name, command: o.command })
  return name
}

/** The servers a widen row names, for the warning above the rows. */
export function reachNames(v: unknown): string {
  if (Array.isArray(v)) return fmtList(v.map(r => String((r as { name?: unknown })?.name ?? r)))
  return typeof v === 'string' ? v : ''
}

/** Where the owner changes which servers new crewmates use unasked. */
export const TEMPLATES_HREF = '/capabilities?tab=templates'

/** The parts an import card says it leaves out, sent as codes. */
const LEFT_OUT_LABELS: Record<string, () => string> = {
  crons: () => i18nT('components.changeCards.left_crons'),
  keyResults: () => i18nT('components.changeCards.left_key_results'),
  avatar: () => i18nT('components.changeCards.left_avatar'),
  model: () => i18nT('components.changeCards.left_model'),
  dashboardTemplate: () => i18nT('components.changeCards.left_dashboard'),
}

/** The approval modes a crewmate's own chat can be set to, in the picker's words. */
const APPROVAL_MODE_LABELS: Record<string, () => string> = {
  normal: () => i18nT('components.approvalModePicker.normal_label'),
  trust_reads: () => i18nT('components.approvalModePicker.reads_label'),
  trust: () => i18nT('components.approvalModePicker.trust_label'),
}

/** What the mode the card asks for means for the crewmate, one line: this is
 *  the permission the user is approving, so the card says it rather than
 *  leaving the reader to look the word up. */
const APPROVAL_MODE_HINTS: Record<string, () => string> = {
  normal: () => i18nT('components.changeCards.approval_mode_hint_normal'),
  trust_reads: () => i18nT('components.changeCards.approval_mode_hint_trust_reads'),
  trust: () => i18nT('components.changeCards.approval_mode_hint_trust'),
}

/** A line under a row's value explaining what the new value means, when the field has one. */
export function changeHint(c: CardChange): string | null {
  if (c.field === 'approval_mode' && typeof c.after === 'string' && APPROVAL_MODE_HINTS[c.after]) return APPROVAL_MODE_HINTS[c.after]()
  if (c.field === 'import_approval') return i18nT('components.changeCards.approval_hint_import')
  return null
}

/** A row's label: the localized field name when known, else the gateway's. */
export function changeLabel(c: CardChange): string {
  if (c.field === 'goal' && typeof c.number === 'number') return i18nT('pages.membersPage.goals_goal_number', { number: c.number })
  if (c.field === 'import_approval') return FIELD_LABELS.approval_mode()
  return (c.field && FIELD_LABELS[c.field]?.()) || c.label
}

/** A row's value as the Schedule page would show it: cron reads as a clock and
 *  days, plus the recurrence the card must always state. */
function changeValue(c: CardChange, v: unknown): string {
  if (c.field === 'cron_expr' && typeof v === 'string') return fmtCardCron(v)
  // An effort level is a code identifier on the wire; "" is the inherit
  // sentinel, which the crew editor also labels as inherited.
  if (c.field === 'reasoning_effort' && typeof v === 'string') return v === '' ? i18nT('pages.kiroCrewAgentsPage.inherited') : effortLabel(v)
  // An approval mode is a code identifier on the wire; the card shows the word
  // the picker shows for it, so "trust" reads as the same thing in both places.
  if ((c.field === 'approval_mode' || c.field === 'import_approval') && typeof v === 'string' && APPROVAL_MODE_LABELS[v]) return APPROVAL_MODE_LABELS[v]()
  // Codes on the wire, so the words are in the reader's language.
  if (c.field === 'not_included' && v === 'private') return i18nT('components.changeCards.not_included_private')
  if (c.field === 'template_auto_approve' && Array.isArray(v)) return v.map(reachText).join('; ')
  if (c.field === 'not_imported' && Array.isArray(v)) return fmtList(v.map(code => LEFT_OUT_LABELS[String(code)]?.() ?? String(code)))
  if (c.field === 'import_crons' && Array.isArray(v)) return v.map(importCronText).join('\n')
  if (c.field === 'import_skills' && Array.isArray(v)) return fmtList(v.map(importSkillText))
  if (c.field === 'carried_skills' && Array.isArray(v)) return fmtList(v.map(carriedSkillText))
  if (c.field === 'carried_scripts' && Array.isArray(v)) return fmtList(v.map(s => String((s as { file?: unknown })?.file ?? '')))
  if (c.field === 'carried_avatar' && v && typeof v === 'object') return i18nT('components.changeCards.carried_avatar_kind', { type: String((v as { type?: unknown }).type ?? '') })
  return formatCardValue(v)
}

export function formatCardValue(v: unknown): string {
  if (v === undefined || v === null || v === '') return i18nT('components.changeCards.empty_value')
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (Array.isArray(v) && v.every(item => typeof item === 'string')) {
    // A string-list setting (e.g. hidden models): read as a list, not JSON.
    return v.length ? fmtList(v as string[], { style: 'narrow' }) : i18nT('components.changeCards.empty_value')
  }
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

/** True when the row is a list edit: its items, not its whole list, are the change. */
export function isListChange(c: CardChange): boolean {
  return !!(c.add?.length || c.remove?.length)
}

const CHIP = 'inline-flex max-w-full items-baseline rounded px-1.5 text-[12px] leading-5 text-text [overflow-wrap:anywhere]'

export function CardChanges({ changes }: { changes: CardChange[] }) {
  return (
    <>
      {changes.map((c, i) => (
        <CardRow key={`${c.label}-${i}`} label={changeLabel(c)}>
          {isListChange(c) ? (
            <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0" data-testid="change-card-items">
              {(c.add ?? []).map((v, j) => (
                <li key={`a${j}`} className={`${CHIP} bg-ok-subtle`}>
                  <span aria-hidden="true">+&nbsp;</span><span className="sr-only">{i18nT('components.changeCards.added')} </span>{formatCardValue(v)}
                </li>
              ))}
              {(c.remove ?? []).map((v, j) => (
                <li key={`r${j}`} className={`${CHIP} bg-danger-subtle`}>
                  <span aria-hidden="true">−&nbsp;</span><span className="sr-only">{i18nT('components.changeCards.removed')} </span>{formatCardValue(v)}
                </li>
              ))}
            </ul>
          ) : (c.before !== undefined || c.after !== undefined) && (
            <>
              <span className="inline-flex max-w-full flex-wrap items-baseline gap-x-1.5" data-testid="change-card-value">
                {c.before !== undefined && <span className="text-muted [overflow-wrap:anywhere]">{changeValue(c, c.before)}</span>}
                {c.before !== undefined && <span aria-hidden="true" className="text-muted">→</span>}
                {c.before !== undefined && <span className="sr-only">{i18nT('components.changeCards.becomes')}</span>}
                {c.after !== undefined && (
                  <span className={`font-medium [overflow-wrap:anywhere]${c.field && FULL_TEXT_FIELDS.has(c.field) ? ' whitespace-pre-wrap' : ''}`}>
                    {changeValue(c, c.after)}
                  </span>
                )}
              </span>
              {changeHint(c) && (
                <span className="block text-[12px] leading-4 text-muted" data-testid="change-card-hint">{changeHint(c)}</span>
              )}
              {c.field === 'template_auto_approve' && (
                <a className="block text-[12px] leading-4 text-accent underline" href={TEMPLATES_HREF} data-testid="change-card-template-link">{i18nT('components.changeCards.template_link')}</a>
              )}
            </>
          )}
        </CardRow>
      ))}
    </>
  )
}
