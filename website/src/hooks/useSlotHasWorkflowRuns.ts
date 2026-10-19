import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { useAppSelector } from '../store'
import { runBelongsToSlot } from '../apps/workflows/runModel'
import type { WorkflowRunSummary } from '../types'

/** Whether `slot` has any workflow run, in any state, as far as this client
 *  can tell. Gates the Workflows row of the side panel's "+" menu.
 *
 *  Two sources, either one enough: the run list from `/api/workflows/runs`,
 *  read through the command center's `['command-center', 'workflows']` entry
 *  (the websocket already refreshes it when a run ends, and the reconnect
 *  reconcile writes it), and the live `workflow_run_event` rows in the store,
 *  which name a run the moment it starts, before any list read has seen it.
 *
 *  Fails OPEN, like `summaryEnabled`: no slot, a list never read, or a failed
 *  read all count as "has runs". A background refetch keeps the cached answer,
 *  so the row does not come and go with every read. */
export function useSlotHasWorkflowRuns(slot: string | null | undefined): boolean {
  const { data: runs, isSuccess } = useQuery({
    queryKey: ['command-center', 'workflows'],
    queryFn: api.workflowRuns,
    select: (d): WorkflowRunSummary[] => (Array.isArray(d?.runs) ? d.runs : []),
    enabled: !!slot,
    retry: false,
    // The command center's options for this same entry: its frames and the
    // reconnect reconcile own freshness, so a focus only re-reads after an error.
    staleTime: 3_000,
    refetchOnWindowFocus: (query: { state: { status: string } }) => query.state.status === 'error',
  })
  // A boolean, so an unrelated run's progress frame does not re-render the panel.
  const hasLiveRun = useAppSelector(s => Object.values(s.chat.workflowRuns ?? {})
    .some(r => runBelongsToSlot(r.sessionKey, slot)))
  const hasListedRun = useMemo(
    () => (runs ?? []).some(r => runBelongsToSlot(r.session_key, slot)),
    [runs, slot],
  )
  if (!slot || !isSuccess) return true
  return hasLiveRun || hasListedRun
}
