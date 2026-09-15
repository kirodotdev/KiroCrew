import { i18nT } from '../../i18n/t'
import { fmtList } from '../../i18n/format'
import { embedModelErrorMessage } from './EmbeddingModelCard'

/** DOM id of the Embedding Model card's path field.
 *
 * Shared because two cards agree on it: the Embedding Model card gives its
 * `<Input>` this id, and the Vector Memory card links `#<id>` from its legacy
 * warning and from its path-error pointer, and checks the DOM for it before
 * rendering that pointer. One constant keeps a rename from silently producing a
 * dead fragment. */
export const EMBED_MODEL_PATH_ID = 'embed-model-path'

export interface EmbeddingSetupFields {
  setup_error?: string
  setup_error_code?: string
  setup_error_params?: { path?: string; error?: string }
  setup_warning?: string
  setup_warning_code?: string
  setup_warning_params?: Record<string, string>
  model_active?: boolean
  repair?: {
    generation?: string
    scope?: string
    pending_invalidation?: number
    pending_vectors?: number
    deferred_stores?: number
    unknown_scope?: boolean
  }
}

/** True for every backend code that means "the configured model path is unusable".
 *
 * These are the codes a user can fix from the Embedding Model card by editing
 * the path, so the card gates Apply on them and the legacy-vectors warning
 * swaps its "reapply" clause for "fix the path first". */
export function isModelPathErrorCode(code: string | undefined): boolean {
  return !!code && code.startsWith('model_path_')
}

export function embeddingSetupWarning(status: EmbeddingSetupFields | null): string {
  if (status?.setup_warning_code === 'legacy_embedding_vectors') {
    // "Reapply it" is a dead end while the file the path names is missing: the
    // Apply button would submit the very path the error is about. Point at the
    // path fix first, then the reapply.
    if (isModelPathErrorCode(status.setup_error_code)) {
      return i18nT('pages.overview.vectorMemoryCard.legacy_vectors_warning_path_error')
    }
    return i18nT('pages.overview.vectorMemoryCard.legacy_vectors_warning')
  }
  return status?.setup_warning || ''
}

/** Localized, actionable notice for a coded setup error.
 *
 * Known codes render fully localized copy with a next step and never
 * interpolate the backend's English exception text — that text stays available
 * through {@link embeddingSetupDiagnostic} for a collapsed details block.
 * Missing and unknown codes fall back to the diagnostic prose so a new backend
 * code is never silently swallowed. */
export function embeddingSetupError(status: EmbeddingSetupFields | null): string {
  if (!status) return ''
  const code = status.setup_error_code
  const path = status.setup_error_params?.path || ''
  const error = status.setup_error_params?.error ?? status.setup_error ?? ''
  switch (code) {
    case 'model_identity_unverified':
      return i18nT('pages.overview.vectorMemoryCard.verification_pending')
    case 'model_verification_failed':
      return i18nT('pages.overview.vectorMemoryCard.verification_failed', { path })
    case 'model_download_failed':
      return i18nT('pages.overview.vectorMemoryCard.download_error_detail')
    case 'model_path_not_absolute':
    case 'model_path_not_found':
    case 'model_path_not_a_file':
    case 'model_path_too_small':
    case 'model_path_protected':
    case 'model_path_unreadable': {
      const message = embedModelErrorMessage({ code, error })
      return path ? i18nT('pages.overview.vectorMemoryCard.model_error_path', { message, path }) : message
    }
    default: return status.setup_error || ''
  }
}

/** Short pointer for a model path error, or '' for every other state.
 *
 * The Embedding Model card already prints the localized message under its path
 * field, and the field itself shows the path, so a second card repeating both is
 * the same fault reported twice. When that field is on the page, the Vector
 * Memory card renders this pointer instead: what the fault costs the user
 * (keyword search meanwhile). It carries neither the message nor the path, so
 * the field stays the one place the fault is stated, and it does not name the
 * destination either: the card renders the "Open embedding model settings"
 * link right beside it, and that link is where the fix lives, said once.
 * Non-path codes have no field to point at and keep their full body. */
export function embeddingSetupErrorPointer(status: EmbeddingSetupFields | null): string {
  if (!status || !isModelPathErrorCode(status.setup_error_code)) return ''
  return i18nT('pages.overview.vectorMemoryCard.model_error_path_pointer')
}

/** The raw backend exception text behind a known-code notice, or ''.
 *
 * Only the two codes whose localized body dropped it: verification and download
 * failures carry an OSError / downloader message that a log reader needs and a
 * user does not. Path codes already say everything the raw text says, and an
 * unknown code renders its prose as the body, so neither needs a second copy. */
export function embeddingSetupDiagnostic(status: EmbeddingSetupFields | null): string {
  if (!status) return ''
  const code = status.setup_error_code
  if (code !== 'model_verification_failed' && code !== 'model_download_failed') return ''
  return status.setup_error_params?.error ?? status.setup_error ?? ''
}

/** User-vocabulary summary of the standing rebuild, or '' when nothing is pending.
 *
 * Three counts, three meanings, never summed: `pending_vectors` is memory
 * entries still waiting for a new vector, `pending_invalidation` is open STORES
 * that still hold old vectors to clear, `deferred_stores` is closed or
 * unavailable stores that are rebuilt when they next open. A state where only
 * the invalidation count is non-zero is still pending and still rendered.
 *
 * Only the non-zero units are named: "0 open stores still hold old vectors" is
 * not progress the user asked about, and the API reports every unit whether or
 * not it applies. The surviving clauses are joined by `fmtList`, so the
 * separators and the conjunction come from the active locale (`A, B, and C` in
 * English, `A、B和C` in Chinese) instead of a fixed three-slot sentence that
 * left a dangling ", and" behind an omitted unit.
 *
 * The sentence names its scope ("across all memory stores"): the backend
 * walks every active store (open handles are counted, closed ones are
 * deferred), while the Vector Memory card's tiles count only the store shown,
 * so without the scope a reader cannot reconcile "7 memories" with tiles that
 * add up to 5. */
export function embeddingRepairMessage(status: EmbeddingSetupFields | null): string {
  const repair = status?.repair
  if (!repair?.generation) return ''
  if (repair.unknown_scope) return i18nT('pages.overview.vectorMemoryCard.repair_unknown')
  const invalidation = repair.pending_invalidation ?? 0
  const vectors = repair.pending_vectors ?? 0
  const deferred = repair.deferred_stores ?? 0
  if (!invalidation && !vectors && !deferred) return ''
  const items = [
    vectors ? i18nT('pages.overview.vectorMemoryCard.repair_vectors', { count: vectors }) : '',
    invalidation ? i18nT('pages.overview.vectorMemoryCard.repair_invalidation', { count: invalidation }) : '',
    deferred ? i18nT('pages.overview.vectorMemoryCard.repair_deferred', { count: deferred }) : '',
  ]
  return i18nT('pages.overview.vectorMemoryCard.repair_pending', { items: fmtList(items) })
}

/** Status tokens are protocol values, never user-facing fallback copy. */
export function embeddingSetupStepLabel(step: string, attempt = 0): string {
  switch (step) {
    case 'idle':
    case 'checking': return i18nT('pages.overview.vectorMemoryCard.checking_system_status')
    case 'downloading': return i18nT('pages.overview.vectorMemoryCard.downloading_embedding_model_610mb')
    case 'installing_faiss': return i18nT('pages.overview.vectorMemoryCard.model_loaded_embedding_engine_is_starting_up')
    case 'verifying': return i18nT('pages.overview.vectorMemoryCard.verifying_model_integrity')
    case 'waiting_retry': return i18nT('pages.overview.vectorMemoryCard.retrying_download', { attempt })
    case 'ready':
    case 'done': return i18nT('pages.overview.vectorMemoryCard.ready')
    case 'failed':
    case 'error': return i18nT('pages.overview.vectorMemoryCard.setup_failed')
    default: return i18nT('pages.overview.vectorMemoryCard.model_loading')
  }
}
