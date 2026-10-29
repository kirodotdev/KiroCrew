/** Cadence of the side panel's `['workflow-runs']` list poll.
 *
 *  The list read is the correctness backstop for workflow rows, not the
 *  progress channel (that is the live `workflow_run_event` stream), and each
 *  read authorizes every stored run on the gateway. So it polls fast only
 *  while someone is looking at the Workflows view or a run of this chat is
 *  running; otherwise it falls back to the same slow tick the socket-side
 *  healer uses, which still converges the Workflows tab badge. A hidden tab
 *  makes no request either way: the query sets `refetchIntervalInBackground:
 *  false`, so react-query skips every tick while the page is not visible. */
import { runBelongsToSlot } from '../../apps/workflows/runModel'

/** Foreground cadence: the Workflows view is open or a run is live. */
export const WORKFLOW_RUNS_FAST_POLL_MS = 2500
/** Idle cadence, matching the socket healer's `WORKFLOW_HEAL_MS`. */
export const WORKFLOW_RUNS_HEAL_POLL_MS = 15000

interface RunLike {
  status?: string
  session_key?: string
}

/** Interval for the list poll given the shown side-panel view, the last list
 *  read and the chat slot it is scoped to. */
export function workflowRunsPollInterval(
  shownView: string | undefined,
  runs: readonly RunLike[] | undefined,
  slot: string | undefined | null,
): number {
  if (shownView === 'workflows') return WORKFLOW_RUNS_FAST_POLL_MS
  const running = (runs ?? []).some(r => r.status === 'running' && runBelongsToSlot(r.session_key, slot))
  return running ? WORKFLOW_RUNS_FAST_POLL_MS : WORKFLOW_RUNS_HEAL_POLL_MS
}
