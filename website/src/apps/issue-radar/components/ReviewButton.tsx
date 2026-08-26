// The "Review" control in the pull-request-detail header — the PR analogue of
// the issue InvestigateButton. Opens (or resumes) a KiroCrew chat session that
// reviews this PR (see lib/review.ts) and shows "Resume" once a session exists.
//
// Unlike Investigate, there is NO status pill: the review agent only drafts the
// comments it thinks you should leave and records nothing, so any status would be
// permanently stuck on "pending".
//
// The session link is still stored in the shared record: GitHub issues and PRs
// share one number sequence per repo, so they cannot collide on `number`.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileSearch } from 'lucide-react'
import { issueRadarApi, type InvestigationResponse, type PullRequest, RepoRef } from '../api'
import { useReviewPr } from '../lib/review'
import AgentSessionButton from './AgentSessionButton'
import { providerTerms, repoScopeKey } from '../lib/links'

import { i18nT } from '../../../i18n/t'
export default function ReviewButton({
  repoRef, pull,
}: {
  repoRef: RepoRef
  pull: PullRequest
}) {
  const { owner, repo } = repoRef
  const scopeKey = repoScopeKey(repoRef)
  const terms = providerTerms(repoRef)
  const queryClient = useQueryClient()
  const key = ['issue-radar', 'investigation', scopeKey, 'pull', pull.number]
  const recordQuery = useQuery({
    queryKey: key,
    queryFn: () => issueRadarApi.getInvestigation(repoRef, pull.number, 'pull'),
    staleTime: 30_000,
  })
  const record = recordQuery.data?.investigation ?? null
  const { reviewPr, busy, error } = useReviewPr()
  // A pending or FAILED lookup is indistinguishable from "no record", and acting
  // on that would start a second session and overwrite the existing record's slot
  // link — orphaning the review the user already has. So the button waits for a
  // definite answer and reports a failed lookup instead of guessing.
  const unresolved = !recordQuery.isSuccess

  const onClick = async () => {
    if (busy || unresolved) return
    const saved = await reviewPr(repoRef, pull, record)
    if (saved) {
      queryClient.setQueryData<InvestigationResponse>(key, {
        owner, repo, number: pull.number, kind: 'pull', investigation: saved,
      })
    }
  }

  return (
    <AgentSessionButton
      icon={FileSearch}
      label={i18nT('apps.issueRadar.components.reviewButton.review')}
      record={record}
      busy={busy || recordQuery.isLoading}
      disabled={unresolved}
      error={error ?? (recordQuery.error as Error | null) ?? null}
      onClick={onClick}
      startHint={
        recordQuery.isError
          ? 'Could not check for an existing review session — retrying on refresh'
          : i18nT('apps.issueRadar.components.reviewButton.open_ai_code_review_session_for_this', { label: terms.changeRequestTitle })
      }
      resumeHint={i18nT('apps.issueRadar.components.reviewButton.resume_ai_code_review_session_for_this', { label: terms.changeRequestTitle })}
      // The review agent only DRAFTS comments for you — it records nothing, so a
      // status pill would sit on "Reviewing" forever. Resume is the only state
      // worth showing.
      showStatus={false}
    />
  )
}
