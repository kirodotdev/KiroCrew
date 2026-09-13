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
}

export function searchErrorCause(err: unknown): SearchErrorCause {
  // A deadline rejection is the one cause the client can name on its own: the walk was
  // still running, which is a different remedy from a gateway that answered with an error.
  if (isDeadlineError(err)) return 'timed_out'
  if (!(err instanceof ApiError) || err.authRequired) return 'failed'
  const code = parseErrorCode(err.body)
  return (code && CAUSE_BY_CODE[code]) || 'failed'
}
