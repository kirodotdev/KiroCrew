/**
 * The words a worker-kit failure shows. The worker-kit routes refuse with a
 * machine `code` (`stale_revision`, `invalid_spec`, …) that is also their
 * `error`, so the raw message is an identifier, never a sentence. Each known
 * code reads as a localized sentence saying what to do; any other failure (an
 * owner-only refusal, a dropped connection) keeps the message the transport
 * already made readable.
 */
import { i18nT } from '../../i18n/t'
import { apiErrorCode } from '../../api/apiError'

// Literal keys, so the catalog gates can see each one. `action` is the label
// of the button that makes a preview, so the sentence names a real control.
const CODE_TEXT: Record<string, (action: string) => string> = {
  stale_revision: action => i18nT('crewWorkerKit.error_stale_revision', { action }),
  revision_required: action => i18nT('crewWorkerKit.error_revision_required', { action }),
  template_required: () => i18nT('crewWorkerKit.error_template_required'),
  template_unreadable: () => i18nT('crewWorkerKit.error_template_unreadable'),
  template_restrictions_uncarried: () => i18nT('crewWorkerKit.error_template_restrictions_uncarried'),
  invalid_spec: () => i18nT('crewWorkerKit.error_invalid_spec'),
  not_a_mate_file: () => i18nT('crewWorkerKit.error_not_a_mate_file'),
  invalid_json: () => i18nT('crewWorkerKit.error_invalid_request'),
  invalid_request: () => i18nT('crewWorkerKit.error_invalid_request'),
}

/**
 * The sentence a worker-kit failure shows, or `null` when there is none.
 * `previewAction` is the label the pane's preview button shows right now;
 * it defaults to the first-copy label.
 */
export function workerKitErrorText(err: unknown, previewAction?: string): string | null {
  if (!err) return null
  const code = apiErrorCode(err)
  if (Object.hasOwn(CODE_TEXT, code)) return CODE_TEXT[code](previewAction ?? i18nT('crewWorkerKit.copy'))
  return err instanceof Error ? err.message : String(err)
}
