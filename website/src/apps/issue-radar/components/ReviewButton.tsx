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
import { issueRadarApi, type InvestigationResponse, type PullRequest } from '../api'
import { useReviewPr } from '../lib/review'
import AgentSessionButton from './AgentSessionButton'

export default function ReviewButton({
  owner, repo, pull,
}: {
  owner: string
  repo: string
  pull: PullRequest
}) {
  const queryClient = useQueryClient()
  const key = ['issue-radar', 'investigation', owner, repo, pull.number]
  const recordQuery = useQuery({
    queryKey: key,
    queryFn: () => issueRadarApi.getInvestigation(owner, repo, pull.number),
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
    const saved = await reviewPr(owner, repo, pull, record)
    if (saved) {
      queryClient.setQueryData<InvestigationResponse>(key, {
        owner, repo, number: pull.number, investigation: saved,
      })
    }
  }

  return (
    <AgentSessionButton
      icon={FileSearch}
      label="Review"
      record={record}
      busy={busy || recordQuery.isLoading}
      disabled={unresolved}
      error={error ?? (recordQuery.error as Error | null) ?? null}
      onClick={onClick}
      startHint={
        recordQuery.isError
          ? 'Could not check for an existing review session — retrying on refresh'
          : 'Open an AI code-review chat session for this Pull Request'
      }
      resumeHint="Resume the AI code-review chat session for this Pull Request"
      // The review agent only DRAFTS comments for you — it records nothing, so a
      // status pill would sit on "Reviewing" forever. Resume is the only state
      // worth showing.
      showStatus={false}
    />
  )
}
