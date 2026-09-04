// The "Investigate" action: open a KiroCrew chat session seeded with an
// investigation prompt for one ISSUE, filed into a per-repo chat folder, and
// linked to a local record so a repeat click RESUMES the same session instead of
// spawning a duplicate.
//
// Only the slot title lives here; the session orchestration (folder → slot →
// seed+run → link → navigate) is shared with the pull-request Review action in
// lib/agentSession.ts, and the seed prompt itself lives in the sibling
// `investigate.prompt.ts`.
//
// Why the prompt is a separate module: `*.prompt.ts` is a declared model-facing
// boundary that the i18n gate ignores (see that file's header). Keeping the split
// means THIS module stays fully covered, so any UI copy added here — a label, a
// title, an error message — is still caught by the gate.
import { useCallback } from 'react'
import { type Issue, type InvestigationRecord, type RepoRef } from '../api'
import { buildInvestigationPrompt } from './investigate.prompt'
import { truncate, useAgentSession } from './agentSession'
import { resolveAiLanguage } from './format'
import { useIssueRadar } from '../context'

export interface UseInvestigate {
  /** Open (or resume) the investigation session for an issue, then navigate to
   * /chat. Returns the linked record, or null on failure — and null WITHOUT an
   * error when the click was declined because the investigation already
   * concluded (see `concluded`). Pass `force` to start over anyway. */
  investigate: (
    repoRef: RepoRef,
    issue: Issue,
    existing: InvestigationRecord | null,
    force?: boolean,
  ) => Promise<InvestigationRecord | null>
  busy: boolean
  error: Error | null
  concludedFor: string | null
  /** Go to the chat page with Older Sessions open — see `useAgentSession`. */
  openOlderSessions: () => void
}

export function useInvestigate(): UseInvestigate {
  const { openSession, busy, error, concludedFor, openOlderSessions } = useAgentSession()
  // Read the live selection, not the stored one: a browser that refuses the
  // persistence write (private mode, quota) still has to honour the language
  // the user just picked, and the prompt is built at click time.
  const { aiLanguage } = useIssueRadar()

  const investigate = useCallback(
    (
      repoRef: RepoRef,
      issue: Issue,
      existing: InvestigationRecord | null,
      force = false,
    ): Promise<InvestigationRecord | null> =>
      openSession({
        repoRef,
        number: issue.number,
        title: `#${issue.number} · ${truncate(issue.title)}`,
        prompt: buildInvestigationPrompt(
          repoRef, repoRef.owner, repoRef.repo, issue, resolveAiLanguage(aiLanguage),
        ),
        existing,
        force,
      }),
    [openSession, aiLanguage],
  )

  return { investigate, busy, error, concludedFor, openOlderSessions }
}
