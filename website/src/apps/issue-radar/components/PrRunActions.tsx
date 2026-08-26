// CI run controls for the PR detail sidebar: cancel an in-flight run, re-run a
// finished one (all jobs, or just the failed ones).
//
// This is a separate surface from the check ROWS above it, because a check is not a
// run: the checks list reports per-job results and merges in commit statuses from
// services that have no runs at all, while cancel/re-run acts on the parent
// workflow run and needs its id. Fetching the runs is therefore its own query,
// enabled only when a head sha is known.
//
// The buttons offered come from the server's `cancellable` / `rerunnable` flags, so
// the UI never presents an action the provider will refuse (cancelling a finished
// run is a 409; re-running an in-flight one is not a thing).
import { useQuery } from '@tanstack/react-query'
import { CircleSlash, RotateCw, Loader2, AlertTriangle, PlayCircle } from 'lucide-react'
import { safeHttpUrl } from '../../../lib/safeUrl'
import { usePrActions, PR_ACTION } from '../lib/prActions'
import { repoScopeKey } from '../lib/links'
import { issueRadarApi, type RepoRef } from '../api'
import { detailPollMs } from '../lib/format'

import { i18nT } from '../../../i18n/t'

export default function PrRunActions({
  repoRef, number, headSha, canWrite, live,
}: {
  repoRef: RepoRef
  number: number
  /** The PR's head commit — the runs hang off it. Null when unknown (a deleted
   * fork branch), in which case there is nothing to fetch. */
  headSha: string | null
  canWrite: boolean
  /** Whether the PR is still open+unmerged, which decides whether the run list
   * keeps polling. A finished PR's runs do not change. */
  live: boolean
}) {
  const scopeKey = repoScopeKey(repoRef)
  const actions = usePrActions(repoRef, number)

  const runsQuery = useQuery({
    // The sha is part of the key, not just of the fetch: after a force-push the
    // runs belong to a different commit, and a key without it would serve the old
    // commit's runs (and its cancel/re-run ids) until the next poll landed.
    queryKey: ['issue-radar', 'pull-runs', scopeKey, number, headSha],
    queryFn: () => issueRadarApi.pullRuns(repoRef, number, headSha ?? ''),
    // `canWrite` too, not just the sha: the runs feed ONLY the cancel/re-run
    // controls, which the `!canWrite` guard below hides on a read-only repo. Without
    // it every PR-detail open on such a repo fetched /pull/runs and re-polled it
    // every 30s for buttons the user can never see — a provider round-trip per cycle
    // spent on data that is always discarded. React-query re-enables automatically
    // if access flips, so a writable repo is unchanged.
    enabled: canWrite && Boolean(headSha),
    // Only while the PR is live, and at the same cadence as the detail pane — a
    // run's status is exactly as volatile as the checks beside it.
    refetchInterval: detailPollMs(live),
  })

  if (!headSha) return null

  const runs = runsQuery.data?.runs ?? []
  const actionable = runs.filter((r) => r.cancellable || r.rerunnable)
  // Nothing to show: no runs at all, or none the user could act on. The checks
  // rows above already carry the STATUS, so an empty controls block would be noise.
  if (!canWrite || (!runsQuery.isLoading && actionable.length === 0)) return null

  return (
    <div className="mt-2 pt-2 border-t border-border/60">
      <div className="flex items-center gap-1.5 text-[10.5px] uppercase tracking-wider text-muted font-medium mb-1">
        <PlayCircle className="lucide-inline" />
        {i18nT('apps.issueRadar.components.prRunActions.ci_runs')}
      </div>

      {runsQuery.isLoading && (
        <span className="inline-flex items-center gap-1.5 text-[12px] text-muted">
          <Loader2 className="lucide-inline animate-spin text-accent" />
          {i18nT('apps.issueRadar.components.prRunActions.loading_runs')}
        </span>
      )}

      {actions.error && (
        <div className="mb-1 flex items-start gap-1 text-[11.5px] text-danger">
          <AlertTriangle className="lucide-inline flex-shrink-0" />
          <span className="min-w-0 break-words">{actions.error.message}</span>
        </div>
      )}

      <div className="flex flex-col gap-1">
        {actionable.map((run) => {
          const href = run.url ? safeHttpUrl(run.url) : null
          const cancelBusy = actions.busy === `${PR_ACTION.cancelRun}:${run.id}`
          const rerunBusy = actions.busy === `${PR_ACTION.rerunRun}:${run.id}`
          return (
            <div key={run.id} className="flex items-center gap-1.5 min-w-0">
              <span className="min-w-0 flex-1 truncate text-[12px] text-text" title={run.name}>
                {href
                  ? (
                    <a href={href} target="_blank" rel="noreferrer" className="text-text hover:text-accent hover:underline">
                      {run.name}
                    </a>
                  )
                  : run.name}
              </span>
              {run.cancellable && (
                <button
                  onClick={() => actions.cancelRun(run.id)}
                  disabled={Boolean(actions.busy)}
                  aria-label={i18nT('apps.issueRadar.components.prRunActions.cancel_run', { name: run.name })}
                  title={i18nT('apps.issueRadar.components.prRunActions.cancel_run', { name: run.name })}
                  className="flex-shrink-0 inline-flex items-center cursor-pointer bg-transparent text-muted hover:text-danger disabled:opacity-30"
                >
                  {cancelBusy
                    ? <Loader2 className="lucide-inline animate-spin" />
                    : <CircleSlash className="lucide-inline" />}
                </button>
              )}
              {run.rerunnable && (
                <button
                  // Failed-only: the common intent after a flake, and far cheaper
                  // than re-running a whole green matrix to retry one job.
                  onClick={() => actions.rerunRun(run.id, run.conclusion === 'failure')}
                  disabled={Boolean(actions.busy)}
                  aria-label={i18nT('apps.issueRadar.components.prRunActions.rerun_run', { name: run.name })}
                  title={run.conclusion === 'failure'
                    ? i18nT('apps.issueRadar.components.prRunActions.rerun_failed_jobs', { name: run.name })
                    : i18nT('apps.issueRadar.components.prRunActions.rerun_run', { name: run.name })}
                  className="flex-shrink-0 inline-flex items-center cursor-pointer bg-transparent text-muted hover:text-accent disabled:opacity-30"
                >
                  {rerunBusy
                    ? <Loader2 className="lucide-inline animate-spin" />
                    : <RotateCw className="lucide-inline" />}
                </button>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
