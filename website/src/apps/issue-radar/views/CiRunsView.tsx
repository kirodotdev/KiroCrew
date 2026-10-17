import { useQuery } from '@tanstack/react-query'
import { Bell, RefreshCw } from 'lucide-react'
import ErrorNotice from '../../../components/ErrorNotice'
import { issueRadarApi, type CiRepoRuns, type CiRun } from '../api'
import { relativeTimeOrDate } from '../lib/format'
import { fmtElapsed } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'

/** CI runs dashboard — recent GitHub Actions runs across every connected GitHub
 * repo, one block per repo.
 *
 * Read on demand: the query runs while this tab is mounted and on the Refresh
 * button, never in the background and never on window focus, so an install that
 * never opens the tab makes no request for it. A repo whose read failed renders
 * its own error row, so one unreachable repo does not hide the others. */
export default function CiRunsView() {
  const query = useQuery({
    queryKey: ['issue-radar', 'ci-runs'],
    queryFn: () => issueRadarApi.ciRuns(),
    refetchOnWindowFocus: false,
    retry: false,
  })
  const repos = query.data?.repos ?? []

  return (
    <div className="p-6 space-y-5">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[13px] text-muted">
          {i18nT('apps.issueRadar.views.ciRunsView.recent_github_actions_runs_in_your_connected_repos')}
        </p>
        <button
          type="button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-[13px] text-text hover:bg-bg-hover disabled:opacity-50"
        >
          <RefreshCw size={14} aria-hidden="true" className={query.isFetching ? 'animate-spin' : undefined} />
          {i18nT('apps.issueRadar.views.ciRunsView.refresh')}
        </button>
      </div>

      {query.isPending ? (
        <p className="text-[13px] text-muted" role="status">
          {i18nT('apps.issueRadar.views.ciRunsView.loading_runs')}
        </p>
      ) : query.isError ? (
        // A failed read on a page that holds no draft, so the hand-off is offered.
        <ErrorNotice
          title={i18nT('apps.issueRadar.views.ciRunsView.couldn_t_load_ci_runs')}
          message={query.error instanceof Error ? query.error.message : String(query.error)}
          askAgent
          testId="ci-runs-error"
        />
      ) : repos.length === 0 ? (
        <p className="text-[13px] text-muted">
          {i18nT('apps.issueRadar.views.ciRunsView.no_github_repository_is_connected')}
        </p>
      ) : (
        repos.map((r) => <RepoBlock key={`${r.host}/${r.owner}/${r.repo}`} entry={r} />)
      )}

      {query.data?.truncated && (
        <p className="text-[12px] text-muted">
          {i18nT('apps.issueRadar.views.ciRunsView.not_every_connected_github_repository_is_listed')}
        </p>
      )}
    </div>
  )
}

function RepoBlock({ entry }: { entry: CiRepoRuns }) {
  const slug = `${entry.owner}/${entry.repo}`
  return (
    <section aria-label={slug} className="rounded-xl border border-border bg-bg">
      <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-b border-border px-4 py-2.5">
        <h3 className="min-w-0 truncate text-[13px] font-semibold text-text" title={slug}>{slug}</h3>
        {entry.alerts && (
          <span className="inline-flex shrink-0 items-center gap-1 text-[12px] text-muted">
            <Bell size={12} aria-hidden="true" />
            {i18nT('apps.issueRadar.views.ciRunsView.failure_alerts_on')}
          </span>
        )}
      </header>
      {entry.error ? (
        <ErrorNotice
          message={i18nT('apps.issueRadar.views.ciRunsView.couldn_t_read_this_repository_s_runs')}
          variant="inline"
          askAgent
          className="px-4 py-3"
          testId="ci-runs-repo-error"
        />
      ) : entry.runs.length === 0 ? (
        <p className="px-4 py-3 text-[13px] text-muted">
          {i18nT('apps.issueRadar.views.ciRunsView.no_workflow_runs_yet')}
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {entry.runs.map((run) => <RunRow key={run.id} run={run} />)}
        </ul>
      )}
    </section>
  )
}

/** The run's state in the user's words, and the tone it is drawn in. */
export function runState(run: Pick<CiRun, 'status' | 'conclusion'>): { label: string; tone: string } {
  if (run.status !== 'completed') {
    return run.status === 'in_progress'
      ? { label: i18nT('apps.issueRadar.views.ciRunsView.status_running'), tone: 'text-accent' }
      : { label: i18nT('apps.issueRadar.views.ciRunsView.status_queued'), tone: 'text-muted' }
  }
  switch (run.conclusion) {
    case 'success':
      return { label: i18nT('apps.issueRadar.views.ciRunsView.status_passed'), tone: 'text-ok' }
    case 'failure':
      return { label: i18nT('apps.issueRadar.views.ciRunsView.status_failed'), tone: 'text-danger' }
    case 'timed_out':
      return { label: i18nT('apps.issueRadar.views.ciRunsView.status_timed_out'), tone: 'text-danger' }
    case 'cancelled':
      return { label: i18nT('apps.issueRadar.views.ciRunsView.status_cancelled'), tone: 'text-muted' }
    default:
      return { label: i18nT('apps.issueRadar.views.ciRunsView.status_finished'), tone: 'text-muted' }
  }
}

function RunRow({ run }: { run: CiRun }) {
  const state = runState(run)
  const when = run.started_at || run.created_at
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-[13px]">
      <span className={`w-24 shrink-0 font-medium ${state.tone}`}>{state.label}</span>
      {run.url ? (
        <a href={run.url} target="_blank" rel="noopener noreferrer" className="min-w-0 truncate text-text hover:underline">
          {run.name}
        </a>
      ) : (
        <span className="min-w-0 truncate text-text">{run.name}</span>
      )}
      {run.branch && <code className="truncate text-[12px] text-muted">{run.branch}</code>}
      {run.pull_requests.map((n) => (
        <span key={n} className="text-[12px] text-muted">#{n}</span>
      ))}
      <span className="ml-auto flex shrink-0 gap-3 text-[12px] text-muted">
        {run.duration_sec !== null && <span>{fmtElapsed(run.duration_sec * 1000)}</span>}
        {when && <span>{relativeTimeOrDate(when)}</span>}
      </span>
    </li>
  )
}
