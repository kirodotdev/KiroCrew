// The "Investigate" control in the issue-detail header. Opens (or resumes) a
// KiroCrew chat session that investigates this issue — see lib/investigate.ts —
// and reflects the issue's saved investigation state:
//   * never investigated → "Investigate"
//   * has a session       → "Resume" + a status pill (Investigating / Investigated)
// The record is read cache-first; on click we optimistically write the returned
// record back into the query cache so the badge is right if the user returns.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Telescope, Loader2, Check } from 'lucide-react'
import { issueRadarApi, type Issue, type InvestigationResponse } from '../api'
import { useInvestigate } from '../lib/investigate'

const BTN =
  'inline-flex items-center gap-1 text-[12px] px-2 py-1 rounded-md border border-border ' +
  'text-muted hover:text-text hover:border-accent/50 disabled:opacity-40 disabled:cursor-default ' +
  'cursor-pointer bg-transparent whitespace-nowrap'

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

  const onClick = async () => {
    if (busy) return
    const saved = await investigate(owner, repo, issue, record)
    if (saved) {
      queryClient.setQueryData<InvestigationResponse>(key, {
        owner, repo, number: issue.number, investigation: saved,
      })
    }
  }

  const hasSession = !!record?.slot_key
  const resolved = record?.status === 'resolved'
  const verdict = record?.findings?.verdict
  const summary = record?.findings?.summary

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        onClick={onClick}
        disabled={busy}
        title={
          hasSession
            ? 'Resume the AI investigation chat session for this issue'
            : 'Open an AI investigation chat session for this issue'
        }
        className={BTN}
      >
        {busy ? (
          <Loader2 size={13} className="animate-spin" />
        ) : (
          <Telescope size={13} className="text-accent" />
        )}
        {hasSession ? 'Resume' : 'Investigate'}
      </button>

      {record && (
        <span
          title={summary || (resolved ? 'Investigation complete' : 'Investigation in progress')}
          className={
            'text-[10.5px] px-1.5 py-0.5 rounded-full font-medium ' +
            (resolved ? 'bg-aim-subtle text-aim' : 'bg-accent-subtle text-accent')
          }
        >
          {resolved ? (verdict ? <><Check size={10} className="lucide-inline" /> {verdict}</> : 'Investigated') : 'Investigating'}
        </span>
      )}

      {error && (
        <span className="text-[10.5px] text-danger" title={error.message}>
          couldn't start
        </span>
      )}
    </span>
  )
}
