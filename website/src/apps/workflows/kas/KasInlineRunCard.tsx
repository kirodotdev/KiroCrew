/**
 * The inline card a KAS `run_workflow` tool call draws in its chat's transcript
 * (agent.kas_workflows), shaped like the Kiro IDE's in-chat run card:
 *
 *   Q3 Revenue Analysis                      (● Running)  [⏸] [■]
 *   4-step workflow · Data → Analysis → Report → Export
 *   ◌ Analysis  wf-coder
 *   Step 2/5 · 1m 4s
 *
 * Clicking the card (or Enter/Space on it) opens the side panel's Workflows
 * tab on this run. Step approvals stay visible under the card, since a
 * chat-started run has no other surface in the chat to answer them.
 *
 * Identified from the tool's persisted OUTPUT, not its title: KAS answers
 * `run_workflow` with `{"workflowId": "wf_…", "status": …, "message": …}`, which
 * also works for historical messages. Off a KAS backend, or with the KAS
 * workflows preview off, it draws `fallback` (the generic tool line) instead.
 */
import type { ReactNode } from 'react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, ChevronRight, Workflow } from 'lucide-react'

import type { KasApproval, KasQuestion, KasRunRow } from '../../../api/client'
import type { ChatMessage } from '../../../types'
import { Badge, Btn } from '../../../components/ui'
import { useAppDispatch } from '../../../store'
import { openActivityToTab } from '../../../store/chatSlice'
import { useSlotId } from '../../../providers/SlotContext'
import { fmtElapsed } from '../../../i18n/format'
import { fmtDateTime } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import {
  approvalsFor,
  findNode,
  isTerminalRun,
  liveStatusLine,
  liveStepProgress,
  pauseLine,
  planSummary,
  planUpdateIsWarning,
  planUpdateLine,
  questionsFor,
  runBadge,
  runElapsedMs,
  runTitle,
  servedByMcp,
  showNeedsYouPill,
} from './kasModel'
import { AgentTag, KasRunIconControls, KasRunStatusIcon, useNow } from './KasRunBits'
import { KasApprovalCard } from './KasRunView'
import { KasLoopExhaustedControls, KasPauseReasonLine, KasRunRecoveryButtons } from './KasParityControls'
import { KasQuestionCard } from './KasQuestionCard'
import { KasStepsTree } from './KasStepsTree'
import { setKasSelection } from './kasSelection'
import { useKasChatRuns, useKasRunActions, useKasState, useKasWorkflowsEnabled, useKasWorkflowsSetting } from './useKasWorkflows'

const KAS_WORKFLOW_ID_RE = /^wf_[0-9a-f]{8,64}$/

/** The run id a KAS `run_workflow` tool message started, or null for any other message. */
export function extractKasWorkflowId(message: ChatMessage): string | null {
  if (message.role !== 'tool' || !message.content?.startsWith('🔧')) return null
  // An MCP-served call (trusted `meta.mcp_server`) is never KAS's own launch,
  // whatever its output looks like.
  if (servedByMcp(message)) return null
  const output = message.meta?.output
  if (typeof output !== 'string' || !output.trimStart().startsWith('{')) return null
  try {
    const body = JSON.parse(output) as { workflowId?: unknown; status?: unknown }
    if (typeof body.workflowId !== 'string' || typeof body.status !== 'string') return null
    return KAS_WORKFLOW_ID_RE.test(body.workflowId) ? body.workflowId : null
  } catch {
    return null
  }
}

/** True for a KAS run_workflow launch, which renders as its card and is never folded away. */
export function isKasRunWorkflowTool(message: ChatMessage): boolean {
  return extractKasWorkflowId(message) !== null
}

/** The `{n}-step workflow · A → B → C` meta line, or null with no plan. */
export function planMetaLine(row: Pick<KasRunRow, 'nodes'>): string | null {
  const plan = planSummary(row)
  if (!plan) return null
  const chain = plan.labels.join(' → ') + (plan.more ? ' → …' : '')
  return `${i18nT('apps.workflows.kas.n_step_workflow', { count: plan.count })} · ${chain}`
}

/** The `{started} · Step n/m · {elapsed}` footer (each part omitted when unknown). */
export function runFooterLine(row: KasRunRow, now: number): string | null {
  const parts: string[] = []
  // The IDE shows when the run started; Crew only ever showed elapsed (gap 24).
  if (row.startedAt) parts.push(i18nT('apps.workflows.kas.started_at', { when: fmtDateTime(row.startedAt) }))
  const progress = liveStepProgress(row)
  if (progress) parts.push(i18nT('apps.workflows.kas.step_progress', { done: progress.done, total: progress.total }))
  const elapsed = runElapsedMs(row, now)
  if (elapsed != null) parts.push(fmtElapsed(elapsed, { wholeSeconds: true }))
  return parts.length > 0 ? parts.join(' · ') : null
}

export function KasRunCardCompact({
  row,
  approvals,
  questions = [],
  readOnly = false,
}: {
  row: KasRunRow
  approvals: readonly KasApproval[]
  /** Open question-tool asks; this run's are answered under the card. */
  questions?: readonly KasQuestion[]
  /** Workflows off, run still held: view + Stop only, with a note and a settings link. */
  readOnly?: boolean
}) {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const slotId = useSlotId()
  const { answer } = useKasRunActions()
  const now = useNow(!isTerminalRun(row))
  const title = runTitle(row)
  const badge = runBadge(row)
  const meta = planMetaLine(row)
  const live = liveStatusLine(row)
  const footer = runFooterLine(row, now)
  const asks = questionsFor(questions, row.workflowId)
  // The structured pause line is drawn under the card (it may carry KAS's own
  // detail), so the header's live line does not repeat it.
  const parked = live?.kind === 'reason' && pauseLine(row) ? null : live
  const planUpdate = planUpdateLine(row)
  // Gap 14: the step tree rides inside the card — open while the run is live,
  // collapsed once it ends; the reader's own toggle (non-null) wins over that.
  const [treeOpen, setTreeOpen] = useState<boolean | null>(null)
  const showTree = !readOnly && row.nodes.length > 0
  const treeExpanded = treeOpen ?? !isTerminalRun(row)

  const open = () => {
    setKasSelection(slotId ?? row.slotKey, { workflowId: row.workflowId })
    dispatch(openActivityToTab('workflows'))
  }
  const openStep = (nodeId: string, path: string) => {
    setKasSelection(slotId ?? row.slotKey, { workflowId: row.workflowId, nodeId, path })
    dispatch(openActivityToTab('workflows'))
  }

  return (
    <article
      className="max-w-[560px] rounded-lg border border-border bg-bg-elevated text-text transition-colors hover:border-accent/50"
      data-testid="kas-inline-card"
      data-workflow-id={row.workflowId}
      aria-label={title}
    >
      <div className="flex items-start gap-2 px-3 py-2">
        <button
          type="button"
          className="flex-1 min-w-0 text-left bg-transparent border-none p-0 cursor-pointer text-text flex flex-col gap-0.5 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
          onClick={open}
          aria-label={i18nT('apps.workflows.kas.open_run_in_panel', { name: title })}
          data-testid="kas-inline-open"
        >
          <span className="flex items-center gap-2 min-w-0">
            <span className="font-semibold text-[13px] text-text-strong truncate min-w-0">{title}</span>
            <Badge variant={badge.variant} className="text-[11px] px-1.5 py-0" data-testid="kas-run-state">
              {badge.label}
            </Badge>
            {showNeedsYouPill(row) ? (
              <Badge variant="warn" className="text-[11px] px-1.5 py-0" data-testid="kas-needs-you">
                {i18nT('apps.workflows.kas.state_needs_you')}
              </Badge>
            ) : null}
          </span>
          {meta ? (
            <span className="text-[12px] text-muted truncate" data-testid="kas-inline-meta">
              {meta}
            </span>
          ) : null}
          {parked ? (
            <span
              className={`flex items-center gap-1.5 text-[12px] min-w-0 ${parked.kind === 'blocking' ? 'text-warn' : 'text-text'}`}
              data-testid="kas-inline-status"
            >
              <KasRunStatusIcon display={row.controls.display} attention={parked.kind === 'blocking'} size={12} />
              <span className="truncate min-w-0">{parked.text}</span>
              {parked.kind === 'active' && parked.agent ? <AgentTag agent={parked.agent} /> : null}
            </span>
          ) : null}
          {footer ? (
            <span className="text-[11px] text-muted tabular-nums" data-testid="kas-inline-footer">
              {footer}
            </span>
          ) : null}
        </button>
        {!readOnly ? <KasRunRecoveryButtons row={row} compact /> : null}
        <KasRunIconControls row={row} stopOnly={readOnly} />
      </div>
      {readOnly ? (
        <div className="px-3 pb-2 text-[12px] text-muted" data-testid="kas-inline-off-note">
          {i18nT('apps.workflows.kas.workflows_off_note')}{' '}
          <button
            type="button"
            className="text-accent bg-transparent border-none p-0 cursor-pointer hover:underline"
            // The Feature Previews section carries data-setting-key
            // "feature-previews-section" (FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR); ringing
            // it lands the reader on the whole card rather than one row.
            onClick={() => navigate('/settings/developer?highlight=key:feature-previews-section')}
            data-testid="kas-inline-off-settings"
          >
            {i18nT('apps.workflows.kas.workflows_off_settings')}
          </button>
        </div>
      ) : null}
      {pauseLine(row) && live?.kind !== 'blocking' ? (
        <div className="px-3 pb-2" data-testid="kas-inline-pause">
          <KasPauseReasonLine row={row} />
        </div>
      ) : null}
      {planUpdate ? (
        <div
          className={`px-3 pb-2 text-[12px] ${planUpdateIsWarning(row) ? 'text-warn' : 'text-muted'}`}
          data-testid="kas-inline-plan-update"
        >
          {planUpdate}
        </div>
      ) : null}
      {showTree ? (
        <div className="px-3 pb-2" data-testid="kas-inline-tree">
          <button
            type="button"
            className="inline-flex items-center gap-1 bg-transparent border-none p-0 text-[11.5px] text-muted cursor-pointer hover:text-text-strong focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            onClick={() => setTreeOpen(!treeExpanded)}
            aria-expanded={treeExpanded}
            data-testid="kas-inline-tree-toggle"
          >
            {treeExpanded ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
            {i18nT('apps.workflows.kas.steps')}
          </button>
          {treeExpanded ? (
            <div className="mt-1">
              <KasStepsTree nodes={row.nodes} selectedPath={null} onSelect={openStep} row={row} />
            </div>
          ) : null}
        </div>
      ) : null}
      {!readOnly ? (
        <div className="empty:hidden px-3 pb-3 flex flex-col gap-2">
          <KasLoopExhaustedControls row={row} />
          {asks.map(q => (
            <KasQuestionCard key={q.key} question={q} stepLabel={findNode(row.nodes, q.nodeId)?.label ?? null} />
          ))}
        </div>
      ) : null}
      {!readOnly && approvals.length > 0 ? (
        <div className="flex flex-col gap-2 px-3 pb-3">
          {approvals.map(approval => (
            <KasApprovalCard
              key={approval.key}
              approval={approval}
              pending={answer.isPending && answer.variables?.key === approval.key}
              onAnswer={optionId => answer.mutate({ key: approval.key, optionId })}
            />
          ))}
        </div>
      ) : null}
    </article>
  )
}

function Inner({ workflowId, readOnly, fallback }: { workflowId: string; readOnly: boolean; fallback: ReactNode }) {
  const state = useKasState(true)
  const dispatch = useAppDispatch()
  const slotId = useSlotId()
  const live = state.data?.runs.find(r => r.workflowId === workflowId)
  // Not in the live poll (a run from before a gateway restart): read this
  // chat's runs back from the ledger, once.
  const ledger = useKasChatRuns(slotId, state.isSuccess && !live)
  const row = live ?? ledger.data?.runs.find(r => r.workflowId === workflowId)
  if (row) {
    return (
      <KasRunCardCompact
        row={row}
        approvals={approvalsFor(state.data?.approvals ?? [], workflowId)}
        questions={state.data?.questions ?? []}
        readOnly={readOnly}
      />
    )
  }
  // Read-only mode (workflows off): the /state read is refused once the router
  // holds no run, so a run no longer in flight shows nothing — exactly
  // origin/main, the generic tool line.
  if (readOnly) return <>{fallback}</>
  // Enabled but not in the live list yet: still loading, or a run from before a
  // restart that no surface has listed. The panel reads history from disk.
  return (
    <div
      className="max-w-[560px] border border-border rounded-lg bg-bg-elevated text-text px-3 py-2 flex items-center gap-2 text-[13px]"
      data-testid="kas-inline-run-placeholder"
    >
      <Workflow size={14} className="text-muted shrink-0" aria-hidden="true" />
      <span className="truncate min-w-0">{i18nT('apps.workflows.kas.starting_workflow')}</span>
      <Btn className="ml-auto" onClick={() => dispatch(openActivityToTab('workflows'))}>
        {i18nT('apps.workflows.kas.open_workflows_panel')}
      </Btn>
    </div>
  )
}

export function KasInlineRunCard({ workflowId, fallback }: { workflowId: string; fallback: ReactNode }) {
  const { enabled, loading } = useKasWorkflowsEnabled()
  const { kasBackend } = useKasWorkflowsSetting()
  // While the engine probe is in flight show the fallback rather than a blank;
  // on a Crew backend that is the final answer, so it must not flash away.
  if (loading) return <>{fallback}</>
  // On a Kiro Agent backend with the switch off, a run the router still holds is
  // shown read-only (gap 5): view + Stop, no launch/resume/retry/grant/answers.
  // Anywhere else, OFF is origin/main — the generic tool line.
  const readOnly = !enabled && kasBackend
  if (!enabled && !readOnly) return <>{fallback}</>
  return (
    <div className="my-1">
      <Inner workflowId={workflowId} readOnly={readOnly} fallback={fallback} />
    </div>
  )
}
