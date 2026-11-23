import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryBenchPlanRow, MemoryBenchRequest } from '../api/client/memory'
import { registerMemoryBenchSheetHost, type MemoryBenchOutcome } from '../app-sdk/memoryBench'
import { fmtNumber } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'
import { AckBox, HostNote, QuietBtn, SheetActions, SheetCard, SheetRow } from './memorySheetParts'
import { Btn, Checkbox } from './ui'

const RUNNING = new Set(['running'])

/** One provider in the plan: a card row naming where its copy lives, with what the run does to it. */
function ProviderRow({ row }: { row: MemoryBenchPlanRow }) {
  const { t } = useTranslation()
  const na = Object.keys(row.na)
  const remote = row.egress === 'remote'
  const detail = [
    row.ingest_mode === 'replay'
      ? t('pages.overview.memoryBench.replays', { n: fmtNumber(row.turns) })
      : t('pages.overview.memoryBench.writes', { n: fmtNumber(row.writes) }),
    row.cleanup === 'reset' ? t('pages.overview.memoryBench.cleanup_reset') : t('pages.overview.memoryBench.cleanup_delete'),
  ]
  if (na.length > 0) detail.push(t('pages.overview.memoryBench.not_measured', { dims: na.join(', ') }))
  return (
    <div className="flex flex-col gap-0.5 px-3 py-2.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <dt className="font-medium text-text-strong">{row.name}</dt>
        <dd className={`ml-auto text-right font-medium ${remote ? 'text-warn-text' : 'text-text-strong'}`}>
          {remote ? t('pages.overview.memoryBench.egress_remote') : t('pages.overview.memoryBench.egress_local')}
        </dd>
      </div>
      <p className="text-[12px] text-muted">{detail.join(' · ')}</p>
    </div>
  )
}

/**
 * The benchmark sheet: plans a run (a dry run), says it scores each provider on the
 * test set's questions, shows each provider's egress, cleanup and what cannot be
 * measured, the model call budget when the run needs any, and starts the run
 * only on the owner's press. A provider that keeps memory off this machine needs the
 * owner's tick for a copy of their own memory; the gateway checks the item count too.
 */
export function MemoryBenchSheet({
  request,
  onDone,
}: {
  request: MemoryBenchRequest
  onDone: (outcome: MemoryBenchOutcome) => void
}) {
  const { t } = useTranslation()
  const [acked, setAcked] = useState(false)
  const [runId, setRunId] = useState('')
  const planMut = useMutation({ mutationFn: () => api.startMemoryBench({ ...request, dry_run: true }) })
  const plan = planMut.data?.plan
  const own = request.suite.startsWith('own:')
  const remote = (plan?.providers ?? []).filter(p => p.egress === 'remote')
  const needsAck = own && remote.length > 0
  const runMut = useMutation({
    mutationFn: () =>
      api.startMemoryBench({
        ...request,
        dry_run: false,
        egress_ack: needsAck
          ? Object.fromEntries(remote.map(p => [p.id, { app_version: p.app_version, items: plan?.items ?? 0 }]))
          : undefined,
      }),
    onSuccess: started => setRunId(started.run_id),
  })
  const runQuery = useQuery({
    queryKey: ['memory-bench-run', runId],
    queryFn: () => api.memoryBenchRun(runId),
    enabled: !!runId,
    refetchInterval: query => (query.state.data && !RUNNING.has(query.state.data.state) ? false : 1500),
  })
  const started = useRef(false)
  useEffect(() => {
    if (started.current) return
    started.current = true
    planMut.mutate()
  }, [planMut])
  const run = runQuery.data
  const close = () => onDone(runId ? { outcome: 'started', runId } : { outcome: 'cancelled' })
  const progress = run ? Object.values(run.progress) : []
  const done = progress.reduce((n, p) => n + p.done, 0)
  const total = progress.reduce((n, p) => n + p.total, 0)
  const failed = planMut.isError || runMut.isError || runQuery.isError || run?.state === 'failed'
  const overBudget = !!plan && plan.llm_calls_estimate > plan.max_llm_calls
  return (
    <Modal
      open
      onClose={close}
      title={t('pages.overview.memoryBench.title')}
      maxWidth={440}
      layer="top"
      dismissDisabled={runMut.isPending}
      footer={
        <SheetActions
          primary={
            runId ? (
              <Btn primary className="w-full justify-center py-2" onClick={close}>
                {t('pages.overview.memoryProvider.close')}
              </Btn>
            ) : (
              <Btn
                primary
                className="w-full justify-center py-2"
                busy={runMut.isPending}
                disabled={!plan || (needsAck && !acked)}
                onClick={() => runMut.mutate()}
              >
                {t('pages.overview.memoryBench.run')}
              </Btn>
            )
          }
          quiet={
            runId ? null : (
              <QuietBtn className="text-muted" onClick={close} disabled={runMut.isPending}>
                {t('pages.overview.memoryProvider.close')}
              </QuietBtn>
            )
          }
        />
      }
    >
      <div className="flex flex-col gap-3 text-sm" data-testid="memory-bench-sheet">
        {plan && <p className="text-text">{t('pages.overview.memoryBench.body', { n: fmtNumber(plan.queries) })}</p>}
        {planMut.isPending && (
          <p className="text-muted" role="status">
            {t('pages.overview.memoryBench.planning')}
          </p>
        )}
        {plan && (
          <>
            <SheetCard testId="memory-bench-rows">
              <SheetRow label={t('pages.overview.memoryBench.row_suite')}>
                {plan.suite.name}
              </SheetRow>
              <SheetRow label={t('pages.overview.memoryBench.row_questions')}>
                <span className="tabular-nums">{fmtNumber(plan.queries)}</span>
              </SheetRow>
              {plan.llm_calls_estimate > 0 && (
                <SheetRow label={t('pages.overview.memoryBench.row_calls')} warn={overBudget}>
                  {t('pages.overview.memoryBench.calls_value', {
                    n: fmtNumber(plan.llm_calls_estimate),
                    max: fmtNumber(plan.max_llm_calls),
                  })}
                </SheetRow>
              )}
              {plan.providers.map(row => (
                <ProviderRow key={row.id} row={row} />
              ))}
            </SheetCard>
            {plan.suite.restricted && plan.suite.license_note && (
              <p className="text-muted text-[12px]">{plan.suite.license_note}</p>
            )}
            {overBudget && <p className="text-warn-text text-[12px]">{t('pages.overview.memoryBench.over_budget')}</p>}
            {needsAck && !runId && (
              <AckBox>
                <label className="flex items-start gap-2" htmlFor="memory-bench-ack">
                  <Checkbox
                    id="memory-bench-ack"
                    className="mt-[3px] shrink-0"
                    checked={acked}
                    aria-label={t('pages.overview.memoryBench.ack', {
                      names: remote.map(p => p.name).join(', '),
                      n: fmtNumber(plan.items),
                    })}
                    onChange={e => setAcked(e.target.checked)}
                  />
                  <span aria-hidden="true">
                    {t('pages.overview.memoryBench.ack', {
                      names: remote.map(p => p.name).join(', '),
                      n: fmtNumber(plan.items),
                    })}
                  </span>
                </label>
              </AckBox>
            )}
            <p className="text-muted text-[12px]">{t('pages.overview.memoryBench.scratch_only')}</p>
          </>
        )}
        {run && RUNNING.has(run.state) && (
          <p className="text-muted" role="status">
            {t('pages.overview.memoryBench.running', { done: fmtNumber(done), total: fmtNumber(total) })}
          </p>
        )}
        {run?.state === 'done' && <p role="status">{t('pages.overview.memoryBench.done')}</p>}
        {run?.state === 'budget_exhausted' && <p role="status">{t('pages.overview.memoryBench.budget_exhausted')}</p>}
        {run && !RUNNING.has(run.state) && run.warnings.length > 0 && (
          <ErrorNotice variant="inline" impact="partial" askAgent onHandoff={close} title={t('pages.overview.memoryBench.warnings')} message={run.warnings.join('\n')} />
        )}
        {failed && <ErrorNotice variant="inline" askAgent onHandoff={close} message={t('pages.overview.memoryBench.failed')} />}
        <HostNote>{t('pages.overview.memoryBench.host_note')}</HostNote>
      </div>
    </Modal>
  )
}

/**
 * Mounts the sheet `requestMemoryBench()` opens. One per window; an ask that arrives
 * while one is open answers `'cancelled'`.
 */
export function MemoryBenchSheetHost() {
  const [request, setRequest] = useState<MemoryBenchRequest | null>(null)
  const settle = useRef<((outcome: MemoryBenchOutcome) => void) | null>(null)
  useEffect(() => {
    const off = registerMemoryBenchSheetHost(next => new Promise(resolve => {
      if (settle.current) {
        resolve({ outcome: 'cancelled' })
        return
      }
      settle.current = resolve
      setRequest(next)
    }))
    return () => {
      off()
      settle.current?.({ outcome: 'cancelled' })
      settle.current = null
    }
  }, [])
  if (!request) return null
  return (
    <MemoryBenchSheet
      request={request}
      onDone={outcome => {
        settle.current?.(outcome)
        settle.current = null
        setRequest(null)
      }}
    />
  )
}
