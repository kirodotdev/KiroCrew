// Why a review failed, and how to run it again.
//
// The cause was already recorded on the run — it was just buried in the empty
// report's body text, so a failed review looked like a review that found nothing.
// This states the reason where the status is, and puts the retry next to it: a
// failed run's most likely next action is running it again.
//
// The notice itself is the shared `ErrorNotice`, not a local re-implementation
// of its visuals: that is what carries the agent hand-off, and a failed review
// (a driver that could not clone, a model that refused, a `gh` that is signed
// out) is exactly the kind of failure the agent can diagnose from the recorded
// reason. Nothing on this surface is an unsaved draft — the run is persisted —
// so the hand-off is always on. The retry stays a sibling control, the same
// retry-vs-hand-off split as aws-control's `AwsErrorNotice`.
import { Loader2, RotateCcw } from 'lucide-react'

import ErrorNotice from '../../../components/ErrorNotice'
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
  // The driver's own wording rides INSIDE the notice, on its own line, when it
  // differs from the explanation: it is the diagnostic the agent needs, and a
  // sibling element would keep it out of the hand-off. The block variant
  // preserves the line break.
  const message = reason.raw && reason.raw !== reason.text
    ? `${reason.text}\n${reason.raw}`
    : reason.text

  return (
    <div className="flex flex-col items-start gap-2">
      <ErrorNotice
        title={i18nT('apps.codeReviewSage.components.failureNotice.this_review_failed')}
        message={message}
        askAgent
        className="w-full"
      />
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          disabled={retrying}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-[12px] text-text hover:text-accent hover:border-accent disabled:opacity-50 cursor-pointer disabled:cursor-default"
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
  )
}
