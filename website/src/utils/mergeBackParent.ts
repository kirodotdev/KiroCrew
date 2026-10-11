/**
 * What a fork's parent is, read from the parent session key the fork carries
 * (`forked_from`), for the two places that explain a `parent_not_dashboard`
 * refusal: the greyed "Merge into parent…" row and the merge dialog.
 *
 * The gateway (`chat_merge_back._parent_not_dashboard_refusal`) refuses every
 * parent whose turns can run outside the dashboard: a session key outside the
 * `dashboard:` namespace (a channel thread such as `slack:<ts>`, a scheduled job
 * `cron:<id>`), and a dashboard key over a channel-born slot
 * (`dashboard:slack_<ts>`), whose slot key the gateway minted from the channel
 * key. The reason a person reads should name the parent in words they
 * recognise, so this classifies the key as far as the client can tell and leaves
 * the rest generic.
 */
import { i18nT } from '../i18n/t'
import { channelBrandLabel, slotChannelNamespace } from './channelOrigin'

/**
 * The session-key namespace a scheduled job's run takes turns under
 * (`cron:<job id>`), as `cron_script` publishes it. Only the bare session key
 * names a job: a dashboard slot that merely spells `cron_` in its own key is an
 * ordinary dashboard chat to the gateway, so it is to the client too.
 */
const SCHEDULED_JOB_KEY = /^cron[:_]/

/** The namespace `dm_scope="unified"` collapses direct messages into: a conversation with no product name to show. */
const DIRECT_MESSAGE_NAMESPACE = 'unified'

export type MergeBackParent =
  /** A chat that runs in the dashboard: the one kind a merge can reach. */
  | { kind: 'dashboard' }
  /** A conversation on a messaging product, with that product's name. */
  | { kind: 'channel'; channel: string }
  /** A direct-message conversation whose channel the key does not name. */
  | { kind: 'direct_message' }
  /** A scheduled job's run. */
  | { kind: 'scheduled_job' }
  /** A session outside the dashboard the client cannot name further. */
  | { kind: 'outside' }

/**
 * Classify the parent named by a fork's `forked_from`. The channel stem alone
 * says where the conversation started, in either spelling the gateway uses: a
 * channel session key (`slack:<ts>`) when the parent slot is bound to its
 * channel session, and a dashboard key over a channel-born slot
 * (`dashboard:slack_<ts>`) when it is not.
 */
export function mergeBackParentOf(forkedFrom: string): MergeBackParent {
  const dashboardKey = forkedFrom.startsWith('dashboard:')
  const stem = dashboardKey ? forkedFrom.slice('dashboard:'.length) : forkedFrom
  const namespace = slotChannelNamespace(stem)
  if (namespace === DIRECT_MESSAGE_NAMESPACE) return { kind: 'direct_message' }
  if (namespace) return { kind: 'channel', channel: channelBrandLabel(namespace) }
  if (dashboardKey) return { kind: 'dashboard' }
  if (SCHEDULED_JOB_KEY.test(forkedFrom)) return { kind: 'scheduled_job' }
  return { kind: 'outside' }
}

/** Whether the gateway refuses the parent in `forkedFrom` as one whose turns can run outside the dashboard. */
export function parentTakesTurnsOutsideDashboard(forkedFrom: string): boolean {
  return mergeBackParentOf(forkedFrom).kind !== 'dashboard'
}

/**
 * The short reason the greyed menu row shows for such a parent, or `''` for a
 * dashboard parent. Short enough for a menu row: the row's width is fixed by
 * "Merge into parent…" beside it.
 */
export function mergeBackParentMenuReason(forkedFrom: string): string {
  const parent = mergeBackParentOf(forkedFrom)
  switch (parent.kind) {
    case 'dashboard':
      return ''
    case 'channel':
      return i18nT('components.mergeBackMenuItem.parent_channel', { channel: parent.channel })
    case 'direct_message':
      return i18nT('components.mergeBackMenuItem.parent_direct_message')
    case 'scheduled_job':
      return i18nT('components.mergeBackMenuItem.parent_scheduled_job')
    case 'outside':
      return i18nT('components.mergeBackMenuItem.parent_not_dashboard')
  }
}

/**
 * The dialog's wording of the gateway's `parent_not_dashboard` refusal: the
 * rule (a merge reaches only a chat that runs in the dashboard) and then the
 * parent's kind where the client can tell it. A dashboard parent gets the
 * generic line too: the gateway knows a channel-born parent the client does not
 * (its `channel_origin` flag), so the refusal can arrive for a key that reads as
 * a dashboard chat here.
 */
export function mergeBackParentRefusal(forkedFrom: string): string {
  const parent = mergeBackParentOf(forkedFrom)
  switch (parent.kind) {
    case 'channel':
      return i18nT('utils.mergeBackError.parent_channel', { channel: parent.channel })
    case 'direct_message':
      return i18nT('utils.mergeBackError.parent_direct_message')
    case 'scheduled_job':
      return i18nT('utils.mergeBackError.parent_scheduled_job')
    case 'dashboard':
    case 'outside':
      return i18nT('utils.mergeBackError.parent_not_dashboard')
  }
}
