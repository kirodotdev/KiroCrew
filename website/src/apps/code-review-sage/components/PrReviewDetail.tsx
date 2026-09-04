// One pull request, as tabs: Sage Review · Description · Comments · Checks.
//
// Sage Review leads because it is what this app is for; the other three are the
// context you need to interpret it, which previously meant leaving for GitHub.
// They are tabs rather than stacked sections because the description alone can
// run for pages — stacked, the review ended up below the fold on exactly the
// PRs where it matters most.
//
// Every tab reads ONE query (see usePrSource), so switching is free and no tab
// triggers its own provider call.
import {
  ExternalLink, FileText, GitPullRequest, Loader2, ScanSearch,
} from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'

import { sageApi } from '../api'
import { useSage } from '../context'
import { changeKey, effectiveRunStatus, relativeAge, sageSourceLink, typicalRunMs } from '../lib/format'
import { REVIEW_MODEL_AUTO, type PrRef, type ReviewFixFindingSnapshot, type RunReport } from '../lib/types'
import EmptyState from './EmptyState'
import { SourceError, usePrSource } from './PrSourcePanel'
import ReportView from './ReportView'
import DraftReviewActions from './DraftReviewActions'
import FailureNotice from './FailureNotice'
import PrStatusChips from './PrStatusChips'
import ReviewChat from './ReviewChat'
import RunProgress from './RunProgress'
import PostCommentsButton from './PostCommentsButton'
import ReviewModelPicker from './ReviewModelPicker'
import RunStatusPill from './RunStatusPill'
import ShimmerLine from './ShimmerLine'
import { api } from '../../../api/client'
import PullRequestPanel from '../../../components/PullRequestPanel'
import ReviewFixTaskPanel, { type ReviewFixTaskTransport } from '../../../components/ReviewFixTaskPanel'
import ReviewFixSetup from './ReviewFixSetup'

import { i18nT } from '../../../i18n/t'
type Tab = 'review' | 'source'

const reviewFixTransport: ReviewFixTaskTransport = {
  status: (taskId) => api.reviewFixStatus(taskId),
  action: (taskId, input) => api.reviewFixAction(taskId, input),
}

/** http(s) only: the URL is provider-derived and lands in an href. */
function safeHref(url: string): string {
  try {
    return ['http:', 'https:'].includes(new URL(url).protocol) ? url : '#'
  } catch {
    return '#'
  }
}

function ReportSkeleton() {
  return (
    <>
      <span className="sr-only" role="status">{i18nT('apps.codeReviewSage.components.prReviewDetail.loading_review')}</span>
      <div aria-hidden="true" className="flex flex-col gap-3">
        {[0, 1].map((i) => (
          <div key={i} className="rounded-lg border border-border bg-card p-3 flex flex-col gap-2">
            <ShimmerLine w="46%" delay={i * 0.08} />
            <ShimmerLine w="80%" delay={i * 0.08 + 0.05} />
          </div>
        ))}
      </div>
    </>
  )
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-[11px] uppercase tracking-wider text-muted">{label}</span>
      <span className="text-[12.5px] text-text">{value}</span>
    </span>
  )
}

function TabButton({
  label, icon: Icon, active, badge, badgeTone = 'muted', onClick,
}: {
  label: string
  icon: typeof FileText
  active: boolean
  badge?: ReactNode
  /** `alert` colours the badge when it represents something wrong (failing CI). */
  badgeTone?: 'muted' | 'alert' | 'accent'
  onClick: () => void
}) {
  const tone = badgeTone === 'alert'
    ? 'bg-bg-elevated text-danger'
    : badgeTone === 'accent'
      ? 'bg-accent-subtle text-accent'
      : 'bg-bg-elevated text-muted'
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-[12.5px] font-medium rounded-md cursor-pointer transition-colors ${
        active
          ? 'bg-bg-elevated text-text border border-border'
          : 'bg-transparent text-muted border border-transparent hover:text-text'
      }`}
    >
      <Icon size={13} aria-hidden="true" />
      {label}
      {badge !== undefined && badge !== null && (
        <span className={`text-[10.5px] px-1.5 py-0.5 rounded-full ${tone}`}>{badge}</span>
      )}
    </button>
  )
}

export default function PrReviewDetail({ pr }: { pr: PrRef }) {
  const {
    prRun, report, reportLoading, reportError,
    startReview, cancelRun, cancelling, pool,
    archiveRun, archiving, archiveError, runs, postComments, postCommentGroups,
    posting, postError, postingSelection, reviewModel, setReviewModel,
  } = useSage()
  const [tab, setTab] = useState<Tab>('review')
  const [fixFindings, setFixFindings] = useState<ReviewFixFindingSnapshot[]>([])
  const [fixSetupOpen, setFixSetupOpen] = useState(false)
  const [fixTaskId, setFixTaskId] = useState<string | null>(null)
  const source = usePrSource(pr.url)

  const startFix = (findingsToFix: ReviewFixFindingSnapshot[]) => {
    if (findingsToFix.length === 0) return
    setFixFindings(findingsToFix)
    setFixSetupOpen(true)
  }

  const reviewAgain = async () => {
    if (!fixTaskId) return
    const current = await sageApi.fixTask(fixTaskId)
    if (!current.review_fix) return
    await sageApi.reviewAgain(fixTaskId, {
      expected_revision: current.revision,
      target_fingerprint: current.review_fix.target.dirty_fingerprint,
    })
  }

  const running = prRun?.status === 'running'
  // The per-change error the driver wrote for THIS pull request, if it failed.
  const changeEntry = prRun?.progress?.[pr.change_id]
  const changeError = changeEntry?.phase === 'failed'
    ? (changeEntry.error || '').trim() || null
    : null
  const busy = startReview.isPending
  // Which cards may claim they are posting. A post started elsewhere (another
  // tab, or a reload mid-post) arrives only as the run's `posting` flag and
  // cannot be attributed to comments, so it covers the whole review; a local
  // per-finding post covers exactly the keys it sent.
  const localKeys = postingSelection === undefined
    ? []
    : (postingSelection?.keys ?? null)
  const isPosting = (key: string): boolean => {
    if (localKeys === null) return true
    if (localKeys.length > 0) return localKeys.includes(key)
    return Boolean(prRun?.posting)
  }
  // A run can cover many PRs; this pane shows only this one's findings. Scope by
  // change URL rather than `change_id` for the same reason the run lookup does:
  // ids collapse across repos, so an id filter can pull a colliding PR's rows
  // into this PR's report. Redaction leaves a real URL byte-identical, so the
  // key still matches after the rows are scrubbed.
  const scoped: RunReport | null = useMemo(() => {
    if (!report?.ready) return null
    const want = changeKey(pr.url)
    const rows = report.rows.filter((r) => changeKey(r.url) === want)
    const bands = { red: 0, yellow: 0, green: 0 }
    for (const r of rows) bands[r.band] += 1
    return { ...report, rows, bands, total: rows.length }
  }, [report, pr.url])

  const src = source.data
  // A PR opened from the thread list carries only a URL + change id, so every
  // display field falls back to the provider fetch. Same render either way.
  const title = pr.title || src?.title || `#${pr.number}`
  const author = pr.author || src?.author || ''
  const updatedAt = pr.updated_at || src?.updatedAt || ''
  const headSha = pr.head_sha || src?.headSha || ''
  // A failed run's action is a retry, not a first review: "Review" on a run that
  // just failed reads as though nothing had been attempted.
  const failed = !!prRun && effectiveRunStatus(prRun) === 'error'
  const isDraft = pr.draft ?? src?.draft ?? false
  const failing = (src?.checks ?? []).filter((c) => c.bucket === 'failed').length
  const findings = (scoped?.bands.red ?? 0) + (scoped?.bands.yellow ?? 0)

  const reviewBody = (
    <>
      {/* Above the progress block: a failed run's reason and its retry are the
          first things you need, not a footnote under an empty report. */}
      {prRun && (
        <FailureNotice
          run={prRun}
          changeId={pr.change_id}
          onRetry={() => startReview.mutate({
            changes: [pr.url],
            model: prRun?.model ?? REVIEW_MODEL_AUTO,
          })}
          retrying={startReview.isPending}
        />
      )}
      {prRun && (
        <RunProgress
          run={prRun}
          pool={pool}
          typicalMs={typicalRunMs(runs, prRun.changes.length)}
          onCancel={() => cancelRun(prRun.run_id)}
          cancelling={cancelling || Boolean(prRun.cancel_requested_at)}
        />
      )}
      {fixSetupOpen && prRun && (
        <ReviewFixSetup
          findings={fixFindings}
          reviewRunId={prRun.run_id}
          prUrl={pr.url}
          sourceHeadSha={headSha}
          model={reviewModel}
          onModelChange={setReviewModel}
          onCreated={(response) => {
            setFixTaskId(response.task_id)
            setFixSetupOpen(false)
          }}
          onClose={() => setFixSetupOpen(false)}
        />
      )}
      {fixTaskId && (
        <ReviewFixTaskPanel
          taskId={fixTaskId}
          transport={reviewFixTransport}
          onReviewAgain={reviewAgain}
          onOpenTaskRunner={(taskId) => {
            window.location.assign(`/projects?applied=${encodeURIComponent(taskId)}`)
          }}
        />
      )}

      {!prRun ? (
        <EmptyState
          icon={ScanSearch}
          title={i18nT('apps.codeReviewSage.components.prReviewDetail.this_pull_request_has_not_been_reviewed_yet')}
          hint={i18nT('apps.codeReviewSage.components.prReviewDetail.start_a_review_and_its_findings_appear_here_you')}
        />
      ) : reportError ? (
        <div className="text-[12.5px] text-danger">{reportError.message}</div>
      ) : reportLoading ? (
        <ReportSkeleton />
      ) : scoped && scoped.rows.length > 0 ? (
        <ReportView
          report={scoped}
          // The report is where you decide the findings are worth sending, so the
          // post control lives with it — and it is scoped to THIS pull request.
          actions={(
            <PostCommentsButton
              run={prRun}
              report={scoped}
              // The publish bar is mounted on this screen (and only here), so the
              // drafted line may point at it.
              publishBarBelow
              // Scoped to THIS pull request. Without the change id the
              // backend loops over every change in the run, so posting from
              // one PR detail published comments to every PR a repo run
              // covered.
              onPost={() => postComments(prRun.run_id,
                { changeId: pr.change_id })}
              posting={posting || Boolean(prRun.posting)}
              error={prRun.post_error ?? postError?.message ?? null}
              postedKeys={prRun.posted_keys}
            />
          )}
          postedKeys={prRun.posted_keys}
          isPosting={isPosting}
          onPostFinding={(changeId, key) => postComments(prRun.run_id, {
            changeId, keys: [key],
          })}
          // One request per change: a request posts one pending review against
          // one pull request, so a selection spanning changes is grouped.
          onPostSelection={(groups) => postCommentGroups(prRun.run_id, groups)}
          onStartFix={startFix}
          onArchive={() => archiveRun(prRun.run_id)}
          archiving={archiving}
          archiveError={archiveError?.message ?? null}
        />
      ) : running ? (
        // Deliberately NOT another "running" heading: the progress block above is
        // the status. This says only what the user cannot already see.
        <div className="text-[12.5px] text-muted leading-[1.6]">
          {i18nT('apps.codeReviewSage.components.prReviewDetail.findings_appear_here_as_soon_as_the_review_finis')}
        </div>
      ) : report?.ready ? (
        <EmptyState
          icon={GitPullRequest}
          title={i18nT('apps.codeReviewSage.components.prReviewDetail.nothing_flagged_on_this_pull_request')}
          hint={i18nT('apps.codeReviewSage.components.prReviewDetail.review_completed_with_no_findings')}
        />
      ) : (
        <EmptyState
          icon={GitPullRequest}
          title={i18nT('apps.codeReviewSage.components.prReviewDetail.that_review_produced_no_report_for_this_pull_req')}
          // The driver records WHY a change failed, so say it rather than making
          // the user guess whether this is a bug or a clean review.
          // The reason and the retry are in the notice above, so this only says
          // what that notice does not.
          hint={prRun.status === 'cancelled'
            ? i18nT('apps.codeReviewSage.components.prReviewDetail.cancelled_before_finishing')
            : changeError
              ? i18nT('apps.codeReviewSage.components.prReviewDetail.see_the_reason_above')
              : i18nT('apps.codeReviewSage.components.prReviewDetail.did_not_complete_for_this_change')}
        />
      )}
      {/* Questions about the findings sit between reading them and sending
          them: "why is this a problem?" is what you ask before deciding whether
          to post it. The panel is collapsed by default and only offers a composer
          while the reviewer that produced these findings is still loaded. */}
      {prRun && (
        <ReviewChat runId={prRun.run_id} changeId={pr.change_id} />
      )}
      {/* Posting the findings above leaves a PENDING review -- a draft only its
          author can see. This releases it with a verdict, so the whole loop
          (review -> post -> publish) closes here instead of on github.com.

          The gate is DELIVERY EVIDENCE from this run, not merely that the review
          finished. Posting is deliberate and `review.auto_post` defaults to false,
          so a rerun reaches `done` having posted nothing -- and the draft GitHub
          still holds is the PREVIOUS run's. Publishing then submits obsolete
          findings irreversibly, which neither of the other defences catches: the
          content digest binds "what you saw" to "what you send" and both are that
          same stale draft, and the stale-head check never fires because
          re-reviewing does not move head.sha.

          `posted_keys[change_id]` is written by the backend when it posts FOR THIS
          CHANGE, so a non-empty list is proof this run delivered a draft. It is NOT
          proof that the draft pending right now is that one: a later run replaces the
          draft, and publishing from this run's view would then send the other run's
          findings. `posted_review_ids[change_id]` is the id of the draft this run
          actually created, and the publish bar refuses unless the pending draft still
          carries it. Absent or empty reads as "not delivered" -- fail-closed. */}
      {prRun && (
        <DraftReviewActions
          url={pr.url}
          draftDelivered={(prRun.posted_keys?.[pr.change_id]?.length ?? 0) > 0}
          expectedReviewId={prRun.posted_review_ids?.[pr.change_id] ?? ''}
        />
      )}
    </>
  )

  /** The provider-backed view of the pull request itself.
   *
   *  This is the SHARED `PullRequestPanel` -- the same component the chat window
   *  renders -- rather than a Sage-local reimplementation. Sage's job is the
   *  review layer (findings, bands, ship summary, publish); what GitHub says
   *  about a pull request is one surface and belongs in one component. Reusing
   *  it also brings the two sections a Sage-local pane never had: the
   *  syntax-highlighted file diff and the commit list.
   *
   *  `onAddToChat` is deliberately omitted: there is no composer here, and the
   *  panel hides its chat-handoff affordances when the callback is absent. */
  const sourceBody = () => {
    if (source.isLoading) {
      return <div className="text-[12.5px] text-muted">{i18nT('apps.codeReviewSage.components.prReviewDetail.loading_pull_request_details')}</div>
    }
    if (source.error) return <SourceError error={source.error as Error} />
    if (!src) return null
    const link = sageSourceLink(pr.url)
    if (!link) return <SourceError error={new Error(pr.url)} />
    return (
      // The panel manages its own height; give it a definite one to scroll in.
      <div className="flex-1 min-h-0 -mx-6 -my-5">
        <PullRequestPanel
          sources={[link]}
          selectedUrl={pr.url}
          // One pull request, and the left rail already chose it, so selecting
          // the only tab is a no-op rather than a state change to thread.
          onSelect={() => {}}
        />
      </div>
    )
  }

  return (
    <article className="h-full flex flex-col min-h-0">
      <header className="px-4 md:px-6 pt-5 pb-3 border-b border-border flex-shrink-0">
        {/* Stack under the title on narrow viewports: a non-shrinking action
            column beside a long advertised model id forced the review content
            to overflow horizontally instead of wrapping. */}
        <div className="flex flex-col sm:flex-row items-start sm:items-start gap-3">
          <div className="flex-1 min-w-0 w-full">
            <h1 className="text-[21px] font-bold leading-tight text-text-strong break-words">
              {title}
            </h1>
            <div className="flex items-center gap-2.5 mt-2 flex-wrap text-[12.5px] text-muted">
              <a
                href={safeHref(pr.url)}
                target="_blank"
                rel="noreferrer"
                title={i18nT('apps.codeReviewSage.components.prReviewDetail.open_on_github')}
                className="inline-flex items-center gap-1 font-mono text-muted hover:text-accent hover:underline"
              >
                #{pr.number} <ExternalLink size={11} aria-hidden="true" />
              </a>
              {author && <Meta label={i18nT('apps.codeReviewSage.components.prReviewDetail.author')} value={author} />}
              {updatedAt && <Meta label={i18nT('apps.codeReviewSage.components.prReviewDetail.updated')} value={relativeAge(updatedAt)} />}
              {headSha && <Meta label={i18nT('apps.codeReviewSage.components.prReviewDetail.head')} value={headSha.slice(0, 7)} />}
              {isDraft && (
                <span className="text-[10.5px] px-1.5 py-0.5 rounded-full bg-bg-elevated border border-border">
                  {i18nT('apps.codeReviewSage.components.prReviewDetail.draft')}
                </span>
              )}
              {/* The PULL REQUEST's own status. Previously the header described
                  the review but not the thing being reviewed, so a merged or
                  conflicting PR looked like a healthy open one. */}
              <PrStatusChips src={src} />
              {/* Terminal states only: while running, the action button already
                  reads "Reviewing…" and the progress block carries the detail —
                  a pill here made three signals say one thing. */}
              {prRun && prRun.status !== 'running' && (
                <RunStatusPill
                  status={effectiveRunStatus(prRun)}
                  cancelRequested={Boolean(prRun.cancel_requested_at)}
                />
              )}
            </div>
          </div>
          <div className="flex w-full sm:w-auto min-w-0 flex-col items-stretch sm:items-end gap-2">
            <button
              type="button"
              onClick={() => {
                if (failed && prRun) {
                  startReview.mutate({
                    changes: [pr.url],
                    model: prRun.model ?? REVIEW_MODEL_AUTO,
                  })
                  return
                }
                startReview.mutate([pr.url])
              }}
              disabled={busy || running}
              title={running
                ? i18nT('apps.codeReviewSage.components.prReviewDetail.review_already_running')
                : pr.reviewed
                  ? i18nT('apps.codeReviewSage.components.prReviewDetail.review_again_at_current_head')
                  : i18nT('apps.codeReviewSage.components.prReviewDetail.review_this_pull_request')}
              className="inline-flex items-center gap-1.5 rounded-md bg-accent text-accent-fg px-3 py-1.5 text-[12.5px] font-medium border-none cursor-pointer hover:bg-accent-hover disabled:opacity-40 disabled:cursor-default transition-colors"
            >
              {busy
                ? <Loader2 size={13} className="animate-spin motion-reduce:animate-none" />
                : <ScanSearch size={13} />}
              {running
                ? i18nT('apps.codeReviewSage.components.prReviewDetail.reviewing')
                : failed
                  ? i18nT('apps.codeReviewSage.components.prReviewDetail.retry_review')
                  : pr.reviewed || pr.reviewed_stale
                    ? i18nT('apps.codeReviewSage.components.prReviewDetail.review_again')
                    : i18nT('apps.codeReviewSage.components.prReviewDetail.review')}
            </button>
            <ReviewModelPicker
              value={reviewModel}
              onChange={setReviewModel}
              disabled={busy || running}
            />
          </div>
        </div>
        {startReview.error && (
          <div className="mt-2 text-[12.5px] text-danger">
            {(startReview.error as Error).message}
          </div>
        )}

        <div
          role="tablist"
          aria-label={i18nT('apps.codeReviewSage.components.prReviewDetail.pull_request_detail')}
          className="flex items-center gap-1 mt-3 flex-wrap"
        >
          <TabButton
            label={i18nT('apps.codeReviewSage.components.prReviewDetail.sage_review')}
            icon={ScanSearch}
            active={tab === 'review'}
            // A dot, not the word: "running" is already on the action button.
            // The dot exists so an in-flight review is visible from another tab.
            badge={running
              ? (
                <span
                  aria-label={i18nT('apps.codeReviewSage.components.prReviewDetail.running')}
                  className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-pulse motion-reduce:animate-none"
                />
              )
              : findings || undefined}
            badgeTone={running ? 'accent' : findings ? 'alert' : 'muted'}
            onClick={() => setTab('review')}
          />
          <TabButton
            label={i18nT('apps.codeReviewSage.components.prReviewDetail.pull_request')}
            icon={FileText}
            active={tab === 'source'}
            // What is broken is the only count worth a badge here; the panel
            // itself carries per-section counts once you are inside it.
            badge={src ? (failing || undefined) : undefined}
            badgeTone={failing ? 'alert' : 'muted'}
            onClick={() => setTab('source')}
          />
        </div>
      </header>

      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-none px-4 md:px-6 py-5 flex flex-col gap-5">
        {tab === 'review' && reviewBody}
        {tab === 'source' && sourceBody()}
      </div>
    </article>
  )
}
