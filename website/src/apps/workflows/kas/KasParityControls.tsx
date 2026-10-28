/**
 * The run and step controls Kiro Agent offers past pause / resume / stop:
 *
 *   - a parked run's reason line (transient error, retries used up, loop used
 *     up, waiting for a reply, waiting on plan approval);
 *   - an exhausted loop's two ways out: Grant more passes (with a count) and
 *     End the loop and continue;
 *   - Retry (every failed step) and Run again on a finished run;
 *   - per step: Mark done / Mark failed on a paused step, Stop on a running
 *     one, Retry on a failed one.
 *
 * Every button is drawn only when the backend lists it in `run.actions` or
 * `node.actions` (see `kasModel.ts`); nothing here guesses from a status.
 */
import { useId, useState } from 'react'
import { AlertTriangle, Check, CirclePlay, RotateCcw, RotateCw, Square, X } from 'lucide-react'

import type { KasRunNode, KasRunRow } from '../../../api/client'
import { Btn, IconButton, Input } from '../../../components/ui'
import { useConfirm } from '../../../components/ConfirmDialog'
import { i18nT } from '../../../i18n/t'
import { GRANT_DEFAULT, GRANT_MAX, exhaustedLoop, nodeCan, parseGrantCount, pauseLine, runCan, runTitle, stepRetryable } from './kasModel'
import { useKasParityActions } from './useKasWorkflows'
import { useWithdrawStaleStop } from './KasRunBits'

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof Error ? error.message : String(error)
}

function InlineError({ error }: { error: unknown }) {
  const text = errorText(error)
  if (!text) return null
  return (
    <p className="m-0 text-[11.5px] text-danger" role="alert" data-testid="kas-parity-error">
      {text}
    </p>
  )
}

/** Why the run is parked, with KAS's own detail muted after it. */
export function KasPauseReasonLine({ row }: { row: KasRunRow }) {
  const line = pauseLine(row)
  if (!line) return null
  const warn = line.reason !== 'user'
  return (
    <p
      className={`m-0 text-[12px] flex items-start gap-1.5 min-w-0 ${warn ? 'text-warn' : 'text-muted'}`}
      data-testid="kas-pause-reason"
      data-reason={line.reason}
    >
      {warn ? <AlertTriangle size={12} className="shrink-0 mt-[2px]" aria-hidden="true" /> : null}
      <span className="min-w-0 break-words">
        {line.text}
        {line.detail ? <span className="text-muted"> · {line.detail}</span> : null}
      </span>
    </p>
  )
}

/** The exhausted loop's strip: Grant N more passes, or End the loop and continue. */
export function KasLoopExhaustedControls({ row }: { row: KasRunRow }) {
  const loop = exhaustedLoop(row)
  const { step } = useKasParityActions()
  const [count, setCount] = useState(String(GRANT_DEFAULT))
  const inputId = useId()
  if (!loop || (!loop.canGrant && !loop.canEnd)) return null
  const parsed = parseGrantCount(count)
  const busy = step.isPending && step.variables?.workflowId === row.workflowId
  const grant = () => {
    if (parsed == null) return
    step.mutate({ workflowId: row.workflowId, nodeId: loop.nodeId, path: loop.path, verb: 'grant', iterations: parsed })
  }
  const end = () =>
    step.mutate({ workflowId: row.workflowId, nodeId: loop.nodeId, path: loop.path, verb: 'complete', status: 'completed' })
  return (
    <div
      className="flex flex-col gap-1.5 rounded-md border border-warn/50 bg-warn-subtle px-2.5 py-2"
      data-testid="kas-loop-exhausted"
      role="group"
      aria-label={i18nT('apps.workflows.kas.loop_exhausted_group', { loop: loop.label })}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        {loop.canGrant ? (
          <>
            <label htmlFor={inputId} className="sr-only">
              {i18nT('apps.workflows.kas.grant_count')}
            </label>
            <span className="w-16 shrink-0">
              <Input
                id={inputId}
                type="number"
                min={1}
                max={GRANT_MAX}
                step={1}
                inputMode="numeric"
                value={count}
                onChange={e => setCount(e.target.value)}
                aria-invalid={parsed == null || undefined}
                className="px-2 py-1 text-[12px] tabular-nums"
                data-testid="kas-grant-count"
              />
            </span>
            <Btn primary onClick={grant} disabled={busy || parsed == null} data-testid="kas-grant">
              <CirclePlay className="lucide-inline" /> {i18nT('apps.workflows.kas.grant_more')}
            </Btn>
          </>
        ) : null}
        {loop.canEnd ? (
          <Btn onClick={end} disabled={busy} data-testid="kas-end-loop">
            {i18nT('apps.workflows.kas.end_loop')}
          </Btn>
        ) : null}
      </div>
      <InlineError error={step.error} />
    </div>
  )
}

/** Retry (every failed step) and Run again, from `run.actions`. */
export function KasRunRecoveryButtons({ row, compact = false }: { row: KasRunRow; compact?: boolean }) {
  const { retry, restart } = useKasParityActions()
  const { confirm, confirmDialog } = useConfirm()
  const canRetry = runCan(row, 'retry')
  const canRestart = runCan(row, 'restart')
  if (!canRetry && !canRestart) return confirmDialog
  const busy =
    (retry.isPending && retry.variables?.workflowId === row.workflowId) ||
    (restart.isPending && restart.variables?.workflowId === row.workflowId)
  const again = async () => {
    const ok = await confirm({
      title: i18nT('apps.workflows.kas.run_again_title'),
      body: (
        <>
          <p className="m-0 mb-2 font-semibold text-text-strong">{runTitle(row)}</p>
          <p className="m-0">{i18nT('apps.workflows.kas.run_again_body')}</p>
        </>
      ),
      confirmLabel: i18nT('apps.workflows.kas.run_again'),
    })
    if (ok) restart.mutate({ workflowId: row.workflowId })
  }
  const failure = retry.error ?? restart.error
  if (compact) {
    return (
      <span className="inline-flex items-center gap-0.5 shrink-0" data-testid="kas-run-recovery">
        {canRetry ? (
          <IconButton aria-label={i18nT('apps.workflows.kas.retry_failed')} title={i18nT('apps.workflows.kas.retry_failed')} onClick={() => retry.mutate({ workflowId: row.workflowId })} disabled={busy}>
            <RotateCcw size={13} aria-hidden="true" />
          </IconButton>
        ) : null}
        {canRestart ? (
          <IconButton aria-label={i18nT('apps.workflows.kas.run_again')} title={i18nT('apps.workflows.kas.run_again')} onClick={() => void again()} disabled={busy}>
            <RotateCw size={13} aria-hidden="true" />
          </IconButton>
        ) : null}
        {errorText(failure) ? (
          <span role="img" className="p-[4px] text-danger inline-flex" aria-label={errorText(failure) ?? ''} title={errorText(failure) ?? ''}>
            <AlertTriangle size={13} aria-hidden="true" />
          </span>
        ) : null}
        {confirmDialog}
      </span>
    )
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5" data-testid="kas-run-recovery">
      {canRetry ? (
        <Btn onClick={() => retry.mutate({ workflowId: row.workflowId })} disabled={busy}>
          <RotateCcw className="lucide-inline" /> {i18nT('apps.workflows.kas.retry_failed')}
        </Btn>
      ) : null}
      {canRestart ? (
        <Btn onClick={() => void again()} disabled={busy}>
          <RotateCw className="lucide-inline" /> {i18nT('apps.workflows.kas.run_again')}
        </Btn>
      ) : null}
      <InlineError error={failure} />
      {confirmDialog}
    </span>
  )
}

/** Mark a paused step done or failed, so the run moves on. Beside the reply box. */
export function KasStepDecisionButtons({ row, node }: { row: KasRunRow; node: KasRunNode }) {
  const { step } = useKasParityActions()
  const canDone = nodeCan(node, 'complete')
  const canFail = nodeCan(node, 'fail')
  if (!canDone && !canFail) return null
  const busy = step.isPending && step.variables?.workflowId === row.workflowId && step.variables?.nodeId === node.id
  const mark = (status: 'completed' | 'failed') =>
    step.mutate({ workflowId: row.workflowId, nodeId: node.id, path: node.path, verb: 'complete', status })
  return (
    <div className="flex flex-col gap-1" data-testid="kas-step-decision">
      <div className="flex flex-wrap items-center gap-1.5">
        {canDone ? (
          <Btn onClick={() => mark('completed')} disabled={busy} data-testid="kas-mark-done">
            <Check className="lucide-inline" /> {i18nT('apps.workflows.kas.mark_done')}
          </Btn>
        ) : null}
        {canFail ? (
          <Btn danger onClick={() => mark('failed')} disabled={busy} data-testid="kas-mark-failed">
            <X className="lucide-inline" /> {i18nT('apps.workflows.kas.mark_failed')}
          </Btn>
        ) : null}
      </div>
      <InlineError error={step.error} />
    </div>
  )
}

/** The small per-step controls in the STEPS tree: Stop (running) and Retry (failed). */
export function KasTreeStepControls({ row, node, label }: { row: KasRunRow; node: KasRunNode; label?: string }) {
  const name = label ?? node.label
  const { step, retry } = useKasParityActions()
  const { confirm, confirmDialog, cancel: withdraw } = useConfirm()
  const canCancel = nodeCan(node, 'cancel')
  const canRetry = stepRetryable(row, node)
  // Gap 25, step form: a step that ended while its Stop confirm was open
  // withdraws the dialog. Called before the early return (rules of hooks).
  const stopAsk = useWithdrawStaleStop(canCancel, withdraw)
  if (!canCancel && !canRetry) return confirmDialog
  const busy =
    (step.isPending && step.variables?.nodeId === node.id && step.variables?.workflowId === row.workflowId) ||
    (retry.isPending && retry.variables?.nodeId === node.id && retry.variables?.workflowId === row.workflowId)
  const stop = async () => {
    stopAsk.current = true
    let ok = false
    try {
      ok = await confirm({
        title: i18nT('apps.workflows.kas.stop_step_title'),
        body: (
          <>
            <p className="m-0 mb-2 font-semibold text-text-strong">{name}</p>
            <p className="m-0">{i18nT('apps.workflows.kas.stop_step_body')}</p>
          </>
        ),
        confirmLabel: i18nT('apps.workflows.kas.stop_step'),
      })
    } finally {
      stopAsk.current = false
    }
    if (ok) step.mutate({ workflowId: row.workflowId, nodeId: node.id, path: node.path, verb: 'cancel' })
  }
  const failure = errorText(step.error ?? retry.error)
  return (
    <span className="inline-flex items-center shrink-0" data-testid="kas-tree-step-controls">
      {canCancel ? (
        <IconButton
          variant="danger"
          aria-label={i18nT('apps.workflows.kas.stop_step_named', { step: name })}
          title={i18nT('apps.workflows.kas.stop_step')}
          onClick={() => void stop()}
          disabled={busy}
          data-testid="kas-stop-step"
        >
          <Square size={11} aria-hidden="true" />
        </IconButton>
      ) : null}
      {canRetry ? (
        <IconButton
          aria-label={i18nT('apps.workflows.kas.retry_step_named', { step: name })}
          title={i18nT('apps.workflows.kas.retry_step')}
          onClick={() => retry.mutate({ workflowId: row.workflowId, nodeId: node.id, path: node.path })}
          disabled={busy}
          data-testid="kas-retry-step"
        >
          <RotateCcw size={12} aria-hidden="true" />
        </IconButton>
      ) : null}
      {failure ? (
        <span role="img" className="p-[4px] text-danger inline-flex" aria-label={failure} title={failure}>
          <AlertTriangle size={12} aria-hidden="true" />
        </span>
      ) : null}
      {confirmDialog}
    </span>
  )
}
