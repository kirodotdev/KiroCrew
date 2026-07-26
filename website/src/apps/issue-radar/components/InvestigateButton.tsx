// The "Investigate" control in the issue-detail header. Opens (or resumes) a
// KiroCrew chat session that investigates this issue — see lib/investigate.ts —
// and reflects the issue's saved investigation state (never investigated →
// "Investigate"; has a session → "Resume" + a status pill). The record is read
// cache-first; on click we optimistically write the returned record back into
// the query cache so the badge is right if the user returns.
//
// Presentation is shared with the PR "Review" control (AgentSessionButton).
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Telescope } from 'lucide-react'
import { issueRadarApi, type Issue, type InvestigationResponse } from '../api'
import { useInvestigate } from '../lib/investigate'
import AgentSessionButton from './AgentSessionButton'

export default function InvestigateButton({
  owner, repo, issue,
}: {
  owner: string
  repo: string
  issue: Issue
}) {
  const queryClient = useQueryClient()
  const key = ['issue-radar', 'investigation', owner, repo, issue.number]
  const recordQuery = useQuery({
    queryKey: key,
    queryFn: () => issueRadarApi.getInvestigation(owner, repo, issue.number),
    staleTime: 30_000,
  })
  const record = recordQuery.data?.investigation ?? null
  const { investigate, busy, error } = useInvestigate()
  // Same rule as ReviewButton: a pending or failed lookup must not be read as
  // "no session", or clicking would start a second one and orphan the first.
  const unresolved = !recordQuery.isSuccess

  const onClick = async () => {
    if (busy || unresolved) return
    const saved = await investigate(owner, repo, issue, record)
    if (saved) {
      queryClient.setQueryData<InvestigationResponse>(key, {
        owner, repo, number: issue.number, investigation: saved,
      })
    }
  }

  return (
    <AgentSessionButton
      icon={Telescope}
      label="Investigate"
      record={record}
      busy={busy || recordQuery.isLoading}
      disabled={unresolved}
      error={error ?? (recordQuery.error as Error | null) ?? null}
      onClick={onClick}
      startHint={
        recordQuery.isError
          ? 'Could not check for an existing investigation — retrying on refresh'
          : 'Open an AI investigation chat session for this issue'
      }
      resumeHint="Resume the AI investigation chat session for this issue"
      pendingLabel="Investigating"
      donePillLabel="Investigated"
    />
  )
}
