import { useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'

import { api } from '../api/client'
import { fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { SubagentActivity } from '../types'
import {
  type ContextTrace,
  groupBlocks,
  USER_LABEL,
} from './ContextBreakdownPanel'
import { sourceFill } from './contextSourceColors'

/**
 * Session Breakdown tree — the topology half fused onto the composition half.
 *
 * The per-turn Context Breakdown panel answers "what filled THIS session's
 * window" but stops at the session boundary: a `spawn_run` is one tool call and
 * each sub-agent's turns live in a DIFFERENT session. This header restores the
 * spawn tree AND gives every node its own composition — one expandable node per
 * sub-agent, each fetching ITS OWN `context-trace` by the child session key the
 * backend now stamps on the subagent frames (`childSession`). So a reader sees,
 * per node, both who-spawned-whom and what is filling that node's window —
 * which neither the Subagents view nor the Context panel showed alone.
 *
 * Renders nothing when the session spawned nothing: a plain session has no tree
 * and the per-turn rows already tell its whole story.
 */

type NodeStatus = 'running' | 'done' | 'error'

function nodeStatus(s: SubagentActivity['status']): NodeStatus {
  if (s === 'done') return 'done'
  if (s === 'error' || s === 'stopped') return 'error'
  return 'running'
}

const STATUS_KEY: Record<NodeStatus, string> = {
  running: 'pages.sessionBreakdown.status_running',
  done: 'pages.sessionBreakdown.status_done',
  error: 'pages.sessionBreakdown.status_error',
}
const STATUS_CLASS: Record<NodeStatus, string> = {
  running: 'text-[var(--warn)]',
  done: 'text-accent',
  error: 'text-danger',
}
const DOT_CLASS: Record<NodeStatus, string> = {
  running: 'bg-[var(--warn)] text-[var(--warn)]',
  done: 'bg-accent text-accent',
  error: 'bg-danger text-danger',
}

/** Context rows are in CHARS; show a compact humanised figure (the panel is the
 *  authority on the chars/token caveat -- here we only need a comparable size).
 *  Plain humanised chars (no compact unit suffix), matching the per-turn panel's
 *  own char column, which also keeps the i18n number+unit gate satisfied. */
const fmtTok = (chars: number): string => fmtNumber(Math.round(chars))

interface MiniSeg { key: string; pct: number; fill: string; isUser?: boolean }

/**
 * Fixed per-source hue map for the node composition traces.
 *
 * Both the tree's node traces AND the per-turn Context Breakdown panel below
 * now colour a source the SAME way, from the shared `contextSourceColors` map:
 * hue is the data channel (a bar stacks many sources and is compared across
 * rows/nodes), so a reader sees "this node is mostly history, that one mostly
 * skill" at a glance. Unrecognised labels share a neutral mute; the user's own
 * text stays on the accent, matching both surfaces.
 */

/** Compose a node's window bar from a trace's grouped totals, ranked, user
 *  last. Coloured by the shared per-source hue so a node's mix reads at a
 *  glance and compares across nodes and against the panel below. */
export function nodeSegments(totals: Record<string, number>): MiniSeg[] {
  const grouped = groupBlocks(totals)
  const total = Object.values(grouped).reduce((a, b) => a + b, 0)
  if (total <= 0) return []
  const nonUser = Object.entries(grouped)
    .filter(([label]) => label !== USER_LABEL)
    .sort((a, b) => b[1] - a[1])
  const segs: MiniSeg[] = nonUser.map(([label, chars]) => ({
    key: label,
    pct: (chars / total) * 100,
    fill: sourceFill(label),
  }))
  const userChars = grouped[USER_LABEL] ?? 0
  if (userChars > 0) segs.push({ key: USER_LABEL, pct: (userChars / total) * 100, fill: 'var(--accent)', isUser: true })
  return segs
}

/** Occupancy: how full this node's window got, as a fraction of its own
 *  context_window (peak token occupancy the trace already carries). */
function occupancy(trace: ContextTrace | undefined): number {
  if (!trace || trace.context_window <= 0) return 0
  return Math.max(0, Math.min(1, trace.peak_context_used / trace.context_window))
}

function Trace({ segs }: { segs: MiniSeg[] }) {
  if (segs.length === 0) return null
  return (
    <div className="h-1.5 rounded-[1px] overflow-hidden flex outline outline-1 outline-[var(--border)]" style={{ width: 110 }}>
      {segs.map(s => (
        <div key={s.key} className="h-full" style={{ width: `${s.pct}%`, background: s.fill, minWidth: s.isUser ? 2 : undefined }} />
      ))}
    </div>
  )
}

function Gauge({ frac }: { frac: number }) {
  return (
    <span
      className="relative inline-block w-[38px] h-3.5 rounded-[2px] overflow-hidden border border-[var(--border-strong)] bg-bg shrink-0"
      title={i18nT('pages.sessionBreakdown.occupancy', { pct: fmtNumber(Math.round(frac * 100)) })}
      aria-hidden="true"
    >
      <i className="absolute left-0 top-0 bottom-0 bg-accent/25 border-r border-accent" style={{ width: `${Math.max(2, frac * 100)}%` }} />
    </span>
  )
}

/** One expandable sub-agent node: a topology row (dot / name / trace or status
 *  / gauge / total) that expands to that agent's OWN per-turn composition. */
function SubNode({
  node,
  trace,
}: {
  node: SubagentActivity
  trace: ContextTrace | undefined
}) {
  const [open, setOpen] = useState(false)
  const status = nodeStatus(node.status)
  const segs = trace ? nodeSegments(trace.totals) : []
  const total = trace ? trace.injected_chars : 0
  const turns = trace?.turns ?? []
  const maxTurn = Math.max(1, ...turns.map(t => t.total_chars))
  // Only offer expansion when there is a trace to show; a node whose child
  // session wrote no ctx_blocks (native card, or a run too short) stays a
  // one-line topology row.
  const expandable = segs.length > 0

  return (
    <div className="relative">
      <div
        className={`flex flex-wrap items-center gap-x-2.5 gap-y-1 pl-9 md:pl-11 pr-3.5 py-2 relative ${expandable ? 'cursor-pointer hover:bg-bg-hover' : ''}`}
        {...(expandable
          ? {
              role: 'button' as const,
              tabIndex: 0,
              'aria-expanded': open,
              onClick: () => setOpen(v => !v),
              onKeyDown: (e: React.KeyboardEvent) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(v => !v) }
              },
            }
          : {})}
      >
        {/* rail tick into this node */}
        <span className="absolute left-[22px] top-[18px] w-3 h-px bg-[var(--border-strong)]" aria-hidden="true" />
        <span className={`shrink-0 transition-transform ${open ? 'rotate-90 text-accent' : 'text-muted'}`}>
          {expandable ? <ChevronRight className="lucide-inline" size={12} /> : <span className="inline-block w-3" />}
        </span>
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${DOT_CLASS[status]}`} style={{ boxShadow: '0 0 8px currentColor' }} aria-hidden="true" />
        <span className="text-[12.5px] font-semibold text-text truncate min-w-0">{node.agent || i18nT('pages.sessionBreakdown.unknown_agent')}</span>
        {node.stalled ? <span className="font-mono text-[10px] text-[var(--warn)]">{i18nT('pages.sessionBreakdown.stalled')}</span> : null}
        <span className="flex-1 min-w-0" />
        {!open && segs.length > 0 ? <Trace segs={segs} /> : null}
        {typeof node.toolCount === 'number' && node.toolCount > 0 ? (
          <span className="font-mono text-[10px] text-muted shrink-0 tabular-nums">
            {i18nT('pages.sessionBreakdown.tool_count', { count: fmtNumber(node.toolCount) })}
          </span>
        ) : null}
        <span className={`font-mono text-[10px] font-bold uppercase tracking-wider shrink-0 ${STATUS_CLASS[status]}`}>
          {i18nT(STATUS_KEY[status])}
        </span>
        {trace ? <Gauge frac={occupancy(trace)} /> : null}
        <span className="font-mono text-[11px] text-text text-right tabular-nums shrink-0 min-w-[44px]">{total > 0 ? fmtTok(total) : '\u2014'}</span>
      </div>
      {open && expandable ? (
        <div className="pl-11 pr-3.5 pb-3 bg-bg-elevated">
          <div className="font-mono text-[10px] text-muted uppercase tracking-wide py-1.5">
            {i18nT('pages.sessionBreakdown.node_caption', {
              model: node.model || i18nT('pages.sessionBreakdown.unknown_model'),
              turns: fmtNumber(turns.length),
            })}
          </div>
          {turns.map((t, i) => {
            const tsegs = nodeSegments(t.blocks)
            return (
              <div key={i} className="grid grid-cols-[2rem_1fr_3rem] gap-2.5 items-center py-[2px]">
                <span className="font-mono text-[10px] text-muted text-right">t{i + 1}</span>
                <div className="h-4 flex rounded-[2px] overflow-hidden outline outline-1 outline-[var(--border)] bg-bg" style={{ width: `${Math.max(8, Math.sqrt(t.total_chars / maxTurn) * 100)}%` }}>
                  {tsegs.map(s => <span key={s.key} className="h-full" style={{ width: `${s.pct}%`, background: s.fill }} />)}
                </div>
                <span className="font-mono text-[10px] text-text text-right tabular-nums">{fmtTok(t.total_chars)}</span>
              </div>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

/** The tree header. `subagents` is the live map the Subagents panel subscribes
 *  to; nodes sort by spawn time. Each node's own trace is fetched by its
 *  `childSession` key (parallel queries, only for nodes that carry one). */
export function SessionBreakdownTree({
  subagents,
}: {
  subagents: Record<string, SubagentActivity>
}) {
  const [open, setOpen] = useState(true)
  const nodes = Object.values(subagents).sort((a, b) => a.startedAt - b.startedAt)

  // Fetch each sub-agent's OWN context-trace by its child session key. Hooks
  // must run unconditionally, so this runs before the early return; with no
  // nodes the query list is empty and useQueries is a no-op.
  const childKeys = nodes.map(n => n.childSession || '')
  const childQueries = useQueries({
    queries: childKeys.map(key => ({
      queryKey: ['context-trace', key],
      queryFn: () => api.telemetryContextTrace(key),
      enabled: !!key,
      // A sub-agent's rows appear a turn or two after it spawns, so a node
      // fetched too early comes back empty; refetch on the same cadence the
      // main panel uses so the composition fills in without a manual reopen.
      refetchInterval: 15_000,
      staleTime: 10_000,
    })),
  })
  const traceFor = (i: number): ContextTrace | undefined =>
    (childQueries[i]?.data as ContextTrace | undefined) ?? undefined

  if (nodes.length === 0) return null

  const running = nodes.filter(n => nodeStatus(n.status) === 'running').length

  return (
    <div className="border border-border bg-card rounded-xl overflow-hidden mb-3">
      <button
        className="w-full flex items-center justify-between gap-3 px-3.5 py-3 border-b border-border bg-[var(--bg-accent)] cursor-pointer bg-transparent"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        <span className="text-[11.5px] font-semibold uppercase tracking-wide text-text">
          {i18nT('pages.sessionBreakdown.title')}
        </span>
        <span className="font-mono text-[11px] text-muted">
          {i18nT('pages.sessionBreakdown.header_meta', { subs: fmtNumber(nodes.length), running: fmtNumber(running) })}
        </span>
      </button>
      {open ? (
        <div className="relative py-1">
          {/* spawn rail */}
          <span className="absolute left-[22px] top-2 bottom-2 w-px bg-[var(--border-strong)]" aria-hidden="true" />
          {nodes.map((node, i) => (
            <SubNode key={node.id} node={node} trace={traceFor(i)} />
          ))}
          {/* Legend: the trace hues are the data channel, so decode them once
              for the whole tree rather than per node. */}
          <div className="flex flex-wrap gap-x-3.5 gap-y-1 px-3.5 pt-1.5 pb-2.5 text-[10px] text-muted">
            {([
              ['loaded_skill', i18nT('pages.sessionBreakdown.legend_skill')],
              ['memory', i18nT('pages.sessionBreakdown.legend_memory')],
              ['history', i18nT('pages.sessionBreakdown.legend_history')],
              ['lessons', i18nT('pages.sessionBreakdown.legend_lessons')],
              ['agent_instructions', i18nT('pages.sessionBreakdown.legend_sys')],
              ['skill_index', i18nT('pages.sessionBreakdown.legend_skillidx')],
            ] as [string, string][]).map(([label, text]) => (
              <span key={label} className="inline-flex items-center">
                <i className="inline-block w-2 h-2 rounded-[1px] mr-1.5 align-[-1px]" style={{ background: sourceFill(label) }} aria-hidden="true" />
                {text}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  )
}
