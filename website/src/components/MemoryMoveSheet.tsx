import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { MEMORY_MOVE_ACTIVE_STATES, type MemoryMove } from '../api/client/memory'
import { registerMemoryMoveSheetHost, type MemoryMoveOutcome, type MemoryMoveTarget } from '../app-sdk/memoryMove'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import ErrorNotice from './ErrorNotice'
import { MEMORY_PROVIDER_QUERY_KEY, providerName, providerRow } from './MemoryProviderSheet'
import Modal from './Modal'
import { AckBox, HostNote, QuietBtn, SheetActions, SheetCard, SheetRow } from './memorySheetParts'
import { Btn, Checkbox } from './ui'

/** A contract kind (`rule`, `fact`, `document`, ...) in the reader's words. */
export function memoryKindLabel(kind: string): string {
  switch (kind) {
    case 'preference':
      return i18nT('pages.overview.memoryMove.kind.preference')
    case 'fact':
      return i18nT('pages.overview.memoryMove.kind.fact')
    case 'episode':
      return i18nT('pages.overview.memoryMove.kind.episode')
    case 'summary':
      return i18nT('pages.overview.memoryMove.kind.summary')
    case 'rule':
      return i18nT('pages.overview.memoryMove.kind.rule')
    case 'finding':
      return i18nT('pages.overview.memoryMove.kind.finding')
    case 'index':
      return i18nT('pages.overview.memoryMove.kind.index')
    case 'note':
      return i18nT('pages.overview.memoryMove.kind.note')
    case 'document':
      return i18nT('pages.overview.memoryMove.kind.document')
    default:
      return kind
  }
}

/** The planned or reached outcome of each item, in four groups the owner reads. */
type OutcomeGroup = 'write' | 'present' | 'refused' | 'conflict' | 'skipped'

function groupOf(outcome: string): OutcomeGroup {
  if (outcome === 'would_write' || outcome === 'inserted' || outcome === 'enriched') return 'write'
  if (outcome === 'deduped' || outcome === 'unchanged') return 'present'
  if (outcome.startsWith('refused')) return 'refused'
  // Edited on both sides since an earlier copy: left as it is, and the owner must know.
  if (outcome.startsWith('conflict:')) return 'conflict'
  return 'skipped'
}

function groups(job: MemoryMove): Record<OutcomeGroup, number> {
  const out: Record<OutcomeGroup, number> = { write: 0, present: 0, refused: 0, conflict: 0, skipped: 0 }
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

/** The plan's or run's counts, as rows of the sheet card after its two ends. */
function CountRows({ job }: { job: MemoryMove }) {
  const { t } = useTranslation()
  const g = groups(job)
  const rows: [OutcomeGroup, string][] = [
    ['write', job.dry_run ? t('pages.overview.memoryMove.count_would_write') : t('pages.overview.memoryMove.count_written')],
    // A plan counts what is already there when it runs; the copy may find more.
    ['present', job.dry_run ? t('pages.overview.memoryMove.count_present_plan') : t('pages.overview.memoryMove.count_present')],
    ['refused', t('pages.overview.memoryMove.count_refused')],
    ['conflict', t('pages.overview.memoryMove.count_conflict')],
    ['skipped', t('pages.overview.memoryMove.count_skipped')],
  ]
  return (
    <>
      {rows.map(([key, label]) => (
        <SheetRow key={key} label={label}>
          <span className="tabular-nums">{fmtNumber(g[key])}</span>
        </SheetRow>
      ))}
      {job.rolled_back > 0 && (
        <SheetRow label={t('pages.overview.memoryMove.count_removed')}>
          <span className="tabular-nums">{fmtNumber(job.rolled_back)}</span>
        </SheetRow>
      )}
    </>
  )
}

/** Every kind the plan would copy, with its full count; the items under each are samples. */
function KindList({ plan }: { plan: MemoryMove }) {
  return (
    <ul className="flex flex-col gap-1.5" data-testid="memory-move-kinds">
      {Object.entries(plan.by_kind).map(([kind, count]) => {
        const samples = plan.samples?.[kind] ?? []
        const head = (
          <>
            <span className="min-w-0 truncate">{memoryKindLabel(kind)}</span>
            <span className="tabular-nums text-muted">{fmtNumber(count)}</span>
          </>
        )
        return (
          <li key={kind} className="text-[13px]">
            {samples.length ? (
              <details>
                <summary className="flex cursor-pointer items-center justify-between gap-3">{head}</summary>
                <ul className="mt-1 flex flex-col gap-1 pl-3 text-[12px] text-muted">
                  {samples.slice(0, 5).map(sample => (
                    <li key={sample.id} className="truncate">
                      {sample.text}
                    </li>
                  ))}
                </ul>
              </details>
            ) : (
              <div className="flex items-center justify-between gap-3">{head}</div>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/**
 * The move sheet: plans a move from one provider into another, shows what it would
 * write per kind with samples, and runs it on the owner's press. A finished move can
 * be rolled back (only what it inserted is deleted); a paused or failed one resumes
 * from its checkpoint. A move never switches memory. *runId* reopens an unfinished
 * move (from the picker's list) instead of planning a new one.
 */
export function MemoryMoveSheet({
  target,
  onDone,
  runId: openRunId = '',
}: {
  target: MemoryMoveTarget
  onDone: (outcome: MemoryMoveOutcome) => void
  runId?: string
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const overview = useQuery({ queryKey: MEMORY_PROVIDER_QUERY_KEY, queryFn: () => api.memoryProvider() })
  const fromName = providerName(overview.data, target.from === 'builtin' ? undefined : target.from)
  const toName = providerName(overview.data, target.to === 'builtin' ? undefined : target.to)
  const [planId, setPlanId] = useState('')
  const [runId, setRunId] = useState(openRunId)
  // A copy into a remote provider sends memory off this machine, so it needs the owner's
  // consent; the run records it (a plan sends nothing and needs none).
  const toRow = target.to === 'builtin' ? undefined : providerRow(overview.data, target.to)
  const needsEgress = toRow?.capabilities?.egress === 'remote' && !toRow.egress_consented
  const [egress, setEgress] = useState(false)
  const planMut = useMutation({
    mutationFn: () => api.startMemoryMove({ from: target.from, to: target.to, dry_run: true }),
    onSuccess: job => setPlanId(job.id),
  })
  const runMut = useMutation({
    mutationFn: () =>
      api.startMemoryMove({ from: target.from, to: target.to, ...(needsEgress ? { egress_consent: egress } : {}) }),
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
    if (!openRunId) planMut.mutate()
  }, [planMut, openRunId])
  const planQuery = useMove(planId)
  const plan = planQuery.data
  const runQuery = useMove(runId)
  const run = runQuery.data
  const planning =
    !planMut.isError && !planQuery.isError && (planMut.isPending || !plan || MEMORY_MOVE_ACTIVE_STATES.has(plan.state))
  const running = !!runId && (!run || MEMORY_MOVE_ACTIVE_STATES.has(run.state))
  const job = run ?? plan
  // What the app is told. An undo counts from the owner's press, not from the cached
  // job, which still reads 'done' until the refetch lands; a refused undo leaves the
  // copy in place. A copy still going when the sheet closes keeps writing: 'started'.
  const undoing = rollbackMut.isPending || rollbackMut.isSuccess || run?.state === 'rolling_back'
  const close = () => {
    if (undoing || run?.state === 'rolled_back') onDone('cancelled')
    else if (run?.state === 'done') onDone('moved')
    else if (running || resumeMut.isPending || resumeMut.isSuccess) onDone('started')
    else onDone('cancelled')
  }
  // Copy waits for the overview too: without it the sheet cannot name both ends.
  const canRun =
    !!plan && plan.state === 'planned' && groups(plan).write > 0 && !runId && !!overview.data && (!needsEgress || egress)
  const canResume = !!run && (run.state === 'paused' || run.state === 'failed')
  // A paused or failed copy that already wrote items can be undone too: the undo needs
  // only the target, while a resume needs the source back. A copy that inserted nothing
  // has nothing to undo.
  const canRollback =
    !!run &&
    target.to !== 'builtin' &&
    ((run.state === 'done' && (run.outcomes.inserted ?? 0) > 0) ||
      run.state === 'rollback_failed' ||
      (canResume && (run.outcomes.inserted ?? 0) > 0))
  // Every request the sheet makes can fail, and each failure is shown: the plan, its
  // polling, the run, resume, undo, and the overview the names come from.
  const failed =
    planMut.isError ||
    planQuery.isError ||
    runMut.isError ||
    runQuery.isError ||
    resumeMut.isError ||
    rollbackMut.isError ||
    overview.isError ||
    plan?.state === 'failed' ||
    run?.state === 'failed' ||
    run?.state === 'paused' ||
    run?.state === 'rollback_failed'
  // Localized and actionable: the gateway's own error text is English and names
  // internal states, so it is never shown as the message.
  const inserted = run?.outcomes.inserted ?? 0
  const failureMessage =
    run?.state === 'rollback_failed'
      ? t('pages.overview.memoryMove.undo_failed', {
          name: toName,
          removed: fmtNumber(run.rolled_back),
          total: fmtNumber(inserted),
        })
      : run?.state === 'paused'
        ? t('pages.overview.memoryMove.paused_notice', { name: toName })
        : run?.state === 'failed' && ((run.verify.mismatched ?? 0) > 0 || (run.verify.unread ?? 0) > 0)
          ? t('pages.overview.memoryMove.verify_failed', { name: toName })
          : t('pages.overview.memoryMove.failed')
  // One primary action at a time: start the copy, else resume a stopped one, else close.
  const primary = !runId ? (
    <Btn primary className="w-full justify-center py-2" busy={runMut.isPending} disabled={!canRun} onClick={() => runMut.mutate()}>
      {t('pages.overview.memoryMove.run')}
    </Btn>
  ) : canResume ? (
    <Btn primary className="w-full justify-center py-2" busy={resumeMut.isPending} onClick={() => resumeMut.mutate()}>
      {t('pages.overview.memoryMove.resume')}
    </Btn>
  ) : (
    <Btn primary className="w-full justify-center py-2" onClick={close}>
      {t('pages.overview.memoryProvider.close')}
    </Btn>
  )
  const closeIsPrimary = !!runId && !canResume
  return (
    <Modal
      open
      onClose={close}
      title={t('pages.overview.memoryMove.title', { from: fromName, to: toName })}
      maxWidth={440}
      layer="top"
      dismissDisabled={runMut.isPending}
      footer={
        <SheetActions
          primary={primary}
          quiet={
            <>
              {canRollback && (
                <QuietBtn busy={rollbackMut.isPending} onClick={() => rollbackMut.mutate()}>
                  {run?.state === 'rollback_failed'
                    ? t('pages.overview.memoryMove.retry_undo')
                    : t('pages.overview.memoryMove.rollback', { to: toName })}
                </QuietBtn>
              )}
              {!closeIsPrimary && (
                <QuietBtn className="text-muted" onClick={close} disabled={runMut.isPending}>
                  {t('pages.overview.memoryProvider.close')}
                </QuietBtn>
              )}
            </>
          }
        />
      }
    >
      <div className="flex flex-col gap-3 text-sm" data-testid="memory-move-sheet">
        <p className="text-text">{t('pages.overview.memoryMove.body', { from: fromName, to: toName })}</p>
        <SheetCard testId="memory-move-rows">
          <SheetRow label={t('pages.overview.memoryProvider.row_source')}>{fromName}</SheetRow>
          <SheetRow label={t('pages.overview.memoryProvider.row_destination')}>{toName}</SheetRow>
          {job && !MEMORY_MOVE_ACTIVE_STATES.has(job.state) && job.state !== 'rolled_back' && <CountRows job={job} />}
        </SheetCard>
        {plan && !runId && plan.state === 'planned' && Object.keys(plan.by_kind).length > 0 && <KindList plan={plan} />}
        {needsEgress && !runId && (
          <AckBox>
            <label className="flex items-start gap-2">
              <Checkbox className="mt-[3px] shrink-0" checked={egress} onChange={e => setEgress(e.target.checked)} />
              <span>{t('pages.overview.memoryProvider.egress_consent', { name: toName })}</span>
            </label>
          </AckBox>
        )}
        {planning && !runId && (
          <p className="text-muted" role="status">
            {t('pages.overview.memoryMove.planning')}
          </p>
        )}
        {running && (
          <p className="text-muted" role="status">
            {t('pages.overview.memoryMove.running')}
          </p>
        )}
        {job?.notes.map(note => (
          <p key={note} className="text-muted text-[12px]">
            {note}
          </p>
        ))}
        {run?.state === 'done' && (
          <p className="text-text" role="status">
            {t('pages.overview.memoryMove.done', {
              n: fmtNumber(groups(run).write),
              m: fmtNumber(groups(run).present),
            })}
          </p>
        )}
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
            {t('pages.overview.memoryMove.rolled_back', { n: fmtNumber(run.rolled_back), to: toName })}
          </p>
        )}
        {canRollback && <p className="text-muted text-[12px]">{t('pages.overview.memoryMove.untouched')}</p>}
        {(run?.state === 'paused' || run?.state === 'rollback_failed' || run?.state === 'failed') && (
          <p className="text-muted text-[12px]">{t('pages.overview.memoryMove.close_later')}</p>
        )}
        <p className="text-muted text-[12px]">{t('pages.overview.memoryMove.no_switch')}</p>
        {failed && (
          <ErrorNotice
            variant="inline"
            askAgent
            onHandoff={close}
            message={failureMessage}
          />
        )}
        <HostNote>{t('pages.overview.memoryMove.host_note')}</HostNote>
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
