// The "Review" action: open a KiroCrew chat session seeded with a code-review
// prompt for one PULL REQUEST, filed into the same per-repo chat folder as issue
// investigations, and linked to a local record so a repeat click RESUMES the same
// session instead of spawning a duplicate.
//
// The PR analogue of lib/investigate.ts and its exact structural twin: only the
// slot title lives here, the seed prompt is in the sibling `review.prompt.ts`
// (a declared model-facing boundary the i18n gate ignores — see that file's
// header), and the session orchestration is shared via lib/agentSession.ts. The record store is shared but namespaced by item kind
// (see agentSession.ts), which is why every call here passes `kind: 'pull'`.
import { useCallback } from 'react'
import { type InvestigationRecord, type PullRequest, type RepoRef } from '../api'
import { truncate, useAgentSession } from './agentSession'
import { resolveAiLanguage } from './format'
import { useIssueRadar } from '../context'
import { providerTerms } from './links'
import { buildReviewPrompt } from './review.prompt'

export interface UseReviewPr {
  /** Open (or resume) the review session for a PR, then navigate to /chat.
   * Returns the linked record, or null on failure. */
  reviewPr: (
    repoRef: RepoRef,
    pr: PullRequest,
    existing: InvestigationRecord | null,
  ) => Promise<InvestigationRecord | null>
  busy: boolean
  error: Error | null
}

export function useReviewPr(): UseReviewPr {
  const { openSession, busy, error } = useAgentSession()
  // Live selection rather than the stored one -- see useInvestigate.
  const { aiLanguage } = useIssueRadar()

  const reviewPr = useCallback(
    (
      repoRef: RepoRef,
      pr: PullRequest,
      existing: InvestigationRecord | null,
    ): Promise<InvestigationRecord | null> =>
      openSession({
        repoRef,
        number: pr.number,
        kind: 'pull',
        title: `${providerTerms(repoRef).changeRequestShort}${providerTerms(repoRef).sigil}${pr.number} · ${truncate(pr.title)}`,
        prompt: buildReviewPrompt(
          repoRef, repoRef.owner, repoRef.repo, pr, resolveAiLanguage(aiLanguage),
        ),
        existing,
      }),
    [openSession, aiLanguage],
  )

  return { reviewPr, busy, error }
}
