import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { MEMORY_MOVE_ACTIVE_STATES, type MemoryMove } from '../api/client/memory'
import { registerMemoryMoveSheetHost, type MemoryMoveOutcome, type MemoryMoveTarget } from '../app-sdk/memoryMove'
import { fmtNumber } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
import { MEMORY_PROVIDER_QUERY_KEY, providerName } from './MemoryProviderSheet'
import Modal from './Modal'
import { Btn } from './ui'

/** The planned or reached outcome of each item, in four groups the owner reads. */
type OutcomeGroup = 'write' | 'present' | 'refused' | 'skipped'

function groupOf(outcome: string): OutcomeGroup {
  if (outcome === 'would_write' || outcome === 'inserted' || outcome === 'enriched') return 'write'
  if (outcome === 'deduped' || outcome === 'unchanged') return 'present'
  if (outcome.startsWith('refused')) return 'refused'
  return 'skipped'
}

function groups(job: MemoryMove): Record<OutcomeGroup, number> {
  const out: Record<OutcomeGroup, number> = { write: 0, present: 0, refused: 0, skipped: 0 }
  for (const [outcome, n] of Object.entries(job.outcomes)) out[groupOf(outcome)] += n
  return out
}

/** Follow one job until it stops working. */
function useMove(id: string) {
  return useQuery({
    queryKey: [...MEMORY_PROVIDER_QUERY_KEY, 'move', id],
    queryFn: () => api.memoryMove(id),
    enabled: !!id,
    refetchInterval: query =>
      query.state.data && !MEMORY_MOVE_ACTIVE_STATES.has(query.state.data.state) ? false : 1000,
  })
}

function Counts({ job }: { job: MemoryMove }) {
  const { t } = useTranslation()
  const g = groups(job)
  const rows: [OutcomeGroup, string][] = [
    ['write', job.dry_run ? t('pages.overview.memoryMove.count_would_write') : t('pages.overview.memoryMove.count_written')],
    ['present', t('pages.overview.memoryMove.count_present')],
    ['refused', t('pages.overview.memoryMove.count_refused')],
    ['skipped', t('pages.overview.memoryMove.count_skipped')],
  ]
  return (
    <table className="text-[12px]">
      <tbody>
        {rows.map(([key, label]) => (
          <tr key={key}>
            <td className="pr-3 text-muted">{label}</td>
            <td className="text-right tabular-nums">{fmtNumber(g[key])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * The move sheet: plans a move from one provider into another, shows what it would
 * write per kind with samples, and runs it on the owner's press. A finished move can
 * be rolled back (only what it inserted is deleted); a paused or failed one resumes
 * from its checkpoint. A move never switches memory.
 */
export function MemoryMoveSheet({
  target,
  onDone,
}: {
  target: MemoryMoveTarget
  onDone: (outcome: MemoryMoveOutcome) => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const overview = useQuery({ queryKey: MEMORY_PROVIDER_QUERY_KEY, queryFn: () => api.memoryProvider() })
  const fromName = providerName(overview.data, target.from === 'builtin' ? undefined : target.from)
  const toName = providerName(overview.data, target.to === 'builtin' ? undefined : target.to)
  const [planId, setPlanId] = useState('')
  const [runId, setRunId] = useState('')
  const planMut = useMutation({
    mutationFn: () => api.startMemoryMove({ from: target.from, to: target.to, dry_run: true }),
    onSuccess: job => setPlanId(job.id),
  })
  const runMut = useMutation({
    mutationFn: () => api.startMemoryMove({ from: target.from, to: target.to }),
    onSuccess: job => setRunId(job.id),
  })
  const resumeMut = useMutation({
    mutationFn: () => api.resumeMemoryMove(runId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [...MEMORY_PROVIDER_QUERY_KEY, 'move', runId] }),
  })
  const rollbackMut = useMutation({
    mutationFn: () => api.rollbackMemoryMove(runId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: [...MEMORY_PROVIDER_QUERY_KEY, 'move', runId] }),
  })
  const started = useRef(false)
  useEffect(() => {
    if (started.current) return
    started.current = true
    planMut.mutate()
  }, [planMut])
  const plan = useMove(planId).data
  const runQuery = useMove(runId)
  const run = runQuery.data
  const planning = planMut.isPending || !plan || MEMORY_MOVE_ACTIVE_STATES.has(plan.state)
  const running = !!runId && (!run || MEMORY_MOVE_ACTIVE_STATES.has(run.state))
  const job = run ?? plan
  const close = () => onDone(run && (run.state === 'done' || run.state === 'rolled_back') ? 'moved' : 'cancelled')
  const canRun = !!plan && plan.state === 'planned' && groups(plan).write > 0 && !runId
  const canResume = !!run && (run.state === 'paused' || run.state === 'failed')
  const canRollback = !!run && run.state === 'done' && target.to !== 'builtin'
  const failed =
    planMut.isError ||
    runMut.isError ||
    runQuery.isError ||
    plan?.state === 'failed' ||
    run?.state === 'failed' ||
    run?.state === 'paused'
  return (
    <Modal
      open
      onClose={close}
      title={t('pages.overview.memoryMove.title', { from: fromName, to: toName })}
      maxWidth={560}
      layer="top"
      dismissDisabled={runMut.isPending}
      footer={
        <div className="flex justify-end gap-2">
          {canRollback && (
            <Btn busy={rollbackMut.isPending} onClick={() => rollbackMut.mutate()}>
              {t('pages.overview.memoryMove.rollback')}
            </Btn>
          )}
          {canResume && (
            <Btn busy={resumeMut.isPending} onClick={() => resumeMut.mutate()}>
              {t('pages.overview.memoryMove.resume')}
            </Btn>
          )}
          <Btn onClick={close} disabled={runMut.isPending}>
            {t('pages.overview.memoryProvider.close')}
          </Btn>
          {!runId && (
            <Btn primary busy={runMut.isPending} disabled={!canRun} onClick={() => runMut.mutate()}>
              {t('pages.overview.memoryMove.run')}
            </Btn>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm" data-testid="memory-move-sheet">
        <p>{t('pages.overview.memoryMove.body', { from: fromName, to: toName })}</p>
        {planning && !runId && <p className="text-muted">{t('pages.overview.memoryMove.planning')}</p>}
        {running && <p className="text-muted">{t('pages.overview.memoryMove.running')}</p>}
        {job && !MEMORY_MOVE_ACTIVE_STATES.has(job.state) && <Counts job={job} />}
        {plan && !runId && plan.state === 'planned' && (
          <div className="flex flex-col gap-1">
            {Object.entries(plan.samples ?? {}).map(([kind, samples]) => (
              <details key={kind}>
                <summary className="cursor-pointer text-[12px]">
                  {t('pages.overview.memoryMove.kind_count', { kind, n: fmtNumber(plan.by_kind[kind] ?? 0) })}
                </summary>
                <ul className="pl-4 text-[12px] text-muted">
                  {samples.slice(0, 5).map(sample => (
                    <li key={sample.id} className="truncate">
                      {sample.text}
                    </li>
                  ))}
                </ul>
              </details>
            ))}
          </div>
        )}
        {job?.notes.map(note => (
          <p key={note} className="text-muted text-[12px]">
            {note}
          </p>
        ))}
        {run?.state === 'done' && run.verify.checked !== undefined && (
          <p className="text-muted text-[12px]">
            {t('pages.overview.memoryMove.verified', {
              matched: run.verify.matched ?? 0,
              checked: run.verify.checked ?? 0,
            })}
          </p>
        )}
        {run?.state === 'rolled_back' && (
          <p className="text-muted text-[12px]">
            {t('pages.overview.memoryMove.rolled_back', { n: fmtNumber(run.rolled_back) })}
          </p>
        )}
        <p className="text-muted text-[12px]">{t('pages.overview.memoryMove.no_switch')}</p>
        {failed && (
          <ErrorNotice
            variant="inline"
            askAgent
            message={job?.error || t('pages.overview.memoryMove.failed')}
          />
        )}
      </div>
    </Modal>
  )
}

/**
 * Mounts the sheet `requestMemoryMove()` opens. One per window; an ask that arrives
 * while one is open answers `'cancelled'`.
 */
export function MemoryMoveSheetHost() {
  const [target, setTarget] = useState<MemoryMoveTarget | null>(null)
  const settle = useRef<((outcome: MemoryMoveOutcome) => void) | null>(null)
  useEffect(() => {
    const off = registerMemoryMoveSheetHost(next => new Promise(resolve => {
      if (settle.current) {
        resolve('cancelled')
        return
      }
      settle.current = resolve
      setTarget(next)
    }))
    return () => {
      off()
      settle.current?.('cancelled')
      settle.current = null
    }
  }, [])
  if (!target) return null
  return (
    <MemoryMoveSheet
      target={target}
      onDone={outcome => {
        settle.current?.(outcome)
        settle.current = null
        setTarget(null)
      }}
    />
  )
}
