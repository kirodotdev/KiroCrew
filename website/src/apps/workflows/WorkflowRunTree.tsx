/**
 * WorkflowRunTree — hierarchical progress tree for a dynamic-workflow run.
 *
 * Presentational component shared by:
 *   - the chat WorkflowProgressBar (expanded view above the chat input), and
 *   - the ActivityViewer "Workflows" sidebar (per-run expansion).
 *
 * Takes a full run snapshot's events plus its terminal status / result and
 * renders:
 *   - one collapsible row per PHASE with a status icon (running spinner if any
 *     of its agents are still running, ✓ if all finished ok, ✗ if any failed),
 *     title, and agent count.
 *   - under each phase, the AGENTS with per-agent status (spinner / ✓ / ✗),
 *     their `label` (mono), and `last_tool` if present.
 *   - a narrator log strip (most recent `log` lines) and a budget readout.
 *
 * All LLM-derived strings are sanitized via sanitizeLlmOutput. Pure event-fold
 * logic lives in ./runModel and is unit-tested.
 */
import { memo, useMemo, useState } from 'react'
import { CheckCircle2, XCircle, Loader2, ChevronRight, Workflow as WorkflowIcon } from 'lucide-react'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { groupByPhase, latestBudget, type WfEvent } from './runModel'

import { i18nT } from '../../i18n/t'
export interface WorkflowRunTreeProps {
  events: WfEvent[]
  /** Terminal status of the run; drives the per-phase fallback when no agents
   *  have started yet. */
  status?: 'running' | 'finished' | 'failed' | 'cancelled'
  /** Final result blob (only meaningful when status === 'finished'). */
  result?: unknown
  /** Failure message (only meaningful when status === 'failed'). */
  error?: string | null
  /** Cap on rendered narrator log lines (most recent shown). Default 6. */
  maxLogs?: number
}

/**
 * Phase status. Phases are emitted in order, so a phase is COMPLETE once a later
 * phase has started — the run has moved on. Only the last (current) phase can be
 * "running". Without this, a past phase that spawned no agents (e.g. "Authoring"
 * or a "Plan" phase whose work was a single ctx.agent that already finished, or
 * pure ctx.log narration) would render as a perpetual spinner even though the run
 * is already several phases ahead.
 *
 *   - `isLast` false  → the run advanced past this phase: ok, unless one of its
 *                       agents failed (then failed).
 *   - `isLast` true   → current phase: failed if any agent failed; ok if it has
 *                       agents and all finished ok; running if the run is still
 *                       going (agents in flight, or none spawned yet); else ok.
 */
function phaseStatus(
  agents: { ok?: boolean }[],
  runStatus: WorkflowRunTreeProps['status'],
  isLast: boolean,
): 'running' | 'ok' | 'failed' {
  if (agents.some(a => a.ok === false)) return 'failed'
  if (!isLast) return 'ok' // a later phase started → this one is done
  // Last phase = the current one.
  if (runStatus && runStatus !== 'running') {
    // Run is terminal: reflect it (cancelled/failed → not ok unless agents ok).
    return runStatus === 'finished' ? 'ok' : runStatus === 'failed' ? 'failed' : 'ok'
  }
  if (agents.length > 0 && agents.every(a => a.ok === true)) return 'ok'
  return 'running'
}

const WorkflowRunTree = memo(function WorkflowRunTree({
  events,
  status,
  result,
  error,
  maxLogs = 6,
}: WorkflowRunTreeProps) {
  const phases = useMemo(() => groupByPhase(events), [events])
  const budget = useMemo(() => latestBudget(events), [events])
  const logLines = useMemo(
    () => events.filter(e => e.type === 'log').slice(-maxLogs),
    [events, maxLogs],
  )

  // Phases default to expanded. Track collapsed-state by title so the user can
  // hide noisy completed phases without losing the running one.
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const toggle = (title: string) =>
    setCollapsed(s => ({ ...s, [title]: !s[title] }))

  const showEmptyHint = phases.length === 0 && logLines.length === 0
  return (
    <div className="flex flex-col gap-2">
      {budget && (
        <div className="text-[11px] text-muted tabular-nums flex items-center gap-1.5">
          <WorkflowIcon size={11} />
          {i18nT('apps.workflows.workflowRunTree.budget')} {budget.spent}
          {budget.total != null ? ` / ${budget.total}` : ''}
        </div>
      )}

      {showEmptyHint && (
        <div className="text-[11px] text-muted italic">
          {i18nT('apps.workflows.workflowRunTree.waiting_for_events')}
        </div>
      )}

      {phases.map((phase, idx) => {
        const isCollapsed = !!collapsed[phase.title]
        const ps = phaseStatus(phase.agents, status, idx === phases.length - 1)
        const title = sanitizeLlmOutput(phase.title || '(no phase)').slice(0, 80)
        return (
          <div key={phase.title} className="border border-border rounded">
            <button
              type="button"
              onClick={() => toggle(phase.title)}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-[12px] font-medium border-b border-border bg-card hover:bg-bg-hover transition-colors"
              aria-expanded={!isCollapsed}
            >
              <ChevronRight
                size={12}
                className={`text-muted shrink-0 transition-transform ${isCollapsed ? '' : 'rotate-90'}`}
              />
              {ps === 'running' ? (
                <Loader2 size={12} className="text-accent animate-spin shrink-0" />
              ) : ps === 'ok' ? (
                <CheckCircle2 size={12} className="text-green-500 shrink-0" />
              ) : (
                <XCircle size={12} className="text-red-500 shrink-0" />
              )}
              <span className="truncate text-left flex-1">{title}</span>
              <span className="ml-auto text-[10px] text-muted tabular-nums shrink-0">
                {i18nT('apps.workflows.workflowRunTree.agent', { count: phase.agents.length })}
              </span>
            </button>
            {!isCollapsed && phase.agents.length > 0 && (
              <ul className="divide-y divide-border">
                {phase.agents.map(a => {
                  const label = sanitizeLlmOutput(a.label || a.agent_id).slice(0, 100)
                  const lastTool = a.last_tool ? sanitizeLlmOutput(a.last_tool).slice(0, 60) : ''
                  return (
                    <li
                      key={a.agent_id}
                      className="flex items-center gap-2 px-3 py-1.5 text-[12px]"
                    >
                      {a.ok === undefined ? (
                        <Loader2 size={12} className="text-accent animate-spin shrink-0" />
                      ) : a.ok ? (
                        <CheckCircle2 size={12} className="text-green-500 shrink-0" />
                      ) : (
                        <XCircle size={12} className="text-red-500 shrink-0" />
                      )}
                      <span className="font-mono truncate">{label}</span>
                      {lastTool && (
                        <span className="text-muted truncate">· {lastTool}</span>
                      )}
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )
      })}

      {/* narrator log strip — most recent maxLogs lines */}
      {logLines.length > 0 && (
        <div className="text-[11px] font-mono text-muted border border-border rounded p-2 max-h-32 overflow-auto">
          {logLines.map((e, i) => (
            <div key={i} className="truncate">
              · {sanitizeLlmOutput(String((e.data as any)?.message ?? '')).slice(0, 200)}
            </div>
          ))}
        </div>
      )}

      {/* terminal result / failure panel */}
      {status && status !== 'running' && (
        <div
          className={`text-[12px] rounded p-2 border ${
            status === 'finished'
              ? 'border-green-500/30'
              : status === 'cancelled'
                ? 'border-border'
                : 'border-red-500/30 text-red-500'
          }`}
        >
          <div className="font-medium mb-1 flex items-center gap-1.5">
            {status === 'finished' ? (
              <CheckCircle2 size={12} className="text-green-500" />
            ) : status === 'cancelled' ? (
              <XCircle size={12} />
            ) : (
              <XCircle size={12} />
            )}
            {status === 'finished'
              ? 'Result'
              : status === 'cancelled'
                ? 'Cancelled'
                : `Failed: ${sanitizeLlmOutput(error || 'unknown error').slice(0, 200)}`}
          </div>
          {status === 'finished' && result !== undefined && (
            <pre className="font-mono text-[11px] whitespace-pre-wrap break-all max-h-40 overflow-auto">
              {sanitizeLlmOutput(JSON.stringify(result, null, 2)).slice(0, 2000)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
})

export default WorkflowRunTree
