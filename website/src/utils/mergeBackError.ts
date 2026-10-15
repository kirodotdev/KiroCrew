/**
 * The words a person reads when a merge-back route refuses.
 *
 * Every refusal from `POST .../merge-back/draft` and `POST .../merge-back`
 * carries a machine-readable `code`, and this is where a code becomes copy in
 * the user's language. The server's own `error` sentence is English prose for
 * logs and API callers; rendering it would put English inside a translated
 * dashboard. An unknown code, or a failure with no code (a dropped connection),
 * falls back to one generic line.
 */
import { i18nT } from '../i18n/t'
import { parseErrorCode } from './errorReport'
import { mergeBackParentRefusal } from './mergeBackParent'

export function mergeBackErrorCode(err: unknown): string | undefined {
  const body = (err as { body?: unknown } | null)?.body
  return parseErrorCode(typeof body === 'string' ? body : undefined)
}

/**
 * Localized copy for a merge-back refusal. `parentSession` is the fork's
 * `forked_from`, which lets a `parent_not_dashboard` refusal name the parent's
 * kind (a Slack chat, a scheduled job) instead of the rule alone.
 */
export function mergeBackErrorMessage(err: unknown, parentSession = ''): string {
  switch (mergeBackErrorCode(err)) {
    case 'parent_not_open':
      return i18nT('utils.mergeBackError.parent_not_open')
    case 'parent_deleted':
      return i18nT('utils.mergeBackError.parent_deleted')
    case 'parent_unconfirmed':
      return i18nT('utils.mergeBackError.parent_unconfirmed')
    case 'parent_not_dashboard':
      return mergeBackParentRefusal(parentSession)
    case 'nothing_to_merge':
      return i18nT('utils.mergeBackError.nothing_to_merge')
    case 'fork_running':
      return i18nT('utils.mergeBackError.fork_running')
    case 'merge_back_restricted':
      return i18nT('utils.mergeBackError.restricted')
    case 'merge_draft_in_flight':
      return i18nT('utils.mergeBackError.draft_in_flight')
    case 'merge_summary_failed':
      return i18nT('utils.mergeBackError.summary_failed')
    case 'merge_point_missing':
    case 'merge_draft_stale':
      return i18nT('utils.mergeBackError.merge_point_missing')
    case 'already_merged':
      return i18nT('utils.mergeBackError.already_merged')
    case 'merge_queue_full':
      return i18nT('utils.mergeBackError.queue_full')
    case 'deferred_notes_full':
      return i18nT('utils.mergeBackError.parent_holds_too_many')
    case 'summary_too_long':
    case 'deferred_note_too_large':
      return i18nT('utils.mergeBackError.summary_too_long')
    default:
      return i18nT('utils.mergeBackError.failed')
  }
}
