// The "Respond" action: open a KiroCrew chat session that ANSWERS the feedback one
// change request received — the author's seat, as opposed to lib/review.ts, which
// drafts a review of someone else's change and is forbidden from writing anything.
//
// Structurally the third sibling of lib/investigate.ts and lib/review.ts: only the
// seed prompt, the slot title and the record's verb live here, while the session
// orchestration is shared via lib/agentSession.ts.
//
// Every call passes BOTH `kind: 'pull'` and `verb: 'respond'`. The kind keeps the
// record off a GitLab issue with the same number; the verb keeps it off the Review
// session's record, which holds a different `slot_key` for the same change request.
import { useCallback } from 'react'
import { type InvestigationRecord, type PullRequest, type RepoRef } from '../api'
import { truncate, useAgentSession } from './agentSession'
import { providerTerms } from './links'
import { buildRespondPrompt } from './respond.prompt'

export interface UseRespondToPr {
  /** Open (or resume) the respond session for a change request, then navigate to
   * /chat. Returns the linked record, or null on failure.
   *
   * `localPath` is the repository the work hangs off. It is required rather than
   * optional because a session that has to guess the directory is the failure the
   * readiness gate exists to prevent — the caller keeps the control disabled until
   * the server reports a usable checkout. */
  respondToPr: (
    repoRef: RepoRef,
    pr: PullRequest,
    localPath: string,
    existing: InvestigationRecord | null,
  ) => Promise<InvestigationRecord | null>
  busy: boolean
  error: Error | null
}

export function useRespondToPr(): UseRespondToPr {
  const { openSession, busy, error } = useAgentSession()

  const respondToPr = useCallback(
    (
      repoRef: RepoRef,
      pr: PullRequest,
      localPath: string,
      existing: InvestigationRecord | null,
    ): Promise<InvestigationRecord | null> => {
      const terms = providerTerms(repoRef)
      return openSession({
        repoRef,
        number: pr.number,
        kind: 'pull',
        verb: 'respond',
        title: `${terms.changeRequestShort}${terms.sigil}${pr.number} · ${truncate(pr.title)}`,
        prompt: buildRespondPrompt(repoRef, repoRef.owner, repoRef.repo, pr, localPath),
        existing,
      })
    },
    [openSession],
  )

  return { respondToPr, busy, error }
}
