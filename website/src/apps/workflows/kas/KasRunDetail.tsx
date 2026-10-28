/**
 * A KAS run's detail inside the chat side panel, shaped like the Kiro IDE's
 * run view: a header (back, name, "Created by session …", done/total steps,
 * total elapsed, controls), the blocking line and any approvals, then the
 * STEPS tree as a compact full-width table ABOVE the selected step's own chat.
 */
import { AlertTriangle, ArrowLeft } from 'lucide-react'

import type { KasApproval, KasQuestion, KasRunRow } from '../../../api/client'
import { IconButton } from '../../../components/ui'
import { useAppDispatch, useAppSelector } from '../../../store'
import { requestSlotReveal, setActiveSlot } from '../../../store/chatSlice'
import { fmtElapsed } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import {
  defaultStep,
  effectiveSteps,
  findNode,
  findNodeByPath,
  isTerminalRun,
  liveStatusLine,
  nodeCan,
  pauseLine,
  planUpdateIsWarning,
  planUpdateLine,
  questionsFor,
  runElapsedMs,
  runStepTotal,
  runTitle,
  showNeedsYouPill,
  stepAwaitsReply,
} from './kasModel'
import {
  KasLoopExhaustedControls,
  KasPauseReasonLine,
  KasRunRecoveryButtons,
  KasStepDecisionButtons,
} from './KasParityControls'
import { KasQuestionCard } from './KasQuestionCard'
import { KasRunIconControls, KasRunStatusIcon, useNow } from './KasRunBits'
import { KasApprovalCard } from './KasRunView'
import { KasStepChat } from './KasStepChat'
import { KasStepsTree } from './KasStepsTree'
import { useKasRunActions } from './useKasWorkflows'

/** Re-exported for the panel list, which imports it from here. */
export { KasStepsTree }

// ── Run detail ──

export function KasRunDetail({
  row,
  approvals,
  questions = [],
  slotKey,
  selectedNodeId,
  selectedPath,
  onSelectStep,
  onBack,
}: {
  row: KasRunRow
  approvals: readonly KasApproval[]
  /** Open question-tool asks (all runs; filtered to this one here). */
  questions?: readonly KasQuestion[]
  slotKey: string
  /** The step chosen by the reader, by node id (kept for older selections). */
  selectedNodeId: string | undefined
  /** The step instance chosen by the reader, by runtime path; wins over the id. */
  selectedPath?: string | undefined
  onSelectStep: (nodeId: string, path: string) => void
  onBack: () => void
}) {
  const dispatch = useAppDispatch()
  const { answer } = useKasRunActions()
  const parentSlot = row.slotKey || slotKey
  const chatTitle = useAppSelector(s => {
    const slot = s.dashboard.slots.find(x => x.key === parentSlot)
    return slot?.title && slot.title !== slot.key ? slot.title : null
  })
  const now = useNow(!isTerminalRun(row))

  const title = runTitle(row)
  const live = liveStatusLine(row)
  const total = runStepTotal(row)
  const done = effectiveSteps(row).done ?? 0
  const elapsedMs = runElapsedMs(row, now)
  // The selected instance, keyed by runtime path: the reader's own choice (by
  // path, else by node id resolved to its path), else the default step.
  const chosenPath =
    (selectedPath && findNodeByPath(row.nodes, selectedPath) ? selectedPath : null) ??
    (selectedNodeId ? findNode(row.nodes, selectedNodeId)?.path ?? null : null)
  const stepPath = chosenPath ?? defaultStep(row)?.path ?? null
  const step = stepPath ? findNodeByPath(row.nodes, stepPath) : null
  const stepId = step?.id ?? null
  const awaitsReply = stepAwaitsReply(row, step)

  const openParentChat = () => {
    if (!parentSlot) return
    dispatch(setActiveSlot(parentSlot))
    dispatch(requestSlotReveal(parentSlot))
  }

  return (
    <div className="flex flex-col gap-2 min-w-0" data-testid="kas-run-detail" data-workflow-id={row.workflowId}>
      <header className="flex items-center gap-1.5 min-w-0">
        <IconButton aria-label={i18nT('apps.workflows.kas.back_to_runs')} onClick={onBack} data-testid="kas-detail-back">
          <ArrowLeft size={14} aria-hidden="true" />
        </IconButton>
        <KasRunStatusIcon display={row.controls.display} attention={row.controls.needsAttention || !!row.blocking} />
        <h3 className="m-0 text-[13px] font-mono font-semibold text-text-strong truncate min-w-0 shrink">{title}</h3>
        {showNeedsYouPill(row) ? (
          <span className="text-[11px] px-1.5 py-0 rounded bg-warn-subtle text-warn shrink-0" data-testid="kas-needs-you">
            {i18nT('apps.workflows.kas.state_needs_you')}
          </span>
        ) : null}
        <span className="ml-auto flex items-center gap-2 shrink-0">
          {chatTitle ? (
            <span className="text-[11.5px] text-muted truncate max-w-[14rem] hidden sm:inline" data-testid="kas-created-by">
              {i18nT('apps.workflows.kas.created_by_session_prefix')}{' '}
              {parentSlot ? (
                <button
                  type="button"
                  className="bg-transparent border-none p-0 text-accent cursor-pointer hover:underline font-medium"
                  onClick={openParentChat}
                  data-testid="kas-created-by-link"
                >
                  {chatTitle}
                </button>
              ) : (
                <span className="text-text font-medium">{chatTitle}</span>
              )}
            </span>
          ) : null}
          {total != null ? (
            <span className="text-[11.5px] text-muted tabular-nums" data-testid="kas-run-progress">
              {i18nT('apps.workflows.kas.dock_steps', { done, total })}
            </span>
          ) : null}
          {elapsedMs != null ? (
            <span className="text-[11.5px] text-muted tabular-nums" data-testid="kas-run-elapsed">
              {fmtElapsed(elapsedMs, { wholeSeconds: true })}
            </span>
          ) : null}
          <span className="inline-flex items-center">
            <KasRunRecoveryButtons row={row} compact />
            <KasRunIconControls row={row} withDelete />
          </span>
        </span>
      </header>

      {live?.kind === 'blocking' ? (
        <p className="m-0 text-[12px] text-warn flex items-center gap-1.5" data-testid="kas-detail-blocking">
          <AlertTriangle size={12} className="shrink-0" aria-hidden="true" />
          <span className="min-w-0">{live.text}</span>
        </p>
      ) : pauseLine(row) ? (
        <KasPauseReasonLine row={row} />
      ) : live?.kind === 'reason' ? (
        <p className="m-0 text-[12px] text-muted">{live.text}</p>
      ) : null}

      {planUpdateLine(row) ? (
        <p
          className={`m-0 text-[12px] ${planUpdateIsWarning(row) ? 'text-warn' : 'text-muted'}`}
          data-testid="kas-detail-plan-update"
        >
          {planUpdateLine(row)}
        </p>
      ) : null}

      <KasLoopExhaustedControls row={row} />

      {questionsFor(questions, row.workflowId).map(q => (
        <KasQuestionCard key={q.key} question={q} stepLabel={findNode(row.nodes, q.nodeId)?.label ?? null} />
      ))}

      {approvals.map(approval => (
        <KasApprovalCard
          key={approval.key}
          approval={approval}
          pending={answer.isPending && answer.variables?.key === approval.key}
          onAnswer={optionId => answer.mutate({ key: approval.key, optionId })}
        />
      ))}

      {/* Tree table on top, full width; the selected step's chat below it. */}
      <section className="flex flex-col gap-1 min-w-0" aria-label={i18nT('apps.workflows.kas.steps')}>
        <KasStepsTree nodes={row.nodes} selectedPath={stepPath} onSelect={onSelectStep} row={row} />
      </section>
      <div className="min-w-0 border-t border-border pt-3">
        <KasStepChat
          key={stepPath ?? ''}
          workflowId={row.workflowId}
          nodeId={stepId}
          path={stepPath}
          stepRunning={step?.status === 'running'}
          canReply={awaitsReply}
          chatTitle={chatTitle}
          agent={step?.agent ?? null}
          stepLabel={step?.label ?? null}
          model={step?.model ?? null}
          effort={step?.effort ?? null}
          stepActions={
            step && (nodeCan(step, 'complete') || nodeCan(step, 'fail')) ? <KasStepDecisionButtons row={row} node={step} /> : null
          }
        />
      </div>
    </div>
  )
}
