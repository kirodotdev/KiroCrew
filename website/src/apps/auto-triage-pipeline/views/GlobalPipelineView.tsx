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
import { Activity, AlertTriangle, ChevronLeft, RefreshCw } from 'lucide-react'
import { autoTriagePipelineFoldApi, type RepoRef } from '../api'
import { Btn, Card, IconButton, PageHeader, EmptyState as UIEmptyState } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import PipelineFlow, { stepLabel } from './PipelineFlow'
import StepItemsTable from './StepItemsTable'
import ItemSessionsTable from './ItemSessionsTable'

// The repository this pipeline runs against. Both halves are GitHub IDENTIFIERS,
// so the joined spelling is the real name of the repo and not the product name in
// prose -- the slug exemption cannot see that here because the owner and the repo
// are separate fields rather than one `owner/repo` string.
const DEFAULT_REPO: RepoRef = { owner: 'kirodotdev', repo: 'KiroCrew' } // brand-ok: repo identifier

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
 */
function ErrorPanel({ testId, onRetry }: { testId: string; onRetry: () => void }) {
  return (
    <Card className="p-4" data-testid={testId}>
      <div className="flex flex-wrap items-center gap-2">
        <AlertTriangle aria-hidden="true" className="h-4 w-4" style={{ color: 'var(--warn)' }} />
        <p className="text-[12px]" style={{ color: 'var(--text)' }}>
          {i18nT('apps.autoTriagePipeline.global.load_failed')}
        </p>
        <Btn onClick={onRetry} className="h-7 px-2 text-[11px]">
          {i18nT('apps.autoTriagePipeline.global.retry')}
        </Btn>
      </div>
    </Card>
  )
}

export default function GlobalPipelineView() {
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

  // FIXED to the pipeline's own repository, NOT the stored preference. The
  // preference is shared with the other view and a user who set it to a different
  // repo would have pipeline items enriched from THAT repo's issue cache -- titles,
  // labels and assignees belonging to whichever issue happens to share the number.
  // The event trail carries no repository dimension precisely because these jobs
  // run against one repo, so reading the preference here offers a choice the data
  // cannot honour.
  const repo = DEFAULT_REPO

  const overview = useQuery({
    queryKey: ['atp', 'overview'],
    queryFn: () => autoTriagePipelineFoldApi.overview(),
    refetchInterval: REFRESH_MS,
  })

  const stepItems = useQuery({
    queryKey: ['atp', 'step', step, repo.owner, repo.repo],
    queryFn: () => autoTriagePipelineFoldApi.step({ step: step as string, ...repo }),
    enabled: step !== null,
    refetchInterval: REFRESH_MS,
  })

  const sessions = useQuery({
    queryKey: ['atp', 'sessions', item],
    queryFn: () => autoTriagePipelineFoldApi.itemSessions(item as number),
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
      return <ErrorPanel testId="atp-sessions-error" onRetry={() => void sessions.refetch()} />
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
          <ErrorPanel testId="atp-overview-error" onRetry={() => void overview.refetch()} />
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
              <ErrorPanel testId="atp-step-error" onRetry={() => void stepItems.refetch()} />
            ) : stepItems.isLoading ? (
              <Card className="p-3">
                <p className="text-[12px]" style={{ color: 'var(--text-dim)' }}>
                  {i18nT('apps.autoTriagePipeline.global.loading')}
                </p>
              </Card>
            ) : (
              <StepItemsTable
                stepKey={step}
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
