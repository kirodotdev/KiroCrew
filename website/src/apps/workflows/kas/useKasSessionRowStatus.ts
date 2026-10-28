/**
 * The Kiro Workflows half of a sidebar session row's status (`SessionRow`): the
 * chat's KAS-native runs (agent.kas_workflows) from the shared state poll,
 * narrowed to this row, and whether one of them is parked on a person. Inert
 * unless Kiro Workflows is on and the backend is Kiro Agent. Kept here, not in
 * `SessionRow.tsx`, so that file stays under the line cap and the KAS rules sit
 * with the rest of KAS's code.
 */
import { useMemo } from 'react'

import { useKasSidebarActivity, type KasSidebarActivity } from './useKasWorkflows'

/** `{count, name, phase}` of a row's running workflow fan-out. */
export interface SessionRowWorkflow {
  count: number
  name: string
  phase: string
}

export interface KasSessionRowStatus {
  /** The row's workflow line: the Crew run's when there is one, else the KAS runs'. */
  wf: SessionRowWorkflow | undefined
  /** Gap 8: a Kiro Agent workflow in this chat is parked on a person (a step
   *  approval / question, or a non-user park). A Crew dynamic-workflow run never
   *  sets it; it drives the row's dedicated "needs you" status line, ranked with
   *  the owed-decision cluster rather than the "working" signals. */
  needsYou: boolean
}

/** The rule, pure: a Crew run outranks the KAS runs (both cannot normally coexist). */
export function kasSessionRowStatus(
  crew: SessionRowWorkflow | undefined,
  kas: KasSidebarActivity | undefined,
): KasSessionRowStatus {
  return {
    wf: crew ?? (kas ? { count: kas.count, name: kas.name, phase: kas.phase } : undefined),
    needsYou: !crew && !!kas?.attention,
  }
}

/** {@link kasSessionRowStatus} for the row of *slotKey*, read from the KAS state poll. */
export function useKasSessionRowStatus(slotKey: string, crew: SessionRowWorkflow | undefined): KasSessionRowStatus {
  const kas = useKasSidebarActivity(slotKey)
  return useMemo(() => kasSessionRowStatus(crew, kas), [crew, kas])
}
