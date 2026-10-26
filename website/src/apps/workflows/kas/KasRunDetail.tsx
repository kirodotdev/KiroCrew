/**
 * A KAS run's detail inside the chat side panel, shaped like the Kiro IDE's
 * run tab: a header (back, name, state, Step n/m, controls), the blocking line
 * and any approvals, then a STEPS tree beside (panel ≥ 640px) or above (narrow
 * panel) the selected step's own chat.
 */
import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, ArrowLeft } from 'lucide-react'

import type { KasApproval, KasQuestion, KasRunRow } from '../../../api/client'
import { Badge, IconButton } from '../../../components/ui'
import { useAppSelector } from '../../../store'
import { i18nT } from '../../../i18n/t'
import {
  defaultStep,
  findNode,
  findNodeByPath,
  liveStatusLine,
  liveStepProgress,
  nodeCan,
  pauseLine,
  planUpdateIsWarning,
  planUpdateLine,
  questionsFor,
  runBadge,
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
import { KasRunIconControls, KasRunStatusIcon } from './KasRunBits'
import { KasApprovalCard } from './KasRunView'
import { KasStepChat } from './KasStepChat'
import { KasStepsTree } from './KasStepsTree'
import { useKasRunActions } from './useKasWorkflows'

/** Panel width from which the tree and the step chat sit side by side. */
export const SIDE_BY_SIDE_MIN_PX = 640

/** True while `ref`'s element is at least `minPx` wide. */
function useWideEnough(ref: React.RefObject<HTMLElement | null>, minPx: number): boolean {
  const [wide, setWide] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = () => setWide(el.getBoundingClientRect().width >= minPx)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [ref, minPx])
  return wide
}

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
  const rootRef = useRef<HTMLDivElement | null>(null)
  const wide = useWideEnough(rootRef, SIDE_BY_SIDE_MIN_PX)
  const { answer } = useKasRunActions()
  const chatTitle = useAppSelector(s => {
    const slot = s.dashboard.slots.find(x => x.key === (row.slotKey || slotKey))
    return slot?.title && slot.title !== slot.key ? slot.title : null
  })

  const title = runTitle(row)
  const badge = runBadge(row)
  const progress = liveStepProgress(row)
  const live = liveStatusLine(row)
  // The selected instance, keyed by runtime path: the reader's own choice (by
  // path, else by node id resolved to its path), else the default step.
  const chosenPath =
    (selectedPath && findNodeByPath(row.nodes, selectedPath) ? selectedPath : null) ??
    (selectedNodeId ? findNode(row.nodes, selectedNodeId)?.path ?? null : null)
  const stepPath = chosenPath ?? defaultStep(row)?.path ?? null
  const step = stepPath ? findNodeByPath(row.nodes, stepPath) : null
  const stepId = step?.id ?? null
  const awaitsReply = stepAwaitsReply(row, step)

  return (
    <div ref={rootRef} className="flex flex-col gap-2 min-w-0" data-testid="kas-run-detail" data-workflow-id={row.workflowId} data-layout={wide ? 'wide' : 'narrow'}>
      <header className="flex items-center gap-1.5 min-w-0">
        <IconButton aria-label={i18nT('apps.workflows.kas.back_to_runs')} onClick={onBack} data-testid="kas-detail-back">
          <ArrowLeft size={14} aria-hidden="true" />
        </IconButton>
        <KasRunStatusIcon display={row.controls.display} attention={row.controls.needsAttention || !!row.blocking} />
        <h3 className="m-0 text-[13px] font-semibold text-text-strong truncate min-w-0">{title}</h3>
        <Badge variant={badge.variant} className="text-[11px] px-1.5 py-0 shrink-0" data-testid="kas-run-state">
          {badge.label}
        </Badge>
        {showNeedsYouPill(row) ? (
          <Badge variant="warn" className="text-[11px] px-1.5 py-0 shrink-0" data-testid="kas-needs-you">
            {i18nT('apps.workflows.kas.state_needs_you')}
          </Badge>
        ) : null}
        {progress ? (
          <span className="text-[11.5px] text-muted tabular-nums shrink-0" data-testid="kas-run-progress">
            {i18nT('apps.workflows.kas.step_progress', { done: progress.done, total: progress.total })}
          </span>
        ) : null}
        <span className="ml-auto inline-flex items-center">
          <KasRunRecoveryButtons row={row} compact />
          <KasRunIconControls row={row} withDelete />
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

      <div className={wide ? 'grid grid-cols-[minmax(220px,38%)_1fr] gap-3 items-start' : 'flex flex-col gap-3'}>
        <section className="flex flex-col gap-1 min-w-0" aria-label={i18nT('apps.workflows.kas.steps')}>
          <h4 className="m-0 text-[10.5px] font-semibold uppercase tracking-wide text-muted">{i18nT('apps.workflows.kas.steps')}</h4>
          <KasStepsTree nodes={row.nodes} selectedPath={stepPath} onSelect={onSelectStep} row={row} />
        </section>
        <div className={wide ? 'min-w-0 border-l border-border pl-3' : 'min-w-0 border-t border-border pt-3'}>
          <KasStepChat
            key={stepPath ?? ''}
            workflowId={row.workflowId}
            nodeId={stepId}
            path={stepPath}
            stepRunning={step?.status === 'running'}
            canReply={awaitsReply}
            chatTitle={chatTitle}
            agent={step?.agent ?? null}
            stepActions={
              step && (nodeCan(step, 'complete') || nodeCan(step, 'fail')) ? <KasStepDecisionButtons row={row} node={step} /> : null
            }
          />
        </div>
      </div>
    </div>
  )
}
