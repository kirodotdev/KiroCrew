/**
 * WorkflowRunGraph — the planned flow chart of a dynamic-workflow run, lit up as it
 * runs. The graph MODE of the Workflows run panel; <WorkflowRunTree> is the other.
 *
 * The tree answers "what has happened". This answers "what is this run going to do,
 * and where is it now" — the two are different questions, which is why both exist.
 *
 * Shape: phases are stages, drawn top to bottom in the order the script declares them,
 * because `ctx.phase` is sequential and a phase persists until the next one starts.
 * The agents inside a stage are siblings with no order between them, since a stage's
 * work may run concurrently — so there are no edges inside a stage, and exactly one
 * between consecutive stages.
 *
 * NOT A CONTROL SURFACE. Nothing here is clickable. Wiring a node to
 * `workflow_rerun_subtree` would make a drawing able to spend tokens and restart real
 * agents on a misclick; rerun stays on the run controls, where it names what it does.
 *
 * Every string the script authored (phase titles, agent labels) is LLM-derived and is
 * sanitized and length-bounded here, exactly as the tree does it.
 */
import { memo, useMemo } from 'react'
import {
  CheckCircle2,
  ChevronDown,
  Circle,
  HelpCircle,
  Loader2,
  Repeat,
  XCircle,
} from 'lucide-react'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import { fmtElapsed } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { buildGraph, type GraphNode, type RunPlan, type RunStatus } from './planModel'
import type { WfEvent } from './runModel'

export interface WorkflowRunGraphProps {
  events: WfEvent[]
  /** The run-detail snapshot's `plan`. Absent when none could be read. */
  plan?: RunPlan | null
  status?: RunStatus
}

const MAX_TITLE = 80
const MAX_LABEL = 100

/** Phase icon. Mirrors the tree's vocabulary so one run cannot read two ways. */
function PhaseIcon({ state }: { state: 'running' | 'ok' | 'failed' | 'planned' }) {
  if (state === 'running') return <Loader2 size={12} className="text-accent animate-spin shrink-0" />
  if (state === 'ok') return <CheckCircle2 size={12} className="text-ok shrink-0" />
  if (state === 'failed') return <XCircle size={12} className="text-danger shrink-0" />
  return <Circle size={12} className="text-muted shrink-0" />
}

function NodeIcon({ state, resolved }: { state: GraphNode['state']; resolved?: boolean }) {
  if (state === 'running') return <Loader2 size={11} className="text-accent animate-spin shrink-0" />
  if (state === 'ran_ok') return <CheckCircle2 size={11} className="text-ok shrink-0" />
  if (state === 'ran_failed') return <XCircle size={11} className="text-danger shrink-0" />
  if (state === 'unknown') {
    // A question mark beside real boxes reads as "still waiting". Once the region has
    // produced work the marker is answered, so it stops asking.
    return resolved ? (
      <Repeat size={11} className="text-muted shrink-0" />
    ) : (
      <HelpCircle size={11} className="text-warn shrink-0" />
    )
  }
  return <Circle size={11} className="text-muted shrink-0" />
}

/** Chip border. A node nothing has run yet is DASHED — the one visual difference that
 *  carries the whole planned/actual distinction, so it must not be subtle. */
function chipClass(node: GraphNode): string {
  const base = 'flex items-center gap-1.5 px-2 py-1 rounded border text-[11px] max-w-[220px]'
  if (node.state === 'ran_failed') return `${base} border-danger/50 bg-card`
  if (node.state === 'running') return `${base} border-accent bg-card`
  if (node.state === 'ran_ok') return `${base} border-ok/40 bg-card`
  if (node.state === 'unknown') {
    return node.resolvedCount === undefined
      ? `${base} border-dashed border-warn/60 bg-transparent`
      : `${base} border-dashed border-border bg-transparent`
  }
  return `${base} border-dashed border-border bg-transparent opacity-70`
}

/**
 * The hover text for one node.
 *
 * Every node gets one. The visible label is cut twice -- once to MAX_LABEL, again by CSS
 * `truncate` -- so without this a long name is simply unreadable, and the nodes a reader
 * most wants to read are the ones that ran. An `unknown` marker is the exception: its
 * label is a keyword, not a name, so its own sentence is the useful text.
 *
 * A failed node carries a second line pointing at the Tree view. It is a pointer, not a
 * control: the graph is deliberately inert, and a red mark with no route to the detail
 * invites a click that does nothing.
 */
export function nodeTitle(node: GraphNode): string {
  if (node.state === 'unknown') {
    return node.resolvedCount === undefined
      ? i18nT('apps.workflows.workflowRunGraph.runtime_shape')
      : i18nT('apps.workflows.workflowRunGraph.runtime_shape_settled', {
          count: node.resolvedCount,
        })
  }
  const full = sanitizeLlmOutput(node.label)
  const lines = [full]
  if (node.state === 'planned') lines.push(i18nT('apps.workflows.workflowRunGraph.planned'))
  if (node.state === 'ran_failed') {
    lines.push(i18nT('apps.workflows.workflowRunGraph.failed_see_tree'))
  }
  return lines.filter(Boolean).join('\n')
}

const WorkflowRunGraph = memo(function WorkflowRunGraph({
  events,
  plan,
  status,
}: WorkflowRunGraphProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const graph = useMemo(() => buildGraph(plan, events, status), [plan, events, status])

  const runtimeShape = i18nT('apps.workflows.workflowRunGraph.runtime_shape')
  const notPlanned = i18nT('apps.workflows.workflowRunGraph.not_planned')

  if (graph.phases.length === 0) {
    return (
      <div className="text-[11px] text-muted italic" data-testid="workflow-graph-empty">
        {i18nT('apps.workflows.workflowRunGraph.nothing_to_draw')}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2" data-testid="workflow-run-graph">
      {!graph.hasPlan && (
        <div className="text-[11px] text-muted" data-testid="workflow-graph-no-plan">
          {i18nT('apps.workflows.workflowRunGraph.no_plan')}
        </div>
      )}
      {graph.truncated && (
        <div className="text-[11px] text-warn" data-testid="workflow-graph-truncated">
          {i18nT('apps.workflows.workflowRunGraph.partial_plan')}
        </div>
      )}

      <ol className="flex flex-col">
        {graph.phases.map((phase, idx) => {
          const title = sanitizeLlmOutput(
            phase.title || i18nT('apps.workflows.workflowsRuns.unknown'),
          ).slice(0, MAX_TITLE)
          return (
            <li key={`${phase.title}#${idx}`} className="flex flex-col">
              <div
                className={`border rounded ${
                  phase.state === 'planned' ? 'border-dashed border-border' : 'border-border'
                }`}
                data-testid="workflow-graph-phase"
                data-phase-state={phase.state}
                data-phase-certain={phase.certain ? 'yes' : 'no'}
                data-phase-predicted={phase.predicted ? 'yes' : 'no'}
              >
                <div className="flex items-center gap-2 px-3 py-1.5 text-[12px] font-medium border-b border-border bg-card">
                  <PhaseIcon state={phase.state} />
                  <span className="truncate flex-1">{title}</span>
                  {!phase.certain && (
                    <span
                      className="text-[10px] text-warn shrink-0"
                      title={i18nT('apps.workflows.workflowRunGraph.may_not_run_why')}
                    >
                      {i18nT('apps.workflows.workflowRunGraph.may_not_run')}
                    </span>
                  )}
                  {!phase.predicted && (
                    <span
                      className="text-[10px] text-muted shrink-0"
                      title={i18nT('apps.workflows.workflowRunGraph.not_planned_why')}
                    >
                      {notPlanned}
                    </span>
                  )}
                </div>
                {phase.nodes.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 px-3 py-2">
                    {phase.nodes.map(node => {
                      const label =
                        node.state === 'unknown'
                          ? node.label
                          : sanitizeLlmOutput(node.label).slice(0, MAX_LABEL)
                      return (
                        <div
                          key={node.id}
                          className={chipClass(node)}
                          data-testid="workflow-graph-node"
                          data-node-state={node.state}
                          data-node-predicted={node.predicted ? 'yes' : 'no'}
                          data-node-resolved={
                            node.resolvedCount === undefined ? undefined : String(node.resolvedCount)
                          }
                          title={nodeTitle(node)}
                        >
                          <NodeIcon
                            state={node.state}
                            resolved={node.resolvedCount !== undefined}
                          />
                          <span
                            className={
                              node.state === 'unknown'
                                ? node.resolvedCount === undefined
                                  ? 'font-mono text-warn truncate'
                                  : 'font-mono text-muted truncate'
                                : 'font-mono truncate'
                            }
                          >
                            {label}
                          </span>
                          {node.state === 'unknown' && (
                            <span className="text-[10px] text-muted shrink-0">
                              {node.resolvedCount === undefined
                                ? runtimeShape
                                : i18nT('apps.workflows.workflowRunGraph.runtime_shape_settled', {
                                    count: node.resolvedCount,
                                  })}
                            </span>
                          )}
                          {!node.predicted && node.state !== 'unknown' && (
                            <span
                              className="text-[10px] text-muted shrink-0"
                              title={i18nT('apps.workflows.workflowRunGraph.not_planned_why')}
                            >
                              {notPlanned}
                            </span>
                          )}
                          {node.elapsedMs !== undefined && (
                            <span className="text-[10px] text-muted tabular-nums shrink-0">
                              {fmtElapsed(node.elapsedMs)}
                            </span>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
              {idx < graph.phases.length - 1 && (
                <div
                  className="flex justify-center py-0.5 text-muted"
                  aria-hidden="true"
                  data-testid="workflow-graph-edge"
                >
                  <ChevronDown size={14} />
                </div>
              )}
            </li>
          )
        })}
      </ol>
      {graph.phases.some(p => p.nodes.some(n => n.state === 'ran_failed')) && (
        // A red mark needs a route to the failure detail, and the node's own `title`
        // is not one: hover text never reaches a touch or keyboard reader, so for them
        // the red node would be the dead end this view exists to avoid. The same
        // sentence therefore appears once, visibly, whenever anything failed.
        <div className="text-[11px] text-muted" data-testid="workflow-graph-failed-hint">
          {i18nT('apps.workflows.workflowRunGraph.failed_see_tree')}
        </div>
      )}
    </div>
  )
})

export default WorkflowRunGraph
