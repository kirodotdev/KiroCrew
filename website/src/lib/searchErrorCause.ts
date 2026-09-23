/**
 * Name WHY a file-search or listing request failed, as a cause the caller maps to
 * its own copy.
 *
 * Hoisted rather than spelled per surface: the @-menu and the folder panel classify
 * the SAME endpoint's refusals, so a divergence here would name one failure two
 * different things depending on which surface the user happened to be in.
 *
 * Keyed on the machine-readable `code`, never the human `error` string, which is
 * untranslated server text. An unrecognised cause degrades to `failed` rather than
 * leaking the raw reason, and a 403 carrying `authRequired` is a dashboard-session
 * expiry rather than a refusal of this path, so it must not claim the folder is off
 * limits.
 */
import { isDeadlineError } from '../api/queryClient'
import { ApiError } from '../api/apiError'
import { parseErrorCode } from '../utils/errorReport'

export type SearchErrorCause = 'timed_out' | 'failed' | 'denied' | 'root_missing'

const CAUSE_BY_CODE: Record<string, SearchErrorCause> = {
  access_denied: 'denied',
  project_not_found: 'root_missing',
  // The tree endpoint spells the same refusal with its own code, so it has to appear
  // here too -- degrading it to `failed` would call an unreachable root recoverable.
  unknown_project_dir: 'root_missing',
  // The listing endpoints' 400 for a path that resolves to a non-directory: as permanent
  // as a missing root, so it takes the same arm rather than a Refresh that cannot help.
  not_a_directory: 'root_missing',
}

/**
 * The notice copy for a LISTING failure, keyed by the same cause the search arm uses.
 *
 * `denied` and `root_missing` borrow the search arm's strings deliberately: a refusal is the same
 * fact whichever read hit it, and collapsing them to the generic key hid the reason.
 */
export const LISTING_FAILURE_KEYS: Record<SearchErrorCause, string> = {
  timed_out: 'pages.chat.folderPanel.listing_timed_out',
  failed: 'pages.chat.folderPanel.unable_to_list_folder',
  denied: 'pages.chat.folderPanel.search_denied',
  root_missing: 'pages.chat.folderPanel.search_root_missing',
}

/**
 * `named`, pointing at the surface's header Refresh as its remedy. One spelling for every
 * arm that Refresh can repair.
 *
 * Shared because the file rail and the folder panel both render the SAME failed
 * `['project-tree']` read, and each one's header Refresh refetches that key -- so the notice
 * has to name the remedy the same way on both, or one surface states a cause the other
 * pairs with a fix.
 */
export function namingRefresh(t: (key: string) => string, named: string): string {
  return `${named} — ${t('pages.chat.folderPanel.refresh_retries')}`
}

export function searchErrorCause(err: unknown): SearchErrorCause {
  // A deadline rejection is the one cause the client can name on its own: the walk was
  // still running, which is a different remedy from a gateway that answered with an error.
  if (isDeadlineError(err)) return 'timed_out'
  if (!(err instanceof ApiError) || err.authRequired) return 'failed'
  const code = parseErrorCode(err.body)
  return (code && CAUSE_BY_CODE[code]) || 'failed'
}
