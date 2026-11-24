/**
 * The words a failure of a Bot's worker-kit or capabilities routes shows (Add
 * abilities, and the Abilities pane's load and Remove errors). Both refuse with
 * a machine `code` (`stale_revision`, `invalid_spec`, …) that is also their
 * `error`, so the raw message is an identifier, never a sentence. Each known
 * code reads as a localized sentence saying what to do; any other failure (an
 * owner-only refusal, a dropped connection) keeps the message the transport
 * already made readable. A refusal about one field or rule carries it as
 * `detail`, and the sentence names it.
 */
import { i18nT } from '../../i18n/t'
import { ApiError, apiErrorCode } from '../../api/apiError'
import { isDeadlineError } from '../../api/queryClient'

// Literal keys, so the catalog gates can see each one.
const CODE_TEXT: Record<string, (detail: string) => string> = {
  stale_revision: () => i18nT('crewWorkerKit.error_stale_revision'),
  revision_required: () => i18nT('crewWorkerKit.error_revision_required'),
  template_required: () => i18nT('crewWorkerKit.error_template_required'),
  template_unreadable: () => i18nT('crewWorkerKit.error_template_unreadable'),
  template_restrictions_uncarried: detail => detail
    ? i18nT('crewWorkerKit.error_template_restrictions_uncarried_named', { fields: detail })
    : i18nT('crewWorkerKit.error_template_restrictions_uncarried'),
  template_policy_backend_unsupported: detail => i18nT('crewWorkerKit.error_template_policy_backend', { fields: detail }),
  invalid_spec: () => i18nT('crewWorkerKit.error_invalid_spec'),
  not_a_mate_file: () => i18nT('crewWorkerKit.error_not_a_mate_file'),
  invalid_json: () => i18nT('crewWorkerKit.error_invalid_request'),
  invalid_request: () => i18nT('crewWorkerKit.error_invalid_request'),
  // The GET's read of the crewmate's file did not finish in time: the same
  // load failure the Abilities pane above it names.
  capabilities_unavailable: () => i18nT('crewEditorNext.load_error'),
}

/** A string field of a refusal body (`detail`, `reason`), or `''`. */
function refusalField(err: unknown, field: 'detail' | 'reason'): string {
  if (!(err instanceof ApiError)) return ''
  try {
    const parsed = JSON.parse(err.body) as unknown
    const value = parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>)[field] : undefined
    return typeof value === 'string' ? value : ''
  } catch {
    return ''
  }
}

/** The sentence a worker-kit failure shows, or `null` when there is none. */
export function workerKitErrorText(err: unknown): string | null {
  if (!err) return null
  // A read past either deadline: the dashboard's own (the untranslated
  // `deadline exceeded`) or the gateway's (`reason: timed_out`). Both read as
  // the same load failure, with its likely causes.
  if (isDeadlineError(err) || refusalField(err, 'reason') === 'timed_out') {
    return i18nT('crewEditorNext.load_error_timed_out')
  }
  const code = apiErrorCode(err)
  if (Object.hasOwn(CODE_TEXT, code)) return CODE_TEXT[code](refusalField(err, 'detail'))
  return err instanceof Error ? err.message : String(err)
}
