// One thread's detail pane: what it is, how it is going, and its report.
//
// This is the panel that removes the artifact round-trip. While the run is live
// it shows real progress; the moment the report is written it renders here.
import { AlertTriangle, ScanSearch } from 'lucide-react'

import { useSage } from '../context'
import { effectiveRunStatus, relativeAge, typicalRunMs } from '../lib/format'
import { runIdentity } from '../lib/format'
import type { Run } from '../lib/types'
import EmptyState from './EmptyState'
import ReportView from './ReportView'
import RunProgress from './RunProgress'
import PostCommentsButton from './PostCommentsButton'
import FailureNotice from './FailureNotice'
import RunStatusPill from './RunStatusPill'
import ShimmerLine from './ShimmerLine'

import { i18nT } from '../../../i18n/t'
/** The report area's loading state: placeholder rows in the report's own shape. */
function ReportSkeleton() {
  return (
    <>
      <span className="sr-only" role="status">{i18nT('apps.codeReviewSage.components.runDetail.loading_report')}</span>
      <div aria-hidden="true" className="flex flex-col gap-3">
        {[0, 1, 2].map((i) => (
          <div key={i} className="rounded-lg border border-border bg-card p-3 flex flex-col gap-2">
            <ShimmerLine w="48%" delay={i * 0.08} />
            <ShimmerLine w="82%" delay={i * 0.08 + 0.05} />
            <ShimmerLine w="34%" delay={i * 0.08 + 0.1} />
          </div>
        ))}
      </div>
    </>
  )
}

function DetailHeader({ run }: { run: Run }) {
  const { startReview } = useSage()
  const { label, more } = runIdentity(run.repo, run.changes)
  return (
    <header className="px-6 pt-5 pb-4 border-b border-border flex-shrink-0">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="text-[22px] font-bold leading-tight text-text-strong break-words">
            {label}{more > 0 && (
              <span className="text-muted font-medium"> +{more} {i18nT('apps.codeReviewSage.components.runDetail.more')}</span>
            )}
          </h1>
          <div className="flex items-center gap-2 mt-2 flex-wrap text-[12.5px] text-muted">
            <RunStatusPill
              status={effectiveRunStatus(run)}
              cancelRequested={!!run.cancel_requested_at}
            />
            <span>
              {i18nT('apps.codeReviewSage.components.runDetail.pull_request', { count: run.changes.length })}
            </span>
            <span aria-hidden="true">·</span>
            <span title={run.started_at}>{i18nT('apps.codeReviewSage.components.runDetail.started')} {relativeAge(run.started_at)}</span>
            {run.finished_at && (
              <>
                <span aria-hidden="true">·</span>
                <span title={run.finished_at}>
                  {i18nT('apps.codeReviewSage.components.runDetail.finished')} {relativeAge(run.finished_at)}
                </span>
              </>
            )}
          </div>
        </div>
      {/* No delete control here. Deleting a run removes its directory and stored
          report with nothing to bring them back, so that action lives on the
          RunCard in the list, behind its two-step confirm. A second, unguarded
          trash icon on the same run in the detail header meant one stray click
          destroyed the review. */}
      </div>
      {/* The shared notice, so a failed run explains itself and offers the retry
          identically wherever it is read. */}
      <div className="mt-3">
        <FailureNotice
          run={run}
          onRetry={() => startReview.mutate(run.changes)}
          retrying={startReview.isPending}
        />
      </div>
      {!!run.skipped_inflight && (
        <div className="mt-3 text-[12px] text-muted leading-[1.5]">
          {/* The count, its noun and the verb all inflect together, so the
              whole sentence is one plural key. */}
          {i18nT('apps.codeReviewSage.components.runDetail.skipped_inflight', { count: run.skipped_inflight })}
        </div>
      )}
    </header>
  )
}

export default function RunDetail({ run }: { run: Run }) {
  const {
    pool, cancelRun, cancelling, report, reportLoading, reportError,
    archiveRun, archiving, archiveError, runs, postComments,
    postCommentGroups, posting, postError,
    postingSelection,
  } = useSage()
  const running = run.status === 'running'
  // Only the cards actually being sent may say so — see PrReviewDetail. A post
  // observed through the run's flag alone (another tab, or a reload mid-post)
  // cannot be attributed to comments, so it covers the whole review.
  const localKeys = postingSelection === undefined
    ? []
    : (postingSelection?.keys ?? null)
  const isPosting = (key: string): boolean => {
    if (localKeys === null) return true
    if (localKeys.length > 0) return localKeys.includes(key)
    return Boolean(run.posting)
  }

  return (
    <article className="h-full flex flex-col min-h-0">
      <DetailHeader run={run} />
      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-none px-6 py-5 flex flex-col gap-5">
        <RunProgress
          run={run}
          pool={pool}
          typicalMs={typicalRunMs(runs, run.changes.length)}
          onCancel={() => cancelRun(run.run_id)}
          cancelling={cancelling || !!run.cancel_requested_at}
        />

        {reportError ? (
          <div className="text-[12.5px] text-danger">{reportError.message}</div>
        ) : reportLoading ? (
          <ReportSkeleton />
        ) : report?.ready ? (
          <ReportView
            report={report}
            actions={(
              <PostCommentsButton
                run={run}
                report={report}
                onPost={() => postComments(run.run_id)}
                posting={posting || Boolean(run.posting)}
                error={run.post_error ?? postError?.message ?? null}
                postedKeys={run.posted_keys}
              />
            )}
            postedKeys={run.posted_keys}
          isPosting={isPosting}
          onPostFinding={(changeId, key) => postComments(run.run_id, {
            changeId, keys: [key],
          })}
          // One request per change: a request posts one pending review against
          // one pull request, so a selection spanning changes is grouped — and
          // the groups go out SEQUENTIALLY, because the backend refuses a second
          // post while one is in flight.
          onPostSelection={(groups) => postCommentGroups(run.run_id, groups)}
          onArchive={() => archiveRun(run.run_id)}
            archiving={archiving}
            archiveError={archiveError?.message ?? null}
          />
        ) : running ? (
          <EmptyState
            icon={ScanSearch}
            title={i18nT('apps.codeReviewSage.components.runDetail.report_appears_when_the_review_finishes')}
            hint={i18nT('apps.codeReviewSage.components.runDetail.you_can_leave_this_page_a_notification_will_tell')}
          />
        ) : (
          <EmptyState
            icon={AlertTriangle}
            title={i18nT('apps.codeReviewSage.components.runDetail.this_review_produced_no_report')}
            hint={run.status === 'cancelled'
              ? i18nT('apps.codeReviewSage.components.runDetail.cancelled_before_any_finished')
              : i18nT('apps.codeReviewSage.components.runDetail.nothing_to_report')}
          />
        )}
      </div>
    </article>
  )
}
