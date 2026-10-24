/**
 * Change-card kind registry: for every kind the gateway can propose, the fixed
 * component and wording the dashboard renders. The agent picks a kind and fills its
 * params; it never supplies a component, a form or a request address.
 */
import type { ComponentType } from 'react'
import { AlarmClock,
  CalendarClock, Cog, FolderCog, Goal, Gauge, KeyRound, LayoutDashboard, Link2, Plug, Power, ShieldCheck, ShieldOff, UserPlus, UserCog, Wrench, LayoutTemplate, PackagePlus, PackageOpen,
  type LucideIcon,
} from 'lucide-react'
import { i18nT } from '../i18n/t'
import type { Card, CardChange, CardKind } from '../api/cards'
import { NoBody, OneCrewmateBody, ScheduleBody, type KindBodyProps } from './kindBodies'
import { ImportFilesBody } from './ImportFilesBody'
import { cardTimezone, fmtOneShot, isOneShot } from './oneShot'

export interface CardKindSpec {
  icon: LucideIcon
  /** The kind's short name, shown above the title. */
  label: () => string
  /** The primary button: the concrete action, never a bare "OK". */
  primary: (card?: Card) => string
  /** The decline button, when "Decline change" does not say what declining means here. */
  decline?: () => string
  /** One line beside the primary button: what the press does and how to take it back. */
  pressNote?: () => string
  Body: ComponentType<KindBodyProps>
}

export const CARD_REGISTRY: Record<CardKind, CardKindSpec> = {
  'setting.change': {
    icon: Cog, label: () => i18nT('components.changeCards.kind_setting_change'),
    primary: () => i18nT('components.changeCards.primary_setting_change'), Body: NoBody,
  },
  'schedule.create': {
    icon: CalendarClock, label: () => i18nT('components.changeCards.kind_schedule_create'),
    // A one-shot is what a person calls a reminder, and it is the word Mate
    // uses when it points at this button: the label has to match what it says.
    primary: card => i18nT(card && isOneShot(card)
      ? 'components.changeCards.primary_schedule_once'
      : 'components.changeCards.primary_schedule_create'), Body: ScheduleBody,
  },
  'schedule.update': {
    icon: CalendarClock, label: () => i18nT('components.changeCards.kind_schedule_update'),
    primary: () => i18nT('components.changeCards.primary_schedule_update'), Body: ScheduleBody,
  },
  'crewmate.create': {
    icon: UserPlus, label: () => i18nT('components.changeCards.kind_crewmate_create'),
    primary: () => i18nT('components.changeCards.primary_crewmate_create'), Body: ScheduleBody,
  },
  'crewmate.import': {
    icon: UserPlus, label: () => i18nT('components.changeCards.kind_crewmate_import'),
    primary: () => i18nT('components.changeCards.primary_crewmate_import'), Body: ImportFilesBody,
    decline: () => i18nT('components.changeCards.decline_crewmate_import'),
    pressNote: () => i18nT('components.changeCards.note_crewmate_import'),
  },
  'crewmate.update': {
    icon: UserCog, label: () => i18nT('components.changeCards.kind_crewmate_update'),
    primary: () => i18nT('components.changeCards.primary_crewmate_update'), Body: OneCrewmateBody,
  },
  'crewmate.capabilities': {
    icon: Wrench, label: () => i18nT('components.changeCards.kind_crewmate_capabilities'),
    primary: () => i18nT('components.changeCards.primary_crewmate_capabilities'), Body: OneCrewmateBody,
  },
  'crewmate.goal': {
    icon: Goal, label: () => i18nT('components.changeCards.kind_crewmate_goal'),
    primary: () => i18nT('components.changeCards.primary_crewmate_goal'), Body: OneCrewmateBody,
  },
  'crewmate.key_results': {
    icon: Gauge, label: () => i18nT('components.changeCards.kind_crewmate_key_results'),
    primary: () => i18nT('components.changeCards.primary_crewmate_key_results'), Body: OneCrewmateBody,
  },
  'crewmate.check_ins': {
    icon: AlarmClock, label: () => i18nT('components.changeCards.kind_crewmate_check_ins'),
    primary: () => i18nT('components.changeCards.primary_crewmate_check_ins'), Body: OneCrewmateBody,
  },
  'crewmate.dashboard_page': {
    icon: LayoutDashboard, label: () => i18nT('components.changeCards.kind_crewmate_dashboard_page'),
    primary: () => i18nT('components.changeCards.primary_crewmate_dashboard_page'), Body: OneCrewmateBody,
  },
  'template.update': {
    icon: LayoutTemplate, label: () => i18nT('components.changeCards.kind_template_update'),
    primary: () => i18nT('components.changeCards.primary_template_update'), Body: NoBody,
  },
  'mcp.install': {
    icon: PackagePlus, label: () => i18nT('components.changeCards.kind_mcp_install'),
    primary: () => i18nT('components.changeCards.primary_mcp_install'), Body: NoBody,
  },
  'mcp.add_custom': {
    icon: PackageOpen, label: () => i18nT('components.changeCards.kind_mcp_add_custom'),
    primary: () => i18nT('components.changeCards.primary_mcp_add_custom'), Body: NoBody,
  },
  'mcp.toggle': {
    icon: Power, label: () => i18nT('components.changeCards.kind_mcp_toggle'),
    primary: () => i18nT('components.changeCards.primary_mcp_toggle'), Body: NoBody,
  },
  'connection.connect': {
    icon: Link2, label: () => i18nT('components.changeCards.kind_connection_connect'),
    primary: () => i18nT('components.changeCards.primary_connection_connect'), Body: NoBody,
  },
  'secret.save': {
    icon: KeyRound, label: () => i18nT('components.changeCards.kind_secret_save'),
    primary: () => i18nT('components.changeCards.primary_secret_save'), Body: NoBody,
  },
  'trust.app': {
    icon: ShieldCheck, label: () => i18nT('components.changeCards.kind_trust_app'),
    primary: () => i18nT('components.changeCards.primary_trust_app'), Body: NoBody,
  },
  denied_command: {
    icon: ShieldOff, label: () => i18nT('components.changeCards.kind_denied_command'),
    primary: () => i18nT('components.changeCards.primary_denied_command'), Body: NoBody,
  },
  'folder.steering': {
    // The Folder settings section's own name, so the card reads as that setting.
    icon: FolderCog, label: () => i18nT('components.folderConfigModal.steering_dirs'),
    primary: () => i18nT('components.changeCards.primary_setting_change'), Body: NoBody,
  },
}

/** A kind the dashboard does not know renders nothing actionable. */
export function specFor(kind: string): CardKindSpec | null {
  return Object.prototype.hasOwnProperty.call(CARD_REGISTRY, kind) ? CARD_REGISTRY[kind as CardKind] : null
}

/** The undo button, named for what it actually does (the gateway's `undo_label`). */
export function undoLabelFor(card: Pick<Card, 'undo_label'>): string {
  if (card.undo_label === 'delete_secret') return i18nT('components.changeCards.undo_delete_secret')
  return i18nT('components.changeCards.undo')
}

/** The icon of a kind this build does not know. */
export const FALLBACK_ICON: LucideIcon = Plug

/** How a one-item edit of a list setting reads: the list it really edits and
 *  what the person will see. The Settings control's own label can say the
 *  opposite ("Selectable Models" edits the HIDDEN list), so an entry here wins
 *  over it; mirrors the gateway's `LIST_SETTING_WORDING`. */
interface ListWording {
  label: () => string
  add: (item: string) => string
  remove: (item: string) => string
}

const HIDDEN_MODELS: ListWording = {
  label: () => i18nT('components.changeCards.hidden_models_label'),
  add: item => i18nT('components.changeCards.hidden_models_hide', { item }),
  remove: item => i18nT('components.changeCards.hidden_models_show', { item }),
}

/** By registry id, and by the `path` form a card may carry instead. */
const LIST_WORDING: Record<string, ListWording> = {
  'chat.selectable-models': HIDDEN_MODELS,
  'dashboard.model_picker_hidden_models': HIDDEN_MODELS,
}

interface ListOp { op: 'add' | 'remove'; item: string; wording: ListWording | null }

/** The add/remove of a list setting card, or null for any other card. */
function listOp(card: Pick<Card, 'kind' | 'params'>): ListOp | null {
  if (card.kind !== 'setting.change') return null
  const { op, item, setting_id: settingId, path } = card.params
  if ((op !== 'add' && op !== 'remove') || typeof item !== 'string') return null
  const id = typeof settingId === 'string' ? settingId : typeof path === 'string' ? path : ''
  const wording = Object.prototype.hasOwnProperty.call(LIST_WORDING, id) ? LIST_WORDING[id] : null
  return { op, item, wording }
}

/** The one card that lets a crewmate ask the user for its worker's tools
 *  (`kiro_crew/mate_tool_access.py`): a `crewmate.update` writing only its
 *  `dynamic_capabilities` flag on. It is a preview opt-in, so it carries the
 *  Preview badge the Profile card's switch shows. */
export function isToolAccessCard(card: Pick<Card, 'kind' | 'params'>): boolean {
  if (card.kind !== 'crewmate.update') return false
  const fields = card.params.fields
  if (!fields || typeof fields !== 'object' || Array.isArray(fields)) return false
  const entries = Object.entries(fields as Record<string, unknown>)
  return entries.length === 1 && entries[0][0] === 'dynamic_capabilities' && entries[0][1] === true
}

/** The card's title in the reader's language where the dashboard owns the
 *  wording (a list setting's add/remove, the tool-access ask, the import); otherwise
 *  the gateway's title. */
export function cardTitle(card: Pick<Card, 'kind' | 'params' | 'title' | 'changes'> & Partial<Pick<Card, 'scope'>>): string {
  if (isToolAccessCard(card)) {
    const name = card.scope?.members?.[0] || String(card.params.name ?? '')
    return i18nT('components.changeCards.tool_access_title', { name })
  }
  // The gateway's title is English; the import's is rebuilt in the reader's language.
  const name = card.kind === 'crewmate.import' ? card.params?.name : undefined
  if (typeof name === 'string' && name) return i18nT('components.changeCards.title_crewmate_import', { name })
  const list = listOp(card)
  if (!list) return card.title
  if (list.wording) return list.wording[list.op](list.item)
  const label = card.changes[0]?.label
  if (!label) return card.title
  const key = list.op === 'add' ? 'components.changeCards.list_title_add' : 'components.changeCards.list_title_remove'
  return i18nT(key, { item: list.item, label })
}

/** A one-shot schedule's rows: no recurrence row, and its `at` row reads
 *  "Runs once" with the localized date and time. */
function oneShotChanges(card: Pick<Card, 'kind' | 'params' | 'changes' | 'once' | 'timezone'>): CardChange[] {
  const tz = cardTimezone(card)
  const when = (v: unknown) => (typeof v === 'string' || typeof v === 'number' ? fmtOneShot(v, tz) ?? v : v)
  return card.changes
    .filter(c => c.field !== 'cron_expr')
    .map(c => c.field !== 'at' ? c : {
      // The row is named here, not by the `at` edit field's label.
      ...c,
      field: undefined,
      label: i18nT('components.changeCards.schedule_once'),
      ...(c.before !== undefined ? { before: when(c.before) } : {}),
      ...(c.after !== undefined ? { after: when(c.after) } : {}),
    })
}

/** The card's change rows, a list setting's row named for the list it edits. */
export function cardChanges(card: Pick<Card, 'kind' | 'params' | 'changes'> & Partial<Pick<Card, 'once' | 'timezone'>>): CardChange[] {
  if (isOneShot(card)) return oneShotChanges(card)
  const wording = listOp(card)?.wording
  if (!wording || card.changes.length !== 1) return card.changes
  return [{ ...card.changes[0], label: wording.label() }]
}
