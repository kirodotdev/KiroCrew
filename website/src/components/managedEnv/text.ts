/**
 * The managed-environment lane's copy, read through the catalog (`managedEnv.*` in
 * `locales/en.json`). Each entry is a getter, so it is translated when it is read, never at
 * module load. Lines the server sends (errors, setup lines) are shown as they come.
 */
import { i18nT } from '../../i18n/t'

export const SIGN_IN_TEXT = {
  get signedIn() { return i18nT('managedEnv.signIn.signed_in') },
  get nameUnknown() { return i18nT('managedEnv.signIn.name_unknown') },
  get required() { return i18nT('managedEnv.signIn.required') },
  get localAgentOff() { return i18nT('managedEnv.signIn.local_agent_off') },
}

export function signedInAsEmail(email: string): string {
  return i18nT('managedEnv.signIn.signed_in_as', { email })
}

export function signedInAsTail(tail: string): string {
  return i18nT('managedEnv.signIn.signed_in_account', { tail })
}

export const CREATE_TEXT = {
  get title() { return i18nT('managedEnv.create.title') },
  get lead() { return i18nT('managedEnv.create.lead') },
  get launch() { return i18nT('managedEnv.create.launch') },
  get launching() { return i18nT('managedEnv.create.launching') },
  get name() { return i18nT('managedEnv.create.name') },
  get nameHint() { return i18nT('managedEnv.create.name_hint') },
  get nameMissing() { return i18nT('managedEnv.create.name_missing') },
  get nameRule() { return i18nT('managedEnv.create.name_rule') },
  get idle() { return i18nT('managedEnv.create.idle') },
  get idleUnit() { return i18nT('managedEnv.create.idle_unit') },
  get idleHint() { return i18nT('managedEnv.create.idle_hint') },
  get idleOff() { return i18nT('managedEnv.create.idle_off') },
  get idleOffRelaunch() { return i18nT('managedEnv.create.idle_off_relaunch') },
  get idleBad() { return i18nT('managedEnv.create.idle_bad') },
  get checks() { return i18nT('managedEnv.create.checks') },
  get netOk() { return i18nT('managedEnv.create.net_ok') },
  get netAsk() { return i18nT('managedEnv.create.net_ask') },
  get netAllow() { return i18nT('managedEnv.create.net_allow') },
  get netWithout() { return i18nT('managedEnv.create.net_without') },
  get netPending() { return i18nT('managedEnv.create.net_pending') },
}

export const LIST_TEXT = {
  get yours() { return i18nT('managedEnv.list.yours') },
  get noCrewTitle() { return i18nT('managedEnv.list.no_crew_title') },
  get allHaveCrews() { return i18nT('managedEnv.list.all_have_crews') },
  get empty() { return i18nT('managedEnv.list.empty') },
  get otherClient() { return i18nT('managedEnv.list.other_client') },
  get notSetUp() { return i18nT('managedEnv.list.not_set_up') },
  get markerLine() { return i18nT('managedEnv.list.marker_line') },
  get setUpCrew() { return i18nT('managedEnv.list.set_up_crew') },
  get unknownStatus() { return i18nT('managedEnv.list.unknown_status') },
  get stop() { return i18nT('managedEnv.list.stop') },
  get resume() { return i18nT('managedEnv.list.resume') },
  get delete() { return i18nT('managedEnv.list.delete') },
  get deleteBody() { return i18nT('managedEnv.list.delete_body') },
  get deleteConfirm() { return i18nT('managedEnv.list.delete_confirm') },
  get inProgress() { return i18nT('managedEnv.list.in_progress') },
}

export function withCrewPointer(count: number): string {
  return i18nT('managedEnv.list.with_crew_pointer', { count })
}

export function deleteTitle(name: string): string {
  return i18nT('managedEnv.list.delete_title', { name })
}

export const NETCFG_TEXT = {
  get label() { return i18nT('managedEnv.network.label') },
  get appliesLine() { return i18nT('managedEnv.network.applies_line') },
  get fallbackDialog() { return i18nT('managedEnv.network.fallback_dialog') },
  get askTitle() { return i18nT('managedEnv.network.ask_title') },
  get agree() { return i18nT('managedEnv.network.agree') },
  get without() { return i18nT('managedEnv.network.without') },
  get cancel() { return i18nT('managedEnv.network.cancel') },
  get withoutWarning() { return i18nT('managedEnv.network.without_warning') },
  get restore() { return i18nT('managedEnv.network.restore') },
}

export const IDLE_TEXT = {
  get title() { return i18nT('managedEnv.idle.title') },
  get running() { return i18nT('managedEnv.idle.running') },
  get stopped() { return i18nT('managedEnv.idle.stopped') },
  get busy() { return i18nT('managedEnv.idle.busy') },
  get range() { return i18nT('managedEnv.idle.range') },
  get hint() { return i18nT('managedEnv.idle.hint') },
  get off() { return i18nT('managedEnv.idle.off') },
  get save() { return i18nT('managedEnv.idle.save') },
  get saveRestart() { return i18nT('managedEnv.idle.save_restart') },
  get cancel() { return i18nT('managedEnv.idle.cancel') },
  get field() { return i18nT('managedEnv.idle.field') },
  get unit() { return i18nT('managedEnv.idle.unit') },
}

export function idleUnitLabel(unit: 'minutes' | 'hours' | 'days'): string {
  if (unit === 'days') return i18nT('managedEnv.idle.unit_days')
  if (unit === 'hours') return i18nT('managedEnv.idle.unit_hours')
  return i18nT('managedEnv.idle.unit_minutes')
}

/** "60 minutes", "4 hours", "7 days": whole hours and days from 2 hours up, else minutes. */
export function idleSpanText(minutes: number): string {
  if (minutes >= 120 && minutes % 1440 === 0) return i18nT('managedEnv.idle.span_days', { count: minutes / 1440 })
  if (minutes >= 120 && minutes % 60 === 0) return i18nT('managedEnv.idle.span_hours', { count: minutes / 60 })
  return i18nT('managedEnv.idle.span_minutes', { count: minutes })
}

export function idleStopsAfter(minutes: number): string {
  return i18nT('managedEnv.idle.stops_after', { span: idleSpanText(minutes) })
}

/** The four quick picks, in minutes. */
export const IDLE_PRESET_MINUTES = [60, 240, 1440, 10080] as const

export function idleDialogTitle(name: string): string {
  return i18nT('managedEnv.idle.dialog_title', { name })
}

export function renameDialogTitle(name: string): string {
  return i18nT('managedEnv.rename.dialog_title', { name })
}

export const RENAME_TEXT = {
  get title() { return i18nT('managedEnv.rename.title') },
  get label() { return i18nT('managedEnv.rename.label') },
  get hint() { return i18nT('managedEnv.rename.hint') },
  get rule() { return i18nT('managedEnv.rename.rule') },
  get save() { return i18nT('managedEnv.rename.save') },
  get cancel() { return i18nT('managedEnv.rename.cancel') },
}

export const ROW_TEXT = {
  get running() { return i18nT('managedEnv.row.running') },
  get stopped() { return i18nT('managedEnv.row.stopped') },
  get unknown() { return i18nT('managedEnv.row.unknown') },
  get stop() { return i18nT('managedEnv.row.stop') },
  get resume() { return i18nT('managedEnv.row.resume') },
  get delete() { return i18nT('managedEnv.row.delete') },
  get idle() { return i18nT('managedEnv.row.idle') },
  get rename() { return i18nT('managedEnv.row.rename') },
  get unreachable() { return i18nT('managedEnv.row.unreachable') },
  get cantReconnect() { return i18nT('managedEnv.row.cant_reconnect') },
  get gatewayDown() { return i18nT('managedEnv.row.gateway_down') },
  get startCrew() { return i18nT('managedEnv.row.start_crew') },
  get stopButton() { return i18nT('managedEnv.row.stop_button') },
  get resumeButton() { return i18nT('managedEnv.row.resume_button') },
  get idleButton() { return i18nT('managedEnv.row.idle_button') },
  get deleteBody() { return i18nT('managedEnv.row.delete_body') },
  get deleteConfirm() { return i18nT('managedEnv.row.delete_confirm') },
  get stopping() { return i18nT('managedEnv.row.stopping') },
  get resuming() { return i18nT('managedEnv.row.resuming') },
  get deleting() { return i18nT('managedEnv.row.deleting') },
  get idleChanging() { return i18nT('managedEnv.row.idle_changing') },
  get inProgress() { return i18nT('managedEnv.row.in_progress') },
  get phaseStarting() { return i18nT('managedEnv.row.phase_starting') },
  get phaseTunnel() { return i18nT('managedEnv.row.phase_tunnel') },
  get phaseSigningIn() { return i18nT('managedEnv.row.phase_signing_in') },
}

export function rowDeleteTitle(name: string): string {
  return i18nT('managedEnv.row.delete_title', { name })
}

export type RowAction = 'stop' | 'resume' | 'delete' | 'idle' | 'rename'

/** The accessible name of a row action, with the crew it acts on. */
export function rowActionLabel(action: RowAction, name: string): string {
  if (action === 'stop') return i18nT('managedEnv.row.stop_of', { name })
  if (action === 'resume') return i18nT('managedEnv.row.resume_of', { name })
  if (action === 'delete') return i18nT('managedEnv.row.delete_of', { name })
  if (action === 'idle') return i18nT('managedEnv.row.idle_of', { name })
  return i18nT('managedEnv.row.rename_of', { name })
}

/** A state line for a status other than RUNNING or STOPPED (upper-case, as the list sends it). */
export function rowStatus(status: string): string {
  if (status === 'PENDING') return i18nT('managedEnv.row.state_pending')
  if (status === 'STOPPING') return i18nT('managedEnv.row.state_stopping')
  if (status === 'FAILED') return i18nT('managedEnv.row.state_failed')
  if (status === 'GONE' || status === 'DELETED') return i18nT('managedEnv.row.state_gone')
  return i18nT('managedEnv.row.state_other', { status })
}
