// GlobalPipelineView — the shell that owns the drill-down between the three
// levels of the pipeline view.
//
//   L0  the pipeline: every step, what is sitting in it, what it has moved
//   L1  one step: the items inside it
//   L2  one item: the sessions that worked it, and what each cost
//
// The levels STACK rather than replace: choosing a step keeps the pipeline in
// view, and opening an item keeps its step in view. An operator drilling into a
// stall is comparing levels, so replacing the parent would force them to
// remember the number they just clicked away from.
//
// L2 renders INSIDE the item row that owns it, injected through `renderSessions`,
// rather than as a section appended after the list. Appended, it was the page's
// last element below every other row, so opening the first item's sessions put the
// answer twenty rows further down -- and it could not be centred, because nothing
// followed it to scroll against. The shell still owns the fetch, so only the open
// item is read.
//
// Each level fetches only when it is open. L1 and L2 are the expensive reads
// (they walk the whole event trail and the usage shards), so mounting them
// eagerly would pay for data nobody asked to see.
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, AlertTriangle, ChevronLeft, GitBranch, RefreshCw } from 'lucide-react'
import {
  autoTriagePipelineFoldApi,
  isQueueMigrationPending,
  isUnsupportedForge,
  type RepoRef,
} from '../api'
import { repoScopeKey } from '../../lib/links'
import { Btn, Card, IconButton, PageHeader, EmptyState as UIEmptyState } from '../../../../components/ui'
import { i18nT } from '../../../../i18n/t'
import PipelineFlow, { stepLabel } from './PipelineFlow'
import StepItemsTable from './StepItemsTable'
import ItemSessionsTable from './ItemSessionsTable'

/** How often the open level refetches, in ms.
 *
 * The pipeline's own jobs run on minute-scale timers, so polling faster than this
 * spends reads to redisplay the same numbers. Each fetch re-folds the trail from
 * disk, which is cheap but not free.
 */
const REFRESH_MS = 30_000

/** A failed fetch, shown as a FAILURE with a retry -- never as an empty result.
 *
 * The three levels each used to fall through to their empty state when a request
 * failed, so a backend error read as "no pipeline activity yet", "no items in this
 * step", or "this item never opened an agent session". Each of those is a
 * confident factual claim, and an operator has no way to tell it from the truth.
 * Low frequency, but the failure mode is that the view lies rather than that it
 * breaks.
 *
 * One 503 is special and gets its own copy: the backend refuses `_handle_step` /
 * `_handle_item_sessions` with `queue_migration_pending` when the dispatch queue on
 * this install has not been sharded per repository yet -- the common state for a
 * teammate whose cron scripts predate that migration. The generic "Could not load
 * this data" plus a bare Retry is a trap there: retrying alone can NEVER clear it,
 * only re-running the pipeline installer can. So when the error carries that code we
 * name the real reason and the real fix. Retry is KEPT rather than hidden -- it
 * becomes meaningful the moment the operator runs the installer, and a distinct
 * label ("Retry after running the installer") stops it implying retrying is itself
 * the fix. Recognition-only: `isQueueMigrationPending` reads the backend's code, it
 * does not restate the rule.
 */
function ErrorPanel({
  testId,
  onRetry,
  error,
}: {
  testId: string
  onRetry: () => void
  error?: unknown
}) {
  const migrationPending = isQueueMigrationPending(error)
  return (
    <Card className="p-4" data-testid={migrationPending ? 'atp-queue-migration-pending' : testId}>
      <div className="flex flex-wrap items-center gap-2">
        <AlertTriangle aria-hidden="true" className="h-4 w-4" style={{ color: 'var(--warn)' }} />
        <p className="text-[12px]" style={{ color: 'var(--text)' }}>
          {i18nT(
            migrationPending
              ? 'apps.autoTriagePipeline.global.queue_migration_pending'
              : 'apps.autoTriagePipeline.global.load_failed',
          )}
        </p>
        <Btn onClick={onRetry} className="h-7 px-2 text-[11px]">
          {i18nT(
            migrationPending
              ? 'apps.autoTriagePipeline.global.queue_migration_retry'
              : 'apps.autoTriagePipeline.global.retry',
          )}
        </Btn>
      </div>
    </Card>
  )
}

export default function GlobalPipelineView({ repo }: { repo: RepoRef }) {
  const [step, setStep] = useState<string | null>(null)
  const [item, setItem] = useState<number | null>(null)
  // The clock has to ADVANCE, not be captured at mount. The queries below refetch
  // on their own, so a frozen clock leaves a tab that has been open a while
  // rendering fresh events as "12m ago" and a "last activity" that only ever gets
  // staler -- the relative labels would be lying about data that just arrived.
  // Ticking on the refetch interval keeps the two in step without a second timer
  // that could drift against them.
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  // The repository is HANDED IN by the host dashboard, which is Issue Radar's own
  // active repository. Scoping is honourable here because the trail stamps the
  // repository and the queue is one file per repository, so the enrichment, the
  // sessions and the costs all narrow with the filter rather than joining across it.
  const scopeKey = repoScopeKey(repo)

  const overview = useQuery({
    queryKey: ['atp', 'overview', scopeKey],
    queryFn: () => autoTriagePipelineFoldApi.overview(undefined, repo),
    // Poll while the answer can change. A refused forge is a standing fact about the
    // repository rather than a transient failure, so polling it would spend a request
    // every interval to be told the same thing. The RETRY policy is deliberately left
    // to the app: overriding it per query would replace a global setting -- including
    // the harness's `retry: false` -- with this one call site's opinion.
    refetchInterval: (query) => (isUnsupportedForge(query.state.error) ? false : REFRESH_MS),
  })

  const stepItems = useQuery({
    queryKey: ['atp', 'step', step, scopeKey],
    queryFn: () => autoTriagePipelineFoldApi.step({ step: step as string, ...repo }),
    enabled: step !== null,
    refetchInterval: REFRESH_MS,
  })

  const sessions = useQuery({
    queryKey: ['atp', 'sessions', item, scopeKey],
    queryFn: () => autoTriagePipelineFoldApi.itemSessions(item as number, repo),
    enabled: item !== null,
    refetchInterval: REFRESH_MS,
  })

  const refreshAll = () => {
    setNowMs(Date.now())
    void overview.refetch()
    if (step !== null) void stepItems.refetch()
    if (item !== null) void sessions.refetch()
  }

  const selectStep = (key: string) => {
    setStep((prev) => (prev === key ? null : key))
    setItem(null)
  }

  /** L2 for the open item, rendered inside its own row.
   *
   * Guarded on the number so a stale render of a row that is no longer the open one
   * cannot show another item's sessions -- the query is keyed on `item`, and while a
   * new item's fetch is in flight the cache still holds the previous one's rows.
   */
  const renderSessions = (number: number) => {
    if (item !== number) return null
    if (sessions.isError) {
      return <ErrorPanel testId="atp-sessions-error" onRetry={() => void sessions.refetch()} error={sessions.error} />
    }
    if (sessions.isLoading) {
      return (
        <p className="text-[12px]" style={{ color: 'var(--text-dim)' }}>
          {i18nT('apps.autoTriagePipeline.global.loading')}
        </p>
      )
    }
    return (
      <ItemSessionsTable
        sessions={sessions.data?.sessions ?? []}
        populatedColumns={sessions.data?.populatedColumns ?? []}
        nowMs={nowMs}
      />
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto px-4 py-4 md:px-6">
      <PageHeader
        title={i18nT('apps.autoTriagePipeline.global.title')}
        subtitle={i18nT('apps.autoTriagePipeline.global.subtitle')}
        actions={
          <IconButton
            onClick={refreshAll}
            title={i18nT('apps.autoTriagePipeline.global.refresh')}
            aria-label={i18nT('apps.autoTriagePipeline.global.refresh')}
          >
            <RefreshCw aria-hidden="true" className="h-3.5 w-3.5" />
          </IconButton>
        }
      />

      <div className="mt-3 flex flex-col gap-4">
        {overview.isError ? (
          isUnsupportedForge(overview.error) ? (
            // The backend refused this repository's forge, which is a different fact
            // from a failure: there is nothing to show and nothing to retry. Rendered
            // here rather than by the host because this is where the request and its
            // error already live -- asking the same question a second time, above the
            // tabs, would mean restating the rule the refusal already carries.
            <div
              className="min-h-[50vh] flex flex-col items-center justify-center gap-2.5 text-center px-6"
              data-testid="atp-unsupported-forge"
            >
              <GitBranch size={26} className="text-muted opacity-50" strokeWidth={1.5} />
              <div className="text-[13px] text-muted">
                {i18nT('apps.issueRadar.views.pipelineDashboard.github_only')}
              </div>
              <div className="text-[11.5px] text-muted opacity-70 max-w-md">
                {i18nT('apps.issueRadar.views.pipelineDashboard.github_only_hint')}
              </div>
            </div>
          ) : (
            // No `error` prop here, deliberately. The migration refusal cannot reach
            // this level: `fold_pipeline` never reads the queue, and `_handle_overview`
            // maps every FoldError to code "unreadable", so `queue_migration_pending`
            // is not a code the overview can return. Passing the error for symmetry
            // would ship a branch nothing can enter.
            <ErrorPanel testId="atp-overview-error" onRetry={() => void overview.refetch()} />
          )
        ) : overview.data && overview.data.steps.length > 0 ? (
          <PipelineFlow
            overview={overview.data}
            selectedStep={step}
            onSelectStep={selectStep}
            nowMs={nowMs}
          />
        ) : overview.isLoading ? (
          <Card className="p-4">
            <p className="text-[12px]" style={{ color: 'var(--text-dim)' }}>
              {i18nT('apps.autoTriagePipeline.global.loading')}
            </p>
          </Card>
        ) : (
          <Card className="p-4">
            <UIEmptyState
              icon={<Activity aria-hidden="true" className="h-5 w-5" />}
              title={i18nT('apps.autoTriagePipeline.global.no_pipeline_title')}
              subtitle={i18nT('apps.autoTriagePipeline.global.no_pipeline_subtitle')}
              testId="atp-no-pipeline"
            />
          </Card>
        )}

        {step !== null ? (
          <section className="flex flex-col gap-2" aria-live="polite">
            <header className="flex items-center gap-2">
              <IconButton
                onClick={() => {
                  setStep(null)
                  setItem(null)
                }}
                title={i18nT('apps.autoTriagePipeline.global.close_step')}
                aria-label={i18nT('apps.autoTriagePipeline.global.close_step')}
              >
                <ChevronLeft aria-hidden="true" className="h-3.5 w-3.5" />
              </IconButton>
              <h2
                className="text-[11px] font-semibold uppercase tracking-wide"
                style={{ color: 'var(--text)' }}
              >
                {/* The count is OMITTED until it is known, rather than defaulted to
                    zero. `count ?? 0` asserted "Implement - 0 items" for one refetch
                    cycle on every drill-in, which is a confident factual claim about
                    a step that in fact had items -- and it appeared directly above
                    the rows that contradicted it. */}
                {stepItems.data
                  ? i18nT('apps.autoTriagePipeline.global.step_heading', {
                      // The LOCALIZED label, not the raw key: the heading sat under
                      // the card the operator clicked, so "implement" appeared
                      // directly below "Implement" as if they were different things.
                      step: stepLabel(
                        overview.data?.steps.find((s) => s.key === step) ?? {
                          key: step,
                          label: step,
                        },
                      ),
                      count: stepItems.data.count,
                    })
                  : stepLabel(
                      overview.data?.steps.find((s) => s.key === step) ?? {
                        key: step,
                        label: step,
                      },
                    )}
              </h2>
            </header>
            {stepItems.isError ? (
              <ErrorPanel testId="atp-step-error" onRetry={() => void stepItems.refetch()} error={stepItems.error} />
            ) : stepItems.isLoading ? (
              <Card className="p-3">
                <p className="text-[12px]" style={{ color: 'var(--text-dim)' }}>
                  {i18nT('apps.autoTriagePipeline.global.loading')}
                </p>
              </Card>
            ) : (
              <StepItemsTable
                stepKey={step}
                repo={repo}
                items={stepItems.data?.items ?? []}
                expandedItem={item}
                onToggleItem={(n) => setItem((prev) => (prev === n ? null : n))}
                renderSessions={renderSessions}
                nowMs={nowMs}
              />
            )}
          </section>
        ) : null}
      </div>
    </div>
  )
}
