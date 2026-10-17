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
 *
 * `DELETE /api/sessions` ("Delete all") answers 200 even when it left rows
 * behind, with `ok: false` and the shortfall in the body. Its refusal carries
 * `bulk` and renders through this same function, so a left-behind row reads
 * the same as when it was deleted on its own.
 */
import { i18nT } from '../i18n/t'
import type { ErrorReport } from './errorReport'

/** One row a delete left on disk: its key, its title, and the gateway's code. */
export type HistoryDeleteRow = {
  key: string
  title: string
  code: string
}

/** Raw refusal facts plus the exact API report used by the agent hand-off. */
export type HistoryDeleteRefusal = HistoryDeleteRow & {
  report?: ErrorReport
  /** Set only by the Older Sessions "Delete all". `left` is how many closed
   *  sessions stayed on disk (`failed` plus `undeletable` of the 200 body), or
   *  `null` when the request itself was refused and nothing was deleted.
   *  `cleared` is how many sessions the clear did delete (the 200 body's
   *  `cleared`), 0 for a refused request. `rows` are the `undeletable` entries,
   *  each with its own code; a `failed` row comes back with no id, so it is
   *  only counted. */
  bulk?: { left: number | null; cleared: number; rows: HistoryDeleteRow[] }
}

/** The row's cron jobs could not be determined (unreadable transcript metadata). */
export const CRON_OWNERSHIP_UNKNOWN_CODE = 'cron_ownership_unknown'
/** The cron store's lock stayed held past the gateway's bounded backoff. */
export const CRON_STORE_BUSY_CODE = 'cron_store_busy'
/** The cron store file could not be read at all. */
export const CRON_STORE_UNREADABLE_CODE = 'cron_store_unreadable'

/** How many left-behind rows a "Delete all" notice names; the count covers the rest. */
export const BULK_NAMED_ROWS = 3

/** Each part of a several-part "Delete all" notice gets its own line;
 *  `ErrorNotice` renders its message with `whitespace-pre-wrap`. */
const BULK_LINE_BREAK = '\n'

export function historyDeleteRefusalMessage(r: HistoryDeleteRefusal): string {
  if (r.bulk) return bulkDeleteRefusalMessage(r.code, r.bulk)
  return rowDeleteRefusalMessage(r)
}

/** A "Delete all" that was refused or left sessions behind. A refused request
 *  keeps the unreadable cron store's remedy, as the single delete does; any
 *  other refusal gets the generic sentence. One named row reads exactly like
 *  the single delete when only that row was in play (nothing else deleted).
 *  Otherwise a count comes first (with how many the clear did delete, when it
 *  deleted any), then the first few named rows' sentences, so
 *  each named row's remedy is still said, then one retry line when some left
 *  row is not named, each on its own line. */
function bulkDeleteRefusalMessage(
  code: string,
  { left, cleared, rows }: NonNullable<HistoryDeleteRefusal['bulk']>,
): string {
  if (!left) {
    if (code === CRON_STORE_UNREADABLE_CODE) {
      return i18nT('pages.chatPage.could_not_delete_closed_sessions_cron_store_unreadable')
    }
    return i18nT('pages.chatPage.could_not_delete_closed_sessions')
  }
  if (left === 1 && rows.length === 1 && cleared === 0) return rowDeleteRefusalMessage(rows[0])
  const named = rows.slice(0, BULK_NAMED_ROWS)
  const notDeleted = i18nT('pages.chatPage.closed_sessions_not_deleted', { count: left })
  const head = cleared > 0
    ? `${notDeleted} ${i18nT('pages.chatPage.older_sessions_deleted_count', { count: cleared })}`
    : notDeleted
  // Rows the named sentences do not cover (unnamed failures, rows past the cap)
  // get the retry line.
  return [
    head,
    ...named.map(rowDeleteRefusalMessage),
    ...(left > named.length ? [i18nT('pages.chatPage.retry_delete_all')] : []),
  ].join(BULK_LINE_BREAK)
}

function rowDeleteRefusalMessage(r: HistoryDeleteRow): string {
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
