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
import { issueRadarApi, type Issue, type InvestigationResponse, RepoRef } from '../api'
import { useInvestigate } from '../lib/investigate'
import AgentSessionButton from './AgentSessionButton'
import { repoScopeKey } from '../lib/links'

import { i18nT } from '../../../i18n/t'
export default function InvestigateButton({
  repoRef, issue,
}: {
  repoRef: RepoRef
  issue: Issue
}) {
  const { owner, repo } = repoRef
  const scopeKey = repoScopeKey(repoRef)
  const queryClient = useQueryClient()
  const key = ['issue-radar', 'investigation', scopeKey, 'issue', issue.number]
  const recordQuery = useQuery({
    queryKey: key,
    queryFn: () => issueRadarApi.getInvestigation(repoRef, issue.number),
    staleTime: 30_000,
  })
  const record = recordQuery.data?.investigation ?? null
  const { investigate, busy, error } = useInvestigate()
  // Same rule as ReviewButton: a pending or failed lookup must not be read as
  // "no session", or clicking would start a second one and orphan the first.
  const unresolved = !recordQuery.isSuccess

  const onClick = async () => {
    if (busy || unresolved) return
    const saved = await investigate(repoRef, issue, record)
    if (saved) {
      queryClient.setQueryData<InvestigationResponse>(key, {
        owner, repo, number: issue.number, investigation: saved,
      })
    }
  }

  return (
    <AgentSessionButton
      icon={Telescope}
      label={i18nT('apps.issueRadar.components.investigateButton.investigate')}
      record={record}
      busy={busy || recordQuery.isLoading}
      disabled={unresolved}
      error={error ?? (recordQuery.error as Error | null) ?? null}
      onClick={onClick}
      startHint={
        recordQuery.isError
          ? i18nT('apps.issueRadar.components.investigateButton.could_not_check_for_an_existing_investigation_re')
          : i18nT('apps.issueRadar.components.investigateButton.open_an_ai_investigation_chat_session_for_this_i')
      }
      resumeHint={i18nT('apps.issueRadar.components.investigateButton.resume_the_ai_investigation_chat_session_for_thi')}
      pendingLabel={i18nT('apps.issueRadar.components.investigateButton.investigating')}
      donePillLabel={i18nT('apps.issueRadar.components.investigateButton.investigated')}
    />
  )
}
