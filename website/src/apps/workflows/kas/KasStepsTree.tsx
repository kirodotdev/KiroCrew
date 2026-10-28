/**
 * The IDE-style STEPS tree of a KAS run (or of a proposed plan), drawn as a
 * compact full-width table: one row per step with its status glyph, name, the
 * agent (accent-coloured), `model · effort` and a right-aligned duration. A
 * repeat is one row showing `↻ <passes>` with its body indented beneath it on
 * tree guide lines; the selected step carries an accent left bar, and a looped
 * step an iteration pager `‹ n/m ›` that switches which instance the chat below
 * shows. An included workflow is one collapsible row ("included workflow · N
 * steps"); a watch step shows a watch marker where an agent would sit.
 *
 * Given the live run (`row`) rows also offer the per-step controls KAS allows
 * (Stop, Retry) and say which step waits for a reply.
 */
import { createContext, useContext, useState } from 'react'
import { ChevronDown, ChevronLeft, ChevronRight, Eye, RotateCw, Workflow } from 'lucide-react'

import type { KasNodeDescriptor, KasRunNode, KasRunRow } from '../../../api/client'
import { fmtElapsed } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import {
  groupSummary,
  includeMemberLabel,
  includeStepCount,
  loopBodyChildren,
  loopIterationCount,
  nodeWaitLabel,
  retryWaitLine,
  stepAwaitsReply,
  stepDurationMs,
  stepIterationInstances,
  watchOutcomeLabel,
} from './kasModel'
import { KasStepGlyph, stepStatusLabel } from './KasRunBits'
import { KasTreeStepControls } from './KasParityControls'

/** A finished step's duration, e.g. `2m 10s`, or null while it runs or when KAS
 *  carries no timing for it. */
function stepDuration(node: KasRunNode): string | null {
  if (!node.endedAt) return null
  const ms = stepDurationMs(node, 0)
  return ms != null ? fmtElapsed(ms, { wholeSeconds: true }) : null
}

/** The row's trailing columns: the agent (accent) or a watch marker, then
 *  `model · effort`, then the step's duration — the IDE's step-row meta. */
function StepMeta({ node, isWatch }: { node: KasRunNode; isWatch: boolean }) {
  const model = [node.model, node.effort].filter(Boolean).join(' · ')
  const duration = stepDuration(node)
  if (!node.agent && !model && !duration && !isWatch) return null
  return (
    <span className="ml-auto pl-2 flex items-center gap-3 shrink-0 text-[11.5px]" data-testid="kas-step-meta">
      {isWatch ? (
        <span
          className="flex items-center gap-1 text-muted"
          data-testid="kas-watch-marker"
          title={i18nT('apps.workflows.kas.watch_marker')}
        >
          <Eye size={11} className="shrink-0" aria-hidden="true" />
          {i18nT('apps.workflows.kas.watch_marker')}
        </span>
      ) : node.agent ? (
        <span className="truncate max-w-[10rem] font-mono italic text-accent" data-testid="kas-step-agent">
          {node.agent}
        </span>
      ) : null}
      {model ? <span className="truncate max-w-[14rem] tabular-nums text-muted">{model}</span> : null}
      {duration ? (
        <span className="tabular-nums text-muted" data-testid="kas-step-duration">
          {duration}
        </span>
      ) : null}
    </span>
  )
}

/**
 * The tree guide column to the left of a row: a vertical line for each ancestor
 * that still has rows below it, then this node's elbow (`└` last child, `├`
 * otherwise). `prefix[i]` is whether the ancestor at that level continues. A
 * root row (`depth === 0`) draws no guide.
 */
function TreeGuides({ depth, prefix, isLast }: { depth: number; prefix: readonly boolean[]; isLast: boolean }) {
  if (depth === 0) return null
  return (
    <span className="flex self-stretch shrink-0" aria-hidden="true" data-testid="kas-tree-guide">
      {prefix.map((line, i) => (
        <span key={i} className="w-3.5 self-stretch flex justify-center">
          {line ? <span className="w-px self-stretch bg-border" /> : null}
        </span>
      ))}
      <span className="w-3.5 self-stretch relative flex justify-center">
        <span className={`w-px bg-border ${isLast ? 'h-1/2 self-start' : 'self-stretch'}`} />
        <span className="absolute left-1/2 right-0 top-1/2 h-px bg-border" />
      </span>
    </span>
  )
}

// ── STEPS tree ──

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
      <ul className="m-0 p-0 list-none flex flex-col" aria-label={label ?? i18nT('apps.workflows.kas.steps')} data-testid="kas-steps-tree">
        {nodes.map((node, i) => (
          <TreeNode
            key={node.path}
            node={node}
            depth={0}
            prefix={[]}
            isLast={i === nodes.length - 1}
            selectedPath={selectedPath}
            onSelect={onSelect}
          />
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

/** The children a tree node shows. A repeat renders its body ONCE (the latest
 *  iteration's nodes) with the repeat passed as `loop`, so a body step offers
 *  its iteration pager; every other group renders its definition children. */
function NodeChildren({
  node,
  depth,
  prefix,
  isLast,
  selectedPath,
  onSelect,
}: {
  node: KasRunNode
  depth: number
  prefix: readonly boolean[]
  isLast: boolean
  selectedPath: string | null
  onSelect: (nodeId: string, path: string) => void
}) {
  const kids = node.type === 'repeat' ? loopBodyChildren(node) : node.children
  if (kids.length === 0) return null
  // An included workflow's members carry the include's id as a prefix
  // (`review/setup`); inside its labelled group they read without it.
  const includeId = node.include ? node.id : null
  const loop = node.type === 'repeat' ? node : null
  // A root node's children draw no ancestor line to its left; deeper ones
  // extend the guide with whether THIS node still has rows below it.
  const childPrefix = depth === 0 ? [] : [...prefix, !isLast]
  return (
    <ul className="m-0 p-0 list-none flex flex-col">
      {kids.map((child, i) => (
        <TreeNode
          key={child.path}
          node={child}
          depth={depth + 1}
          prefix={childPrefix}
          isLast={i === kids.length - 1}
          selectedPath={selectedPath}
          onSelect={onSelect}
          includeId={includeId}
          loop={loop}
        />
      ))}
    </ul>
  )
}

/** The iteration pager on the selected looped step: `‹ n/m ›`, switching which
 *  instance the chat below shows. Shown only when the step ran more than once. */
function IterationPager({
  instances,
  index,
  onSelect,
}: {
  instances: readonly KasRunNode[]
  index: number
  onSelect: (nodeId: string, path: string) => void
}) {
  const go = (to: number) => {
    const target = instances[to]
    if (target) onSelect(target.id, target.path)
  }
  return (
    <span className="flex items-center gap-0.5 shrink-0 text-[11px] text-muted tabular-nums" data-testid="kas-iteration-pager">
      <button
        type="button"
        className="inline-flex p-0 bg-transparent border-none text-muted cursor-pointer hover:text-text disabled:opacity-40 disabled:cursor-default"
        onClick={e => {
          e.stopPropagation()
          go(index - 1)
        }}
        disabled={index <= 0}
        aria-label={i18nT('apps.workflows.kas.iteration_prev')}
      >
        <ChevronLeft size={12} aria-hidden="true" />
      </button>
      <span aria-label={i18nT('apps.workflows.kas.iteration_position', { n: index + 1, max: instances.length })}>
        {index + 1}/{instances.length}
      </span>
      <button
        type="button"
        className="inline-flex p-0 bg-transparent border-none text-muted cursor-pointer hover:text-text disabled:opacity-40 disabled:cursor-default"
        onClick={e => {
          e.stopPropagation()
          go(index + 1)
        }}
        disabled={index >= instances.length - 1}
        aria-label={i18nT('apps.workflows.kas.iteration_next')}
      >
        <ChevronRight size={12} aria-hidden="true" />
      </button>
    </span>
  )
}

function TreeNode({
  node,
  depth,
  prefix,
  isLast,
  selectedPath,
  onSelect,
  includeId = null,
  loop = null,
}: {
  node: KasRunNode
  depth: number
  prefix: readonly boolean[]
  isLast: boolean
  selectedPath: string | null
  onSelect: (nodeId: string, path: string) => void
  /** The enclosing include group's id, whose `id/` prefix the label drops. */
  includeId?: string | null
  /** The enclosing repeat, when this is a loop-body node: enables the pager. */
  loop?: KasRunNode | null
}) {
  const run = useContext(TreeRunContext)
  const isWatch = node.type === 'watch'
  const isGroup = node.type !== 'step' && !isWatch
  const [open, setOpen] = useState(!node.include)
  const status = stepStatusLabel(node.status)
  const label = includeMemberLabel(node.label, includeId)

  if (isGroup) {
    const passes = loopIterationCount(node)
    const maxPasses = node.maxIterations ?? node.loop?.max ?? null
    // An included workflow is one collapsible row: "included workflow · N steps".
    const includeSummary = node.include
      ? i18nT('apps.workflows.kas.included_workflow_steps', { n: includeStepCount(node) })
      : null
    // Collapsed, a plain group still says what is inside (gap 23).
    const summary = open || node.include ? null : groupSummary(node)
    return (
      <li>
        <div className="flex items-stretch">
          <TreeGuides depth={depth} prefix={prefix} isLast={isLast} />
          <button
            type="button"
            className="flex-1 min-w-0 flex items-center gap-1.5 rounded px-1.5 py-1 bg-transparent border-none text-left text-[12px] text-text cursor-pointer hover:bg-bg-hover"
            onClick={() => setOpen(v => !v)}
            aria-expanded={open}
            title={node.include?.path || node.include?.recipe || undefined}
            data-testid={node.include ? 'kas-include-group' : 'kas-step-group'}
            data-status={node.status}
          >
            {open ? (
              <ChevronDown size={12} className="text-muted shrink-0" aria-hidden="true" />
            ) : (
              <ChevronRight size={12} className="text-muted shrink-0" aria-hidden="true" />
            )}
            <KasStepGlyph status={node.status} />
            <span className="sr-only">{status}</span>
            {node.include ? (
              <>
                <Workflow size={12} className="text-muted shrink-0" aria-hidden="true" />
                <span className="sr-only">{i18nT('apps.workflows.kas.included_workflow')}</span>
              </>
            ) : null}
            <span className="truncate min-w-0 font-medium">{label}</span>
            {includeSummary ? (
              <span className="text-[11px] text-muted truncate min-w-0" data-testid="kas-include-summary">
                {includeSummary}
              </span>
            ) : null}
            {passes != null ? (
              <span className="flex items-center gap-0.5 text-[11px] text-muted tabular-nums shrink-0" data-testid="kas-loop-passes">
                <RotateCw size={11} className="shrink-0" aria-hidden="true" />
                {maxPasses != null ? `${passes}/${maxPasses}` : passes}
              </span>
            ) : maxPasses != null ? (
              // A plan preview has run no passes yet: name the budget instead.
              <span className="text-[11px] text-muted tabular-nums shrink-0" data-testid="kas-loop-max">
                {i18nT('apps.workflows.kas.max_iterations', { max: maxPasses })}
              </span>
            ) : null}
            {summary ? (
              <span className="text-[11px] text-muted truncate min-w-0" data-testid="kas-group-summary">
                {summary}
              </span>
            ) : null}
            <StepMeta node={node} isWatch={false} />
          </button>
        </div>
        {open ? (
          <NodeChildren node={node} depth={depth} prefix={prefix} isLast={isLast} selectedPath={selectedPath} onSelect={onSelect} />
        ) : null}
      </li>
    )
  }

  // A loop-body step's realised instances, so the selected row shows a pager
  // and the row reflects whichever instance is selected (else the latest).
  const instances = loop ? stepIterationInstances(loop, node.id) : []
  const selectedIdx = instances.findIndex(inst => inst.path === selectedPath)
  const selected = selectedPath === node.path || selectedIdx >= 0
  const shown = selectedIdx >= 0 ? instances[selectedIdx] : node
  const pagerIndex = selectedIdx >= 0 ? selectedIdx : instances.length - 1

  const waiting = run ? stepAwaitsReply(run, shown) : false
  // A step row's secondary line, in priority order (gap 1 / 23).
  const retry = retryWaitLine(shown)
  const waitTag = nodeWaitLabel(shown)
  const watchLine = isWatch ? watchOutcomeLabel(shown) : null
  const secondary = retry
    ? { text: retry, warn: true, testid: 'kas-step-retry-wait' }
    : waitTag
      ? { text: waitTag, warn: true, testid: 'kas-step-wait' }
      : waiting
        ? { text: i18nT('apps.workflows.kas.waiting_reply'), warn: true, testid: 'kas-step-waiting' }
        : watchLine
          ? { text: watchLine, warn: false, testid: 'kas-watch-outcome' }
          : shown.reason
            ? { text: shown.reason, warn: false, testid: 'kas-step-reason' }
            : null
  const content = (
    <>
      <KasStepGlyph status={shown.status} />
      <span className="sr-only">{waiting ? i18nT('apps.workflows.kas.waiting_reply') : status}</span>
      <span className={`truncate min-w-0 ${shown.status === 'aborted' || shown.status === 'skipped' ? 'text-muted' : ''}`}>{label}</span>
      {shown.replacementPending ? (
        <span className="shrink-0 rounded px-1.5 py-px text-[10px] bg-bg-hover border border-border text-muted" data-testid="kas-will-replace">
          {i18nT('apps.workflows.kas.will_be_replaced')}
        </span>
      ) : null}
      {secondary ? (
        <span className={`text-[11px] truncate min-w-0 ${secondary.warn ? 'text-warn' : 'text-muted'}`} data-testid={secondary.testid}>
          {secondary.text}
        </span>
      ) : null}
      {selected && instances.length > 1 ? <IterationPager instances={instances} index={pagerIndex} onSelect={onSelect} /> : null}
      <StepMeta node={shown} isWatch={isWatch} />
    </>
  )
  // The selected row carries an accent LEFT bar; the whole row stays the same
  // width, so the bar replaces the old full border.
  const rowClass = `flex-1 min-w-0 flex items-center gap-1.5 rounded-r px-1.5 py-1 text-left text-[12px] border-l-[3px] ${
    selected ? 'bg-accent-subtle border-l-accent text-text-strong' : 'border-l-transparent text-text'
  }`
  return (
    <li className="flex items-stretch gap-0.5 min-w-0">
      <TreeGuides depth={depth} prefix={prefix} isLast={isLast} />
      {shown.sessionId ? (
        <button
          type="button"
          className={`${rowClass} cursor-pointer bg-transparent hover:bg-bg-hover`}
          onClick={() => onSelect(shown.id, shown.path)}
          aria-current={selected ? 'true' : undefined}
          data-testid="kas-tree-step"
          data-status={shown.status}
        >
          {content}
        </button>
      ) : (
        <div className={rowClass} aria-current={selected ? 'true' : undefined} data-testid="kas-tree-step" data-status={shown.status}>
          {content}
        </div>
      )}
      {run ? <KasTreeStepControls row={run} node={shown} label={label} /> : null}
    </li>
  )
}
