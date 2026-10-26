/**
 * The IDE-style STEPS tree of a KAS run (or of a proposed plan): groups with
 * their status, a repeat's realised iterations, included workflows labelled
 * with their recipe, and step rows that open the step's own chat. Given the
 * live run (`row`), rows also offer the per-step controls KAS allows (Stop,
 * Retry) and say which step waits for a reply.
 */
import { createContext, useContext, useState } from 'react'
import { ChevronDown, ChevronRight, Eye, Workflow } from 'lucide-react'

import type { KasNodeDescriptor, KasRunNode, KasRunRow } from '../../../api/client'
import { fmtElapsed } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { groupSummary, includeMemberLabel, nodeWaitLabel, retryWaitLine, stepAwaitsReply, stepDurationMs, watchOutcomeLabel } from './kasModel'
import { KasStepGlyph, stepStatusLabel } from './KasRunBits'
import { KasTreeStepControls } from './KasParityControls'

/** Indentation stops growing past this depth, so a deep plan stays readable. */
const MAX_INDENT_DEPTH = 4

// ── STEPS tree ──

/** A finished step's duration, e.g. `2m 10s`, or null while it runs or when KAS
 *  carries no timing for it. */
function stepDuration(node: KasRunNode): string | null {
  if (!node.endedAt) return null
  const ms = stepDurationMs(node, 0)
  return ms != null ? fmtElapsed(ms, { wholeSeconds: true }) : null
}

/** The IDE's step-row columns: the agent, then `model · effort`, then the
 *  step's duration — all muted. */
function StepMeta({ node }: { node: KasRunNode }) {
  const model = [node.model, node.effort].filter(Boolean).join(' · ')
  const duration = stepDuration(node)
  if (!node.agent && !model && !duration) return null
  return (
    <span className="ml-auto pl-2 flex items-center gap-3 shrink-0 text-[11.5px] text-muted" data-testid="kas-step-meta">
      {node.agent ? <span className="truncate max-w-[10rem]">{node.agent}</span> : null}
      {model ? <span className="truncate max-w-[14rem] tabular-nums">{model}</span> : null}
      {duration ? <span className="tabular-nums" data-testid="kas-step-duration">{duration}</span> : null}
    </span>
  )
}

/** The run a tree belongs to, so its rows can offer per-step controls
 *  (Stop, Retry) and say which step waits for a reply. Null for a plan preview. */
const TreeRunContext = createContext<KasRunRow | null>(null)

export function KasStepsTree({
  nodes,
  selectedPath,
  onSelect,
  row = null,
  label,
}: {
  nodes: readonly KasRunNode[]
  selectedPath: string | null
  onSelect: (nodeId: string, path: string) => void
  /** The live run: enables its per-step controls. Omit for a plan preview. */
  row?: KasRunRow | null
  /** The list's accessible name; "Steps" by default. */
  label?: string
}) {
  if (nodes.length === 0) {
    return <p className="m-0 text-[12px] text-muted">{i18nT('apps.workflows.kas.no_steps')}</p>
  }
  return (
    <TreeRunContext.Provider value={row}>
      <ul className="m-0 p-0 list-none flex flex-col gap-px" aria-label={label ?? i18nT('apps.workflows.kas.steps')} data-testid="kas-steps-tree">
        {nodes.map(node => (
          <TreeNode key={node.path} node={node} depth={0} selectedPath={selectedPath} onSelect={onSelect} />
        ))}
        {row && row.queuedSteps.length > 0 ? <QueuedSteps queued={row.queuedSteps} /> : null}
      </ul>
    </TreeRunContext.Provider>
  )
}

/** The pending replacement plan (gap 11): KAS has a queued `update_workflow`
 *  this run will apply. Drawn as muted "queued" rows after the live tree. */
function QueuedSteps({ queued }: { queued: readonly KasNodeDescriptor[] }) {
  return (
    <li className="mt-1 pt-1 border-t border-dashed border-border" data-testid="kas-queued-steps">
      <p className="m-0 px-1.5 text-[10.5px] font-semibold uppercase tracking-wide text-muted">
        {i18nT('apps.workflows.kas.queued_plan')}
      </p>
      <ul className="m-0 p-0 list-none flex flex-col gap-px">
        {queued.map((node, i) => (
          <li
            key={node.id || i}
            className="flex items-center gap-1.5 rounded px-1.5 py-1 text-[12px] text-muted italic"
            style={{ paddingLeft: 6 }}
            data-testid="kas-queued-step"
          >
            <KasStepGlyph status="pending" />
            <span className="truncate min-w-0">{node.label || node.agent || node.id}</span>
            <span className="ml-auto shrink-0 rounded px-1.5 py-px text-[10px] bg-bg-hover border border-border not-italic">
              {i18nT('apps.workflows.kas.queued_badge')}
            </span>
          </li>
        ))}
      </ul>
    </li>
  )
}

/** The children a tree node shows: a repeat with realised iterations renders
 *  those (latest expanded); every other group renders its definition children. */
function NodeChildren({
  node,
  depth,
  selectedPath,
  onSelect,
}: {
  node: KasRunNode
  depth: number
  selectedPath: string | null
  onSelect: (nodeId: string, path: string) => void
}) {
  if (node.type === 'repeat' && node.iterations && node.iterations.length > 0) {
    const last = node.iterations.length - 1
    return (
      <ul className="m-0 p-0 list-none flex flex-col gap-px">
        {node.iterations.map((iter, i) => (
          <IterationGroup
            key={iter.path}
            iteration={iter}
            depth={depth + 1}
            defaultOpen={i === last}
            selectedPath={selectedPath}
            onSelect={onSelect}
          />
        ))}
      </ul>
    )
  }
  if (node.children.length === 0) return null
  // An included workflow's members carry the include's id as a prefix
  // (`review/setup`); inside its labelled group they read without it.
  const includeId = node.include ? node.id : null
  return (
    <ul className="m-0 p-0 list-none flex flex-col gap-px">
      {node.children.map(child => (
        <TreeNode key={child.path} node={child} depth={depth + 1} selectedPath={selectedPath} onSelect={onSelect} includeId={includeId} />
      ))}
    </ul>
  )
}

/** One realised loop iteration: a collapsible group of the body AS IT RAN. */
function IterationGroup({
  iteration,
  depth,
  defaultOpen,
  selectedPath,
  onSelect,
}: {
  iteration: NonNullable<KasRunNode['iterations']>[number]
  depth: number
  defaultOpen: boolean
  selectedPath: string | null
  onSelect: (nodeId: string, path: string) => void
}) {
  const [open, setOpen] = useState(defaultOpen)
  const indent = Math.min(depth, MAX_INDENT_DEPTH) * 12
  const status = stepStatusLabel(iteration.status)
  const label = i18nT('apps.workflows.kas.iteration_label', { n: iteration.iteration + 1 })
  return (
    <li>
      <button
        type="button"
        className="w-full flex items-center gap-1.5 rounded px-1.5 py-1 bg-transparent border-none text-left text-[12px] text-text cursor-pointer hover:bg-bg-hover"
        style={{ paddingLeft: 6 + indent }}
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        data-testid="kas-iteration-group"
        data-iteration={iteration.iteration}
        data-status={iteration.status}
      >
        <KasStepGlyph status={iteration.status} />
        <span className="sr-only">{status}</span>
        <span className="truncate min-w-0 font-medium tabular-nums">{label}</span>
        {open ? (
          <ChevronDown size={12} className="ml-auto text-muted shrink-0" aria-hidden="true" />
        ) : (
          <ChevronRight size={12} className="ml-auto text-muted shrink-0" aria-hidden="true" />
        )}
      </button>
      {open && iteration.children.length > 0 ? (
        <ul className="m-0 p-0 list-none flex flex-col gap-px">
          {iteration.children.map(child => (
            <TreeNode key={child.path} node={child} depth={depth + 1} selectedPath={selectedPath} onSelect={onSelect} />
          ))}
        </ul>
      ) : null}
    </li>
  )
}

function TreeNode({
  node,
  depth,
  selectedPath,
  onSelect,
  includeId = null,
}: {
  node: KasRunNode
  depth: number
  selectedPath: string | null
  onSelect: (nodeId: string, path: string) => void
  /** The enclosing include group's id, whose `id/` prefix the label drops. */
  includeId?: string | null
}) {
  const row = useContext(TreeRunContext)
  const [open, setOpen] = useState(true)
  const isWatch = node.type === 'watch'
  const isGroup = node.type !== 'step' && !isWatch
  const indent = Math.min(depth, MAX_INDENT_DEPTH) * 12
  const status = stepStatusLabel(node.status)
  const label = includeMemberLabel(node.label, includeId)

  if (isGroup) {
    const iteration = node.loop
      ? i18nT('apps.workflows.kas.iteration', { n: node.loop.done, max: node.loop.max })
      : node.type === 'repeat' && node.maxIterations
        ? node.iteration != null
          ? i18nT('apps.workflows.kas.iteration', { n: node.iteration, max: node.maxIterations })
          : i18nT('apps.workflows.kas.max_iterations', { max: node.maxIterations })
        : null
    // An included workflow is labelled with the recipe it brought in; its file
    // path shows on hover.
    const groupLabel = node.include ? node.include.recipe || label : label
    // Collapsed, say what is inside so the group does not hide its state (gap 23).
    const summary = open ? null : groupSummary(node)
    return (
      <li>
        <button
          type="button"
          className="w-full flex items-center gap-1.5 rounded px-1.5 py-1 bg-transparent border-none text-left text-[12px] text-text cursor-pointer hover:bg-bg-hover"
          style={{ paddingLeft: 6 + indent }}
          onClick={() => setOpen(v => !v)}
          aria-expanded={open}
          title={node.include?.path || undefined}
          data-testid={node.include ? 'kas-include-group' : 'kas-step-group'}
          data-status={node.status}
        >
          <KasStepGlyph status={node.status} />
          <span className="sr-only">{status}</span>
          {node.include ? (
            <>
              <Workflow size={12} className="text-muted shrink-0" aria-hidden="true" />
              <span className="sr-only">{i18nT('apps.workflows.kas.included_workflow')}</span>
            </>
          ) : null}
          <span className="truncate min-w-0 font-medium">{groupLabel}</span>
          {iteration ? <span className="text-[11px] text-muted tabular-nums shrink-0">{iteration}</span> : null}
          {summary ? (
            <span className="text-[11px] text-muted truncate min-w-0" data-testid="kas-group-summary">
              {summary}
            </span>
          ) : null}
          {open ? (
            <ChevronDown size={12} className="ml-auto text-muted shrink-0" aria-hidden="true" />
          ) : (
            <ChevronRight size={12} className="ml-auto text-muted shrink-0" aria-hidden="true" />
          )}
        </button>
        {open ? <NodeChildren node={node} depth={depth} selectedPath={selectedPath} onSelect={onSelect} /> : null}
      </li>
    )
  }

  const selected = selectedPath === node.path
  const waiting = row ? stepAwaitsReply(row, node) : false
  // A step row's secondary line, in priority order (gap 1 / 23):
  //  - a transient-error backoff (step is still running),
  //  - "Needs approval" / "Waiting for you" from KAS's own wait flag,
  //  - a reply wait, a watch node's last-poll outcome, else the node's reason.
  const retry = retryWaitLine(node)
  const waitTag = nodeWaitLabel(node)
  const watchLine = isWatch ? watchOutcomeLabel(node) : null
  const secondary = retry
    ? { text: retry, warn: true, testid: 'kas-step-retry-wait' }
    : waitTag
      ? { text: waitTag, warn: true, testid: 'kas-step-wait' }
      : waiting
        ? { text: i18nT('apps.workflows.kas.waiting_reply'), warn: true, testid: 'kas-step-waiting' }
        : watchLine
          ? { text: watchLine, warn: false, testid: 'kas-watch-outcome' }
          : node.reason
            ? { text: node.reason, warn: false, testid: 'kas-step-reason' }
            : null
  const content = (
    <>
      {isWatch ? <Eye size={13} className="text-muted shrink-0" aria-hidden="true" data-glyph="watch" /> : <KasStepGlyph status={node.status} />}
      <span className="sr-only">{waiting ? i18nT('apps.workflows.kas.waiting_reply') : status}</span>
      <span className={`truncate min-w-0 ${node.status === 'aborted' || node.status === 'skipped' ? 'text-muted' : ''}`}>{label}</span>
      {node.replacementPending ? (
        <span className="shrink-0 rounded px-1.5 py-px text-[10px] bg-bg-hover border border-border text-muted" data-testid="kas-will-replace">
          {i18nT('apps.workflows.kas.will_be_replaced')}
        </span>
      ) : null}
      {secondary ? (
        <span className={`text-[11px] truncate min-w-0 ${secondary.warn ? 'text-warn' : 'text-muted'}`} data-testid={secondary.testid}>
          {secondary.text}
        </span>
      ) : null}
      <StepMeta node={node} />
    </>
  )
  const rowClass = `flex-1 min-w-0 flex items-center gap-1.5 rounded px-1.5 py-1 text-left text-[12px] border ${
    selected ? 'bg-accent-subtle border-accent/40 text-text-strong' : 'border-transparent text-text'
  }`
  return (
    <li className="flex items-center gap-0.5 min-w-0">
      {node.sessionId ? (
        <button
          type="button"
          className={`${rowClass} cursor-pointer bg-transparent hover:bg-bg-hover ${selected ? 'bg-accent-subtle' : ''}`}
          style={{ paddingLeft: 6 + indent }}
          onClick={() => onSelect(node.id, node.path)}
          aria-current={selected ? 'true' : undefined}
          data-testid="kas-tree-step"
          data-status={node.status}
        >
          {content}
        </button>
      ) : (
        <div
          className={rowClass}
          style={{ paddingLeft: 6 + indent }}
          aria-current={selected ? 'true' : undefined}
          data-testid="kas-tree-step"
          data-status={node.status}
        >
          {content}
        </div>
      )}
      {row ? <KasTreeStepControls row={row} node={node} label={label} /> : null}
    </li>
  )
}
