// "Post comments" — publish a reviewed run's findings to its pull request.
//
// Reviews are never posted automatically (``review.auto_post`` defaults off):
// writing to someone else's pull request is a side effect you opt into, not a
// consequence of running a review. This is that opt-in, taken after you have read
// the findings.
//
// Two clicks, deliberately. The post is a visible action on a shared artifact
// that this app cannot undo — the comments land as a GitHub review from your
// account. So the first click states the count and asks; only the second sends.
import { useEffect, useState } from 'react'
import { AlertTriangle, Check, Loader2, MessageSquarePlus } from 'lucide-react'

import { relativeAge } from '../lib/format'
import type { Run, RunReport } from '../lib/types'

import { i18nT } from '../../../i18n/t'
/** How many comments a post would publish.
 *
 * Mirrors the backend's ``posting_expected``: every must-fix and should-fix
 * finding becomes an
 * inline comment, plus one always-on ship-readiness comment per reviewed change.
 * Derived from the report the pane already has, so labelling the button honestly
 * costs no extra request — the endpoint recounts from the records before sending
 * and refuses a no-op, so this is a label, never the authority.
 *
 * Comments already on the pull request are subtracted. Findings can be posted
 * one at a time, and counting the whole report regardless made the confirm for
 * an external write overstate itself — offering to "post 6 comments" when four
 * were already sent and only two remained. */
export function pendingCommentCount(
  report: RunReport | null,
  postedKeys?: Record<string, string[]>,
): number {
  if (!report?.rows?.length) return 0
  return report.rows.reduce((n, row) => {
    const possible = (row.red ?? 0) + (row.yellow ?? 0) + 1
    const sent = postedKeys?.[row.change_id]?.length ?? 0
    return n + Math.max(0, possible - sent)
  }, 0)
}

export default function PostCommentsButton({
  run, report, onPost, posting = false, error = null, postedKeys,
  publishBarBelow = false,
}: {
  run: Run
  report: RunReport | null
  onPost: () => void
  posting?: boolean
  error?: string | null
  /** Comments already on the pull request, keyed by change id, so the count
   *  reflects what is left to send rather than the whole report. */
  postedKeys?: Record<string, string[]>
  /** Whether the publish bar is on this screen. Only the pull-request detail
   *  mounts it, so the run list must not tell the reader to "publish below"
   *  when there is nothing below to publish with. */
  publishBarBelow?: boolean
}) {
  const [confirming, setConfirming] = useState(false)
  const pending = pendingCommentCount(report, postedKeys)

  // Drop the confirm prompt if the run changes under it, so a click cannot land
  // on a different pull request than the one you were looking at.
  useEffect(() => setConfirming(false), [run.run_id])

  if (run.status === 'running') return null

  if (run.posted_at) {
    return (
      <span
        className="inline-flex items-center gap-1.5 text-[12.5px] text-ok"
        title={i18nT('apps.codeReviewSage.components.postCommentsButton.posted_age',
          { age: relativeAge(run.posted_at) })}
      >
        <Check size={13} aria-hidden="true" />
        {/* One key, not "Posted" + number + "comment(s)": a translator cannot
            reorder three sibling fragments, and several languages need the count
            in a different position. Which key depends on whether the publish bar
            is on this screen — pointing "below" at a screen without it sends the
            reader looking for a control that is on the pull request's own page. */}
        {i18nT(publishBarBelow
          ? 'apps.codeReviewSage.components.postCommentsButton.posted_comments'
          : 'apps.codeReviewSage.components.postCommentsButton.drafted_publish_on_pr_page',
        { count: run.posted_comments ?? 0 })}
      </span>
    )
  }

  if (posting) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[12.5px] text-muted">
        <Loader2 size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
        {i18nT('apps.codeReviewSage.components.postCommentsButton.posting')}
      </span>
    )
  }

  // Nothing was flagged: there is no comment to write, so offering the action
  // would be a dead end.
  if (pending === 0) return null

  if (confirming) {
    return (
      <span className="inline-flex items-center gap-2 text-[12.5px]">
        <span className="text-muted">
          {i18nT('apps.codeReviewSage.components.postCommentsButton.confirm_post',
            { count: pending })}
        </span>
        <button
          type="button"
          onClick={() => { setConfirming(false); onPost() }}
          className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2.5 py-1 text-[12.5px] font-medium text-accent hover:bg-accent/20 cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.postCommentsButton.post_action')}
        </button>
        <button
          type="button"
          onClick={() => setConfirming(false)}
          className="rounded-md bg-transparent px-1.5 py-1 text-[12.5px] text-muted hover:text-text cursor-pointer"
        >
          {i18nT('apps.codeReviewSage.components.postCommentsButton.cancel')}
        </button>
      </span>
    )
  }

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span className="inline-flex items-center gap-2">
      <button
        type="button"
        onClick={() => setConfirming(true)}
        title={i18nT('apps.codeReviewSage.components.postCommentsButton.publish_these_findings_as_a_review_on_the_pull_r')}
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-[12.5px] text-text hover:text-accent hover:border-accent cursor-pointer"
      >
        <MessageSquarePlus size={13} aria-hidden="true" />
        {i18nT('apps.codeReviewSage.components.postCommentsButton.post_count', { count: pending })}
      </button>
      </span>
      {/* The reason is rendered, not just tooltipped: a refused post on the app's
          headline action left keyboard and touch users with no way to see why, and
          nothing to act on. Kept as its own line rather than appended to the label,
          so no sentence is assembled from a translated fragment plus provider text. */}
      {error && (
        <span className="inline-flex items-start gap-1 max-w-[42ch] text-[12px] text-danger text-right">
          <AlertTriangle size={12} aria-hidden="true" className="mt-[2px] flex-shrink-0" />
          <span className="flex flex-col gap-0.5">
            <span>{i18nT('apps.codeReviewSage.components.postCommentsButton.post_failed')}</span>
            <span className="break-words font-normal opacity-90">{error}</span>
          </span>
        </span>
      )}
    </span>
  )
}
