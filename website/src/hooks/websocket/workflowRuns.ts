/** Keeps the chat workflow rows (`chat.workflowRuns`) consistent with the
 *  workflow-runs authority. `workflow_run_event` frames fold into the slice
 *  through the router; this owns the reconcile that closes the gaps a
 *  one-shot broadcast leaves. */
import { useCallback, useEffect } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import { useAppSelector, type AppDispatch } from '../../store'
import { reconcileWorkflowRuns } from '../../store/chatSlice'
import { api } from '../../api/client'

/** How often a workflow row this tab still shows as `running` is re-checked
 *  against `/api/workflows/runs`. Deliberately slow: a run lasts minutes, this
 *  is a correctness backstop for a lost terminal frame rather than the progress
 *  channel (that is the live event stream), and the tick makes no request at all
 *  while no row is running. */
const WORKFLOW_HEAL_MS = 15000

/** Reconcile chat workflow rows against the authority (`/api/workflows/runs`).
 *
 *  `workflow_run_event` is a one-shot broadcast with no replay, so a tab that
 *  was closed, asleep, or disconnected when a run ended keeps a row spinning at
 *  `running` for the rest of its life — and a run that started before the tab
 *  opened has no row at all, because nothing else ever seeds this slice. This
 *  read is the only thing that closes either gap.
 *
 *  Fails CLOSED: a rejected request, or a response without a `runs` array,
 *  means "the authority could not be read" and never "there are no runs", so
 *  nothing is dispatched. `api.workflowRuns` is called optionally because many
 *  component tests mock the api client partially, where a newly-added method is
 *  undefined. The merge itself is monotonic — see `reconcileWorkflowRuns`.
 *
 *  Routed through `queryClient.fetchQuery` so the three callers (connect,
 *  visibility, heal tick) SHARE one in-flight request instead of racing
 *  duplicate GETs when two of them land together — a visibility event landing
 *  next to a tick is exactly the common case. `staleTime: 0` keeps it a real
 *  read every time (a cached answer is the thing being corrected, so it must
 *  never be served from cache), and the key is deliberately NOT the Workflows
 *  tab's `['workflow-runs']`: that entry caches an unwrapped `RunSummary[]`
 *  from the app's own base path, so sharing it would collide on shape.
 *
 *  Also runs the heal tick: re-checks a still-`running` row against the
 *  authority on a slow interval. Connect-time reconcile covers every gap that
 *  COINCIDES with a reconnect, which is the common one — but a frame can also
 *  be lost while the socket stays open, and then nothing else would ever
 *  correct the row: the spinner is driven purely by stored status, and the
 *  linger cleanup only arms once a status is terminal. This makes the store
 *  eventually consistent with the authority no matter how a frame went
 *  missing. Returns the reconcile for the connect-time callers. */
export function useWorkflowRunReconcile(dispatch: AppDispatch, queryClient: QueryClient): () => Promise<void> {
  const syncWorkflowRuns = useCallback(async () => {
    const read = api.workflowRuns
    if (!read) return
    try {
      const out = await queryClient.fetchQuery({
        queryKey: ['workflow-runs-reconcile'],
        queryFn: () => read(),
        staleTime: 0,
        gcTime: 30_000,
        retry: false,
      })
      const runs = out?.runs
      if (!Array.isArray(runs)) return
      dispatch(reconcileWorkflowRuns(runs))
      // The command center lays live runs over the same read and does not poll
      // it; hand it this answer so a healed run cannot reappear as running.
      queryClient.setQueryData(['command-center', 'workflows'], out)
    } catch { /* unreadable authority — leave local state untouched */ }
  }, [dispatch, queryClient])

  /** True while ANY workflow row is stored as running. A boolean, so the hook
   *  re-renders only when the last run ends or the first one starts — that flip
   *  is what arms and disarms the heal timer below. */
  const anyWorkflowRunning = useAppSelector(s =>
    Object.values(s.chat.workflowRuns ?? {}).some(r => r?.status === 'running'),
  )

  /** Armed ONLY while a row is actually showing as running, so an idle tab holds
   *  no timer and issues no request; a hidden tab skips the tick (its rows are
   *  off screen and its timers are throttled anyway) and heals the moment it is
   *  looked at again. Lives with the socket rather than in WorkflowProgressBar
   *  because the sidebar's running indicator reads the same slice and a run
   *  belonging to a non-active slot renders no bar at all — one timer heals
   *  every surface. */
  useEffect(() => {
    if (!anyWorkflowRunning) return
    const heal = () => { if (!document.hidden) syncWorkflowRuns() }
    const timer = setInterval(heal, WORKFLOW_HEAL_MS)
    document.addEventListener('visibilitychange', heal)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', heal)
    }
  }, [anyWorkflowRunning, syncWorkflowRuns])

  return syncWorkflowRuns
}
