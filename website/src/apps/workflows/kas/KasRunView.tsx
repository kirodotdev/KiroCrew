/**
 * The shared view of KAS-native workflow runs: one card per run with its state,
 * step progress, step tree, controls and any approvals waiting on it.
 *
 * Used by Customize > Workflows (Kiro Agent mode) and the Task Runner's
 * Workflows section. Every control follows `row.controls` exactly (see
 * `kasModel.ts`): one primary button, Stop separate and always confirmed,
 * Delete only on a finished run, nothing else on a finished run.
 */
import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  Loader2,
  MessageSquare,
  Pause,
  PauseCircle,
  Play,
  PlugZap,
  ShieldQuestion,
  Square,
  Trash2,
  XCircle,
} from 'lucide-react'

import type { KasApproval, KasRunRow } from '../../../api/client'
import { Badge, Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useAppDispatch } from '../../../store'
import { openActivityToTab, switchSlot } from '../../../store/chatSlice'
import { i18nT } from '../../../i18n/t'
import {
  approvalsFor,
  canDelete,
  canStop,
  isRejectOption,
  orderRuns,
  pauseLine,
  planStepLines,
  planTreeNodes,
  primaryControl,
  runBadge,
  runTitle,
  showNeedsYouPill,
  showReconnect,
  stepDisplayLabel,
  stepProgress,
} from './kasModel'
import { KasLoopExhaustedControls, KasPauseReasonLine, KasRunRecoveryButtons } from './KasParityControls'
import { KasStepsTree } from './KasStepsTree'
import { useWithdrawStaleStop } from './KasRunBits'
import { setKasSelection } from './kasSelection'
import { useKasRunActions } from './useKasWorkflows'

/** Open a dashboard chat by key, the way every other "Go to chat" does. */
export function useOpenChat(): (slotKey: string) => void {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  return useCallback(
    (slotKey: string) => {
      // A person's click on "Open chat", like every other run card's link.
      dispatch(switchSlot({ key: slotKey, announceOnMissing: true, gesture: true }))
      navigate('/chat')
    },
    [dispatch, navigate],
  )
}

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof Error ? error.message : String(error)
}

export function KasRunList({
  runs,
  approvals,
  emptyText,
  limit,
  inChat = false,
}: {
  runs: readonly KasRunRow[]
  approvals: readonly KasApproval[]
  emptyText: string
  limit?: number
  /** The list sits in the runs' own chat (see `KasRunCard`). */
  inChat?: boolean
}) {
  // The same order as the chat panel (gap 22): live first by creation, so a
  // step event never moves a row, and a live run never sits below a finished one.
  const ordered = orderRuns(runs)
  const shown = limit ? ordered.slice(0, limit) : ordered
  if (shown.length === 0) {
    return (
      <p className="text-[12px] text-muted border border-dashed border-border rounded p-4" data-testid="kas-runs-empty">
        {emptyText}
      </p>
    )
  }
  return (
    <ul className="flex flex-col gap-2" aria-label={i18nT('apps.workflows.kas.runs')}>
      {shown.map(row => (
        <li key={row.workflowId}>
          <KasRunCard row={row} approvals={approvalsFor(approvals, row.workflowId)} inChat={inChat} />
        </li>
      ))}
    </ul>
  )
}

export function KasRunCard({
  row,
  approvals,
  inChat = false,
}: {
  row: KasRunRow
  approvals: readonly KasApproval[]
  /** Drawn inside the run's own chat: no "Open chat" or "Respond in chat"
   *  button, since the reader is already there and the question is above. */
  inChat?: boolean
}) {
  const { control, answer, reconnect } = useKasRunActions()
  const { confirm, confirmDialog, cancel: withdraw } = useConfirm()
  const stopAsk = useWithdrawStaleStop(canStop(row), withdraw)
  const dispatch = useAppDispatch()
  const openChat = useOpenChat()
  const badge = runBadge(row)
  const progress = stepProgress(row)
  const primary = primaryControl(row)
  const title = runTitle(row)
  const busy = control.isPending && control.variables?.workflowId === row.workflowId

  const act = (action: 'pause' | 'resume' | 'cancel' | 'delete') =>
    control.mutate({ workflowId: row.workflowId, action })

  const stop = async () => {
    stopAsk.current = true
    let ok = false
    try {
      ok = await confirm({
        title: i18nT('apps.workflows.kas.stop_title'),
        body: (
          <>
            <p className="m-0 mb-2 font-semibold text-text-strong">{title}</p>
            <p className="m-0">{i18nT('apps.workflows.kas.stop_body_warn')}</p>
          </>
        ),
        confirmLabel: i18nT('apps.workflows.kas.stop_run'),
      })
    } finally {
      stopAsk.current = false
    }
    // Gap 25: a run that ended while the dialog was open withdrew it.
    if (ok) act('cancel')
  }

  const remove = async () => {
    const ok = await confirm({
      title: i18nT('apps.workflows.kas.delete_title'),
      body: (
        <>
          <p className="m-0 mb-2 font-semibold text-text-strong">{title}</p>
          <p className="m-0">{i18nT('apps.workflows.kas.delete_body')}</p>
        </>
      ),
      confirmLabel: i18nT('apps.workflows.kas.delete_run'),
    })
    if (ok) act('delete')
  }

  const failure = errorText(control.error ?? answer.error ?? reconnect.error)

  return (
    <article
      className="border border-border rounded-lg bg-bg-elevated p-3 flex flex-col gap-2"
      data-testid="kas-run-card"
      data-workflow-id={row.workflowId}
      aria-label={title}
    >
      <header className="flex flex-wrap items-center gap-2">
        <StateIcon display={row.controls.display} />
        <span className="font-semibold text-[13px] text-text-strong truncate min-w-0">{title}</span>
        <Badge variant={badge.variant} data-testid="kas-run-state">{badge.label}</Badge>
        {showNeedsYouPill(row) ? (
          <Badge variant="warn" data-testid="kas-needs-you">{i18nT('apps.workflows.kas.state_needs_you')}</Badge>
        ) : null}
        {progress ? (
          <span className="text-[12px] text-muted tabular-nums" data-testid="kas-run-progress">
            {i18nT('apps.workflows.kas.step_progress', { done: progress.done, total: progress.total })}
          </span>
        ) : null}
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          {primary?.kind === 'pause' ? (
            <Btn onClick={() => act('pause')} disabled={busy}>
              <Pause className="lucide-inline" /> {i18nT('apps.workflows.kas.pause')}
            </Btn>
          ) : primary?.kind === 'resume' ? (
            <Btn primary onClick={() => act('resume')} disabled={busy}>
              <Play className="lucide-inline" /> {i18nT('apps.workflows.kas.resume')}
            </Btn>
          ) : primary?.kind === 'open' && !inChat ? (
            <Btn primary onClick={() => row.slotKey && openChat(row.slotKey)} disabled={!row.slotKey}>
              <MessageSquare className="lucide-inline" /> {i18nT('apps.workflows.kas.respond_in_chat')}
            </Btn>
          ) : primary?.kind === 'pausing' ? (
            <Btn disabled aria-busy="true">
              <Loader2 className="lucide-inline animate-spin" /> {i18nT('apps.workflows.kas.pausing')}
            </Btn>
          ) : null}
          {showReconnect(row) ? (
            <Btn
              onClick={() => row.slotKey && reconnect.mutate(row.slotKey)}
              disabled={reconnect.isPending}
              title={i18nT('apps.workflows.kas.reconnect_hint')}
            >
              <PlugZap className="lucide-inline" /> {i18nT('apps.workflows.kas.reconnect')}
            </Btn>
          ) : null}
          {canStop(row) ? (
            <Btn danger onClick={() => void stop()} disabled={busy}>
              <Square className="lucide-inline" /> {i18nT('apps.workflows.kas.stop')}
            </Btn>
          ) : null}
          <KasRunRecoveryButtons row={row} />
          {canDelete(row) ? (
            <Btn danger onClick={() => void remove()} disabled={busy}>
              <Trash2 className="lucide-inline" /> {i18nT('apps.workflows.kas.delete')}
            </Btn>
          ) : null}
          {row.slotKey && primary?.kind !== 'open' && !inChat ? (
            <Btn onClick={() => openChat(row.slotKey!)}>
              <MessageSquare className="lucide-inline" /> {i18nT('apps.workflows.kas.open_chat')}
            </Btn>
          ) : null}
        </div>
      </header>

      {row.blocking ? (
        <p className="m-0 text-[12px] text-warn flex items-center gap-1.5">
          <AlertTriangle className="lucide-inline" />
          {row.blocking.kind === 'question'
            ? i18nT('apps.workflows.kas.waiting_question', { step: stepDisplayLabel(row.nodes, row.blocking.nodeId, row.blocking.label) })
            : i18nT('apps.workflows.kas.waiting_permission', { step: stepDisplayLabel(row.nodes, row.blocking.nodeId, row.blocking.label) })}
        </p>
      ) : null}
      {pauseLine(row) ? (
        <KasPauseReasonLine row={row} />
      ) : row.pauseReason && row.controls.display !== 'running' ? (
        <p className="m-0 text-[12px] text-muted">{row.pauseReason}</p>
      ) : null}
      <KasLoopExhaustedControls row={row} />

      {approvals.map(approval => (
        <KasApprovalCard
          key={approval.key}
          approval={approval}
          pending={answer.isPending && answer.variables?.key === approval.key}
          onAnswer={optionId => answer.mutate({ key: approval.key, optionId })}
        />
      ))}

      {row.nodes.length > 0 ? (
        <KasStepsTree
          nodes={row.nodes}
          selectedPath={null}
          row={row}
          onSelect={(nodeId, path) => {
            // Open the run's chat on that step; a run with no chat has inert rows.
            if (!row.slotKey) return
            setKasSelection(row.slotKey, { workflowId: row.workflowId, nodeId, path })
            dispatch(openActivityToTab('workflows'))
            openChat(row.slotKey)
          }}
        />
      ) : null}

      {/* No hand-off: this card sits beside the recipe launch form, whose
          unfilled inputs the hand-off would unmount. */}
      <ErrorNotice message={failure} variant="inline" />
      {confirmDialog}
    </article>
  )
}

function StateIcon({ display }: { display: string }) {
  switch (display) {
    case 'running':
    case 'pausing':
      return <Loader2 size={14} className="text-accent animate-spin shrink-0" aria-hidden="true" />
    case 'paused':
      return <PauseCircle size={14} className="text-warn shrink-0" aria-hidden="true" />
    case 'completed':
      return <CheckCircle2 size={14} className="text-ok shrink-0" aria-hidden="true" />
    case 'failed':
      return <XCircle size={14} className="text-danger shrink-0" aria-hidden="true" />
    default:
      return <Ban size={14} className="text-muted shrink-0" aria-hidden="true" />
  }
}

/** A plan preview has no step chats to open. */
const noSelect = () => {}

/** One step permission request: what is asked, then one button per offered option. */
export function KasApprovalCard({
  approval,
  pending,
  onAnswer,
}: {
  approval: KasApproval
  pending: boolean
  onAnswer: (optionId: string) => void
}) {
  const steps = planStepLines(approval.plan)
  // A prompt-created workflow's plan, structured: drawn as the step tree it
  // will run, above KAS's own text.
  const planNodes = approval.isPlan ? planTreeNodes(approval.plan) : []
  const planText = approval.planText ?? (typeof approval.plan?.text === 'string' ? approval.plan.text : null)
  const firstAllow = approval.options.find(o => !isRejectOption(o))
  // A plan handoff titles itself and shows KAS's plain-text plan verbatim; the
  // offered buttons stay as KAS sent them (a plan is never auto-approved).
  const title = approval.isPlan ? i18nT('apps.workflows.kas.approve_plan_title') : approval.title
  return (
    <section
      className="rounded-md border border-warn/50 bg-warn-subtle px-3 py-2 flex flex-col gap-2"
      data-testid="kas-approval"
      data-plan={approval.isPlan ? 'true' : undefined}
      aria-label={title}
    >
      <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong">
        <ShieldQuestion className="lucide-inline text-warn" />
        <span className="min-w-0 break-words">{title}</span>
      </div>
      {approval.command ? (
        <pre className="m-0 rounded bg-bg border border-border px-2 py-1.5 text-[12px] font-mono whitespace-pre-wrap break-all max-h-48 overflow-auto">
          <code>{approval.command}</code>
        </pre>
      ) : null}
      {planNodes.length > 0 ? (
        <div className="rounded bg-bg border border-border px-1.5 py-1 max-h-72 overflow-auto" data-testid="kas-plan-tree">
          <KasStepsTree nodes={planNodes} selectedPath={null} onSelect={noSelect} label={i18nT('apps.workflows.kas.plan_steps')} />
        </div>
      ) : null}
      {approval.isPlan && planText ? (
        <div>
          <p className="m-0 mb-1 text-[12px] text-muted">
            {planNodes.length > 0 ? i18nT('apps.workflows.kas.plan_text') : i18nT('apps.workflows.kas.plan_steps')}
          </p>
          <pre
            className="m-0 rounded bg-bg border border-border px-2 py-1.5 text-[12px] font-mono whitespace-pre text-text max-h-64 overflow-auto"
            data-testid="kas-plan-text"
          >
            <code>{planText}</code>
          </pre>
        </div>
      ) : planNodes.length === 0 && steps.length > 0 ? (
        <div>
          <p className="m-0 mb-1 text-[12px] text-muted">{i18nT('apps.workflows.kas.plan_steps')}</p>
          <ol className="m-0 p-0 list-none flex flex-col gap-0.5" data-testid="kas-plan-steps">
            {steps.map(step => (
              <li key={step.key} className="text-[12px] text-text" style={{ marginLeft: step.depth * 14 }}>
                {step.label}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      <div className="flex flex-wrap gap-1.5">
        {approval.options.map(option => (
          <Btn
            key={option.optionId}
            primary={option === firstAllow}
            danger={isRejectOption(option)}
            disabled={pending}
            onClick={() => onAnswer(option.optionId)}
            data-option-kind={option.kind}
          >
            {option.name}
          </Btn>
        ))}
      </div>
    </section>
  )
}
