/**
 * The sentence for a history delete the gateway REFUSED.
 *
 * `DELETE /api/sessions/<key>` answers 409 with `{ ok: false, error, code }` when
 * the row cannot go yet: `error` is English prose for logs and the CLI, `code` is
 * the machine-readable cause. The dashboard renders from the CODE alone -- the
 * catalogs carry one localized sentence per cause -- and never from `error`,
 * which would show English in eleven other languages.
 *
 * The codes mirror `CRON_*_CODE` in `src/kiro_crew/dashboard/handlers/sessions.py`.
 * They are distinct because their remedies are: unknown cron ownership wants
 * the by-id release plus transcript-metadata repair, a busy cron store wants a
 * retry, and an unreadable one wants the store file repaired. A rejection
 * carrying no recognised code (a dropped connection, a 5xx) gets the generic
 * sentence.
 */
import { i18nT } from '../i18n/t'
import type { ErrorReport } from './errorReport'

/** Raw refusal facts plus the exact API report used by the agent hand-off. */
export type HistoryDeleteRefusal = {
  key: string
  title: string
  code: string
  report?: ErrorReport
}

/** The row's cron jobs could not be determined (unreadable transcript metadata). */
export const CRON_OWNERSHIP_UNKNOWN_CODE = 'cron_ownership_unknown'
/** The cron store's lock stayed held past the gateway's bounded backoff. */
export const CRON_STORE_BUSY_CODE = 'cron_store_busy'
/** The cron store file could not be read at all. */
export const CRON_STORE_UNREADABLE_CODE = 'cron_store_unreadable'

export function historyDeleteRefusalMessage(r: HistoryDeleteRefusal): string {
  const title = r.title || r.key
  switch (r.code) {
    case CRON_OWNERSHIP_UNKNOWN_CODE:
      return i18nT('pages.chatPage.could_not_delete_cron_ownership_unknown', { title })
    case CRON_STORE_BUSY_CODE:
      return i18nT('pages.chatPage.could_not_delete_cron_store_busy', { title })
    case CRON_STORE_UNREADABLE_CODE:
      return i18nT('pages.chatPage.could_not_delete_cron_store_unreadable', { title })
    default:
      return i18nT('pages.chatPage.could_not_delete_this_session', { title })
  }
}
