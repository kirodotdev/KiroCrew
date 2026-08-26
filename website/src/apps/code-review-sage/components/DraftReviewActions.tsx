// The draft-review publish bar.
//
// Sage posts its findings as a GitHub review with no `event`, which leaves the
// review PENDING -- a draft only its author can see. Releasing it used to mean
// opening the pull request on github.com once per PR; this bar does it in place,
// and every guard the backend enforces is mirrored here so a button is absent
// rather than present-and-doomed.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Send } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '../../../api/client'
import { i18nT } from '../../../i18n/t'

const PUBLISH_EVENTS = ['COMMENT', 'REQUEST_CHANGES', 'APPROVE'] as const
type PublishEvent = (typeof PUBLISH_EVENTS)[number]

const PUBLISH_LABEL_KEY: Record<PublishEvent, string> = {
  COMMENT: 'apps.codeReviewSage.components.draftReviewActions.submit_as_comment',
  REQUEST_CHANGES: 'apps.codeReviewSage.components.draftReviewActions.request_changes',
  APPROVE: 'apps.codeReviewSage.components.draftReviewActions.approve',
}

// Past-tense verdicts for the published state. The button labels are imperative
// ("Approve"), which reads wrong once the action is done ("Published as Approve").
const PUBLISHED_VERDICT_KEY: Record<PublishEvent, string> = {
  COMMENT: 'apps.codeReviewSage.components.draftReviewActions.published_verdict_comment',
  REQUEST_CHANGES: 'apps.codeReviewSage.components.draftReviewActions.published_verdict_changes_requested',
  APPROVE: 'apps.codeReviewSage.components.draftReviewActions.published_verdict_approved',
}

/** The confirmation restates the verdict: a second click on a generic "Confirm"
 *  proves the user clicked twice, not that they meant THIS verdict. */
const PUBLISH_CONFIRM_KEY: Record<PublishEvent, string> = {
  COMMENT: 'apps.codeReviewSage.components.draftReviewActions.confirm_submit_as_comment',
  REQUEST_CHANGES: 'apps.codeReviewSage.components.draftReviewActions.confirm_request_changes',
  APPROVE: 'apps.codeReviewSage.components.draftReviewActions.confirm_approve',
}

export default function DraftReviewActions(
  { url, draftDelivered, expectedReviewId = '' }:
    { url: string; draftDelivered: boolean; expectedReviewId?: string },
) {
  const qc = useQueryClient()
  const [done, setDone] = useState<PublishEvent | null>(null)
  const [confirming, setConfirming] = useState<PublishEvent | null>(null)

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['code-review-sage-draft', url],
    queryFn: () => api.pullRequestPendingReview(url),
    retry: false,
  })

  const publishMut = useMutation({
    // The digest binds the publish to the CONTENT that was rendered, not just the
    // review id: GitHub lets a draft's body and comments change under the same id,
    // so the id alone cannot prove this is the draft the user read.
    mutationFn: (event: PublishEvent) =>
      api.submitPullRequestReview(url, data?.reviewId || '', event, data?.contentDigest || ''),
    onSuccess: (_r, event) => {
      setDone(event)
      // The pending review is gone and the PR now carries a submitted review —
      // drop both cached reads so the panel and this bar reflect the new state.
      qc.invalidateQueries({ queryKey: ['code-review-sage-draft', url] })
      qc.invalidateQueries({ queryKey: ['pull-request-source', url] })
    },
  })

  // Reset BOTH the outcome and any armed verdict when the pull request changes. An armed
  // confirmation is bound to the PR that was on screen when it was armed; carrying it
  // across a selection change means the second click publishes an irreversible verdict on
  // a pull request the reader never looked at.
  useEffect(() => { setDone(null); setConfirming(null) }, [url])

  if (isLoading) return null
  // Only the PUBLISH failure belongs here. A failed READ is reported by the error
  // branch below in the user's own terms; echoing its raw provider text here too
  // would restate the same failure twice, once unreadably.
  const err = publishMut.error instanceof Error ? publishMut.error.message : ''
  // Publishing is refused server-side for either reason; mirror it here so the
  // buttons are absent rather than present-and-doomed.
  // Two further grounds, both about publishing findings that are not the ones on
  // screen. Unless THIS run posted a draft for this pull request, the draft GitHub
  // holds is whatever an earlier run left. And even when it did post, a LATER run
  // replaces the draft by deleting and re-creating it, so the pending id has to
  // still be the one this run created.
  //
  // A missing `expectedReviewId` counts as superseded, not as permission: a run that
  // delivered but recorded no draft id cannot prove the pending draft is its own, and
  // an unprovable publish is the one this refusal exists to stop. Fail closed --
  // reading the draft stays available, only the irreversible act is withheld.
  const supersededDraft = draftDelivered && !!data
    && (!expectedReviewId || data.reviewId !== expectedReviewId)
  const blocked = !draftDelivered || supersededDraft
    || (!!data && (data.contentRedacted || data.stale))
  // Approve is withheld on two independent grounds, both about an approval
  // outliving the commit it reviewed: auto-merge could merge before a stale-head
  // check takes it back, and a base branch that keeps stale approvals lets one
  // stand after the head moves at all.
  const approveWithheld = !!data && (data.autoMergeArmed || !data.staleDismissalEnabled)

  if (done) {
    return (
      <div className="border border-border rounded-md p-3 mt-3 text-xs text-muted">
        {i18nT('apps.codeReviewSage.components.draftReviewActions.published_as')}{' '}
        <span className="text-text">{i18nT(PUBLISHED_VERDICT_KEY[done])}</span>
      </div>
    )
  }

  return (
    <div className="border border-border rounded-md p-3 mt-3">
      <div className="text-[13px] font-medium flex items-center gap-1.5">
        <Send size={13} /> {i18nT('apps.codeReviewSage.components.draftReviewActions.draft_review')}
      </div>
      {/* A FAILED read must never render as "no draft review": the read is the only
          thing that knows a draft exists, so reporting its failure as an absence
          tells the user their work is gone. `gh` auth lapsing is common enough that
          this page ships a setup box for it, so branch on the error first and offer
          a retry -- the query sets retry:false, making this the only way back. */}
      {error ? (
        <div className="text-xs mt-2">
          <div className="text-warn leading-relaxed">
            {i18nT('apps.codeReviewSage.components.draftReviewActions.couldn_t_check_for_a_draft_review_on_this_pull_r')}
          </div>
          <button
            onClick={() => { void refetch() }}
            disabled={isFetching}
            className="text-xs px-2.5 py-1.5 mt-2 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-30 cursor-pointer bg-transparent"
          >
            {i18nT('apps.codeReviewSage.components.draftReviewActions.try_again')}
          </button>
        </div>
      ) : !data?.reviewId ? (
        <div className="text-muted text-xs mt-2">
          {i18nT('apps.codeReviewSage.components.draftReviewActions.no_draft_review_on_this_pull_request')}
        </div>
      ) : (
        <>
          <div className="text-muted text-[11px] mt-1.5">
            {i18nT('apps.codeReviewSage.components.draftReviewActions.pending_only_you_can_see_it_until_you_publish')}
          </div>
          {/* Show the draft before offering to publish it. The reviewId guard proves
              you are publishing the draft you were TOLD about; rendering the body is
              what makes that the draft you actually READ — a pending review may be
              one the human started by hand, and publishing is irreversible. */}
          <div className="mt-2.5 border border-border rounded-md px-2.5 py-2 max-h-48 overflow-auto text-xs text-text whitespace-pre-wrap break-words font-mono">
            {data.body || (
              <span className="text-muted">
                {i18nT('apps.codeReviewSage.components.draftReviewActions.this_draft_has_no_summary_body')}
              </span>
            )}
          </div>
          {/* The inline comments publish alongside the body and `contentDigest` binds
              them, so showing only the body would have the digest certify text the
              reader never saw — consent to a summary standing in for consent to
              whatever hides behind it. Anchors are shown because WHERE a comment
              lands is part of what is being published.

              Defaulted rather than trusted: an unconditional `.length` here means a
              payload without the field takes down the whole panel, which is reachable
              during a rolling deploy where this bundle is newer than the backend
              serving it. Missing reads as "no inline comments". */}
          {(data.comments ?? []).length > 0 && (
            <div className="mt-1.5 border border-border rounded-md divide-y divide-border max-h-48 overflow-auto">
              {(data.comments ?? []).map((c, i) => (
                <div key={`${c.path}:${c.line ?? ''}:${i}`} className="px-2.5 py-2">
                  <div className="text-muted text-[11px] font-mono">
                    {c.line == null ? c.path : `${c.path}:${c.line}`}
                  </div>
                  <div className="text-xs text-text whitespace-pre-wrap break-words font-mono mt-1">
                    {c.body}
                  </div>
                </div>
              ))}
            </div>
          )}
          {/* Both refusals are enforced server-side; surfacing them here means the
              user learns WHY publishing is unavailable instead of clicking into a
              400. `contentRedacted` is the more surprising one: the body above is
              redacted for display, but submission publishes GitHub's stored draft,
              so publishing it would post the original text. */}
          {blocked ? (
            <div className="text-warn text-[11px] mt-2.5 leading-relaxed">
              {!draftDelivered
                ? i18nT('apps.codeReviewSage.components.draftReviewActions.publishing_is_blocked_this_review_is_still_runni')
                : supersededDraft
                  ? i18nT('apps.codeReviewSage.components.draftReviewActions.publishing_is_blocked_a_later_review_replaced_th')
                  : data.contentRedacted
                  ? i18nT('apps.codeReviewSage.components.draftReviewActions.publishing_is_blocked_this_draft_contains_conten')
                  : i18nT('apps.codeReviewSage.components.draftReviewActions.publishing_is_blocked_this_draft_was_written_aga')}
            </div>
          ) : (
          <div className="flex items-center gap-2 mt-2.5 flex-wrap">
            {/* Two clicks, and the second one names the verdict. The copy right here
                says publishing is irreversible and public, while the LESS consequential
                post step in this same pane already asks for confirmation — so firing a
                verdict on one click put the heavier action behind the lighter gate. All
                three sit at identical visual weight, so a habituated hand lands on
                Approve as easily as on Submit as comment. */}
            {confirming ? (
              <>
                <span className="text-xs text-text">
                  {i18nT(PUBLISH_CONFIRM_KEY[confirming])}
                </span>
                <button
                  type="button"
                  // Swapping the arming button out for this one drops keyboard focus to
                  // <body>, so Tab would restart from the top of the page on the app's
                  // one irreversible action. Take focus on mount, and let Escape back
                  // out the way every other dismissible surface does.
                  ref={el => el?.focus()}
                  onKeyDown={e => { if (e.key === 'Escape') setConfirming(null) }}
                  onClick={() => { const ev = confirming; setConfirming(null); publishMut.mutate(ev) }}
                  disabled={publishMut.isPending}
                  className="text-xs px-2.5 py-1.5 rounded-md border border-danger text-danger hover:bg-danger-subtle disabled:opacity-30 cursor-pointer bg-transparent"
                >
                  {i18nT(PUBLISH_LABEL_KEY[confirming])}
                </button>
                <button
                  type="button"
                  onKeyDown={e => { if (e.key === 'Escape') setConfirming(null) }}
                  onClick={() => setConfirming(null)}
                  className="text-xs px-2.5 py-1.5 rounded-md border border-border text-muted hover:text-text cursor-pointer bg-transparent"
                >
                  {i18nT('apps.codeReviewSage.components.draftReviewActions.cancel')}
                </button>
              </>
            ) : PUBLISH_EVENTS.map(ev => (
              ev === 'APPROVE' && approveWithheld ? null : (
              <button
                key={ev}
                onClick={() => setConfirming(ev)}
                disabled={publishMut.isPending}
                className="text-xs px-2.5 py-1.5 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-30 cursor-pointer bg-transparent"
              >
                {i18nT(PUBLISH_LABEL_KEY[ev])}
              </button>
              )
            ))}
            {publishMut.isPending && (
              <span className="text-muted text-[11px]">
                {i18nT('apps.codeReviewSage.components.draftReviewActions.publishing')}
              </span>
            )}
          </div>
          )}
          {!blocked && (
            <div className="text-[11px] mt-2">
              {data.autoMergeArmed && (
                <div className="text-warn mb-1 leading-relaxed">
                  {i18nT('apps.codeReviewSage.components.draftReviewActions.approve_is_unavailable_while_auto_merge_is_armed')}
                </div>
              )}
              {!data.autoMergeArmed && !data.staleDismissalEnabled && (
                <div className="text-warn mb-1 leading-relaxed">
                  {i18nT('apps.codeReviewSage.components.draftReviewActions.approve_is_unavailable_because_this_base_branch_')}
                </div>
              )}
              {/* Decision-critical copy is never the smallest, faintest text in the
                  block: this is the one line that tells the user the click cannot be
                  taken back, so it carries body weight, not footnote weight. */}
              <div className="text-text text-xs">
                {i18nT('apps.codeReviewSage.components.draftReviewActions.publishing_is_irreversible_and_visible_to_everyo')}
              </div>
            </div>
          )}
        </>
      )}
      {err && (
        <div className="text-danger text-xs mt-2">
          {/* Lead with what failed, then the provider's words — the same shape as the
              post button's failure, so an irreversible action is not the one place that
              shows bare HTTP text. */}
          <span className="font-medium">
            {i18nT('apps.codeReviewSage.components.draftReviewActions.publish_failed')}
          </span>{' '}
          <span className="break-words font-normal opacity-90">{err}</span>
        </div>
      )}
    </div>
  )
}
