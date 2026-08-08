// Why a review failed, and how to run it again.
//
// The cause was already recorded on the run — it was just buried in the empty
// report's body text, so a failed review looked like a review that found nothing.
// This states the reason where the status is, and puts the retry next to it: a
// failed run's most likely next action is running it again.
import { AlertTriangle, Loader2, RotateCcw } from 'lucide-react'

import type { Run } from '../lib/types'
import { failureReason } from '../lib/format'

import { i18nT } from '../../../i18n/t'
export default function FailureNotice({
  run, changeId, onRetry, retrying = false,
}: {
  run: Run
  /** Prefer this change's cause; on a multi-PR run the run-level error may
   *  belong to a different one. */
  changeId?: string
  /** Omitted where the failed work cannot be re-dispatched from this surface. */
  onRetry?: () => void
  retrying?: boolean
}) {
  const reason = failureReason(run, changeId)
  if (!reason) return null

  return (
    <div className="rounded-lg border border-danger/50 bg-danger-subtle px-3.5 py-2.5">
      <div className="flex items-start gap-2">
        <AlertTriangle
          size={14}
          className="mt-0.5 flex-shrink-0 text-danger"
          aria-hidden="true"
        />
        <div className="flex-1 min-w-0">
          <div className="text-[12.5px] font-medium text-danger">
            {i18nT('apps.codeReviewSage.components.failureNotice.this_review_failed')}
          </div>
          <div className="mt-0.5 text-[12.5px] text-text leading-[1.6]">
            {reason.text}
          </div>
          {/* The driver's own wording, kept for a bug report. Only shown when it
              differs from the explanation, so it is not stated twice. */}
          {reason.raw && reason.raw !== reason.text && (
            <div className="mt-1 font-mono text-[11px] text-muted break-words">
              {reason.raw}
            </div>
          )}
        </div>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            disabled={retrying}
            className="flex-shrink-0 inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-[12px] text-text hover:text-accent hover:border-accent disabled:opacity-50 cursor-pointer disabled:cursor-default"
          >
            {retrying
              ? <Loader2 size={12} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              : <RotateCcw size={12} aria-hidden="true" />}
            {retrying
          ? i18nT('apps.codeReviewSage.components.failureNotice.starting')
          : i18nT('apps.codeReviewSage.components.failureNotice.run_it_again')}
          </button>
        )}
      </div>
    </div>
  )
}
