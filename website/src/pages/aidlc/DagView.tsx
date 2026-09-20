import { useMemo, type ReactNode } from 'react'
import { Wrench, Shield, Hourglass, Pause, SkipForward, Square, Check, X } from 'lucide-react'

import { i18nT } from '../../i18n/t'
interface DagNode { id: string; title: string; status: string; task_type?: string; requires_approval?: boolean }
interface DagEdge { from: string; to: string }

const STATUS_FILL: Record<string, string> = {
  pending: 'var(--muted)', in_progress: 'var(--accent)', reviewing: 'var(--accent)', passed: 'var(--ok)', failed: 'var(--danger)', skipped: 'var(--muted)', cancelled: 'var(--muted)', cancelling: 'var(--accent)', paused: 'var(--info)', pausing: 'var(--accent)', blocked: 'var(--warn)',
}
const StatusDot = ({ color }: { color: string }) => <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: color }} />
/**
 * Legend rows under the graph. `label` is a GETTER, not a value: this table is
 * evaluated once at import, so an `i18nT()` call in the initialiser would freeze
 * the boot language and never re-resolve on a language switch. A getter runs on
 * every property access, and the only access is the `Object.values(...)` map in
 * the render below.
 *
 * `failed` / `skipped` / `cancelled` deliberately point at the `phasedView` keys:
 * PhasedView groups the very same aidlc task statuses under those exact words, so
 * the two views must not be able to drift apart. The rest have no existing key.
 */
const STATUS_DOT: Record<string, { color: string; label: ReactNode; key: string }> = {
  pending: { color: 'var(--muted)', get label() { return <><StatusDot color="var(--muted)" /> {i18nT('pages.aidlc.dagView.pending')}</> }, key: 'pending' },
  in_progress: { color: 'var(--accent)', get label() { return <><StatusDot color="var(--accent)" /> {i18nT('pages.aidlc.dagView.running')}</> }, key: 'in_progress' },
  reviewing: { color: 'var(--accent)', get label() { return <><StatusDot color="var(--accent)" /> {i18nT('pages.aidlc.dagView.reviewing')}</> }, key: 'reviewing' },
  passed: { color: 'var(--ok)', get label() { return <><StatusDot color="var(--ok)" /> {i18nT('pages.aidlc.dagView.done')}</> }, key: 'passed' },
  failed: { color: 'var(--danger)', get label() { return <><StatusDot color="var(--danger)" /> {i18nT('pages.aidlc.phasedView.failed')}</> }, key: 'failed' },
  skipped: { color: 'var(--muted)', get label() { return <><SkipForward className="lucide-inline" /> {i18nT('pages.aidlc.phasedView.skipped')}</> }, key: 'skipped' },
  cancelled: { color: 'var(--muted)', get label() { return <><Square className="lucide-inline" /> {i18nT('pages.aidlc.phasedView.cancelled')}</> }, key: 'cancelled' },
  cancelling: { color: 'var(--accent)', get label() { return <><Hourglass className="lucide-inline" /> {i18nT('pages.aidlc.dagView.cancelling')}</> }, key: 'cancelling' },
  paused: { color: 'var(--info)', get label() { return <><Pause className="lucide-inline" /> {i18nT('pages.aidlc.dagView.paused')}</> }, key: 'paused' },
  pausing: { color: 'var(--accent)', get label() { return <><Hourglass className="lucide-inline" /> {i18nT('pages.aidlc.dagView.pausing')}</> }, key: 'pausing' },
  blocked: { color: 'var(--warn)', get label() { return <><StatusDot color="var(--warn)" /> {i18nT('pages.aidlc.dagView.needs_approval')}</> }, key: 'blocked' },
}
/** Typed nodes tint their card with the stroke token at 8% (`fillOpacity`),
 *  which needs no `*-subtle` variant of the token. Both types share the
 *  neutral `muted`: the icon says which type it is, and a type is not a state,
 *  so it must not borrow a status color (warn reads as "needs approval" and
 *  info as "paused" in the legend). */
const TYPE_STYLE: Record<string, { fill: string; stroke: string; icon: ReactNode }> = {
  fix: { fill: 'var(--muted)', stroke: 'var(--muted)', icon: <Wrench size={16} /> },
  checkpoint: { fill: 'var(--muted)', stroke: 'var(--muted)', icon: <Shield size={16} /> },
}
const TYPE_FILL_OPACITY = 0.08
const NODE_W = 180, NODE_H = 56, GAP_X = 60, GAP_Y = 40, PAD = 40

export default function DagView({ nodes, edges, onNodeClick, selectedId, pendingEditIds, approvalMap, onApprove }: {
  nodes: DagNode[]; edges: DagEdge[]; onNodeClick: (id: string) => void
  selectedId?: string; pendingEditIds?: Set<string>
  approvalMap?: Record<number, string>; onApprove?: (index: number, decision: 'approve' | 'reject') => void
}) {
  const layout = useMemo(() => {
    if (!nodes.length) return { positioned: [], width: 0, height: 0 }
    const adj = new Map<string, string[]>()
    const indeg = new Map<string, number>()
    nodes.forEach(n => { adj.set(n.id, []); indeg.set(n.id, 0) })
    edges.forEach(e => { adj.get(e.from)?.push(e.to); indeg.set(e.to, (indeg.get(e.to) || 0) + 1) })
    const layers: string[][] = []
    let queue = nodes.filter(n => (indeg.get(n.id) || 0) === 0).map(n => n.id)
    while (queue.length) {
      layers.push(queue)
      const next: string[] = []
      for (const id of queue) {
        for (const child of adj.get(id) || []) {
          indeg.set(child, (indeg.get(child) || 0) - 1)
          if (indeg.get(child) === 0) next.push(child)
        }
      }
      queue = next
    }
    const pos = new Map<string, { x: number; y: number }>()
    const maxCols = layers.reduce((m, l) => Math.max(m, l.length), 0)
    layers.forEach((layer, li) => {
      const totalW = layer.length * NODE_W + (layer.length - 1) * GAP_X
      const startX = PAD + (maxCols * (NODE_W + GAP_X) - GAP_X - totalW) / 2
      layer.forEach((id, i) => pos.set(id, { x: startX + i * (NODE_W + GAP_X), y: PAD + li * (NODE_H + GAP_Y) }))
    })
    const positioned = nodes.map(n => ({ ...n, ...pos.get(n.id)! }))
    const width = maxCols * (NODE_W + GAP_X) - GAP_X + PAD * 2
    return { positioned, width, height: layers.length * (NODE_H + GAP_Y) + PAD }
  }, [nodes, edges])

  if (!nodes.length) return (
    <div className="text-[13px] text-center py-8" style={{ color: 'var(--muted)' }}>{i18nT('pages.aidlc.dagView.no_tasks_to_visualize')}</div>
  )

  return (
    <div className="min-w-0">
      {/* Viewport-adaptive cap: grow with the window (leaving ~320px for the page
          header, tab bar and legend) but never below the 480px the view always had,
          so short windows keep today's behavior while tall ones show more layers. */}
      <div className="overflow-auto rounded-lg" style={{ maxHeight: 'max(480px, calc(100vh - 320px))', border: '1px solid var(--border)', background: 'var(--bg-elevated)' }}>
        <svg width={layout.width} height={layout.height}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 7" refX="10" refY="3.5" markerWidth="8" markerHeight="6" orient="auto-start-reverse">
            <polygon points="0 0, 10 3.5, 0 7" fill="var(--muted)" />
          </marker>
          <linearGradient id="arcGrad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.2}/>
            <stop offset="0%" stopColor="var(--accent)" stopOpacity={1}><animate attributeName="offset" values="0;0.6;1;0" dur="2s" repeatCount="indefinite"/></stop>
            <stop offset="0.2%" stopColor="var(--accent)" stopOpacity={1}><animate attributeName="offset" values="0.2;0.8;1;0.2" dur="2s" repeatCount="indefinite"/></stop>
            <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.2}/>
          </linearGradient>
        </defs>
        {edges.map((e, i) => {
          const from = layout.positioned.find(n => n.id === e.from)
          const to = layout.positioned.find(n => n.id === e.to)
          if (!from || !to) return null
          const x1 = from.x + NODE_W / 2, y1 = from.y + NODE_H
          const x2 = to.x + NODE_W / 2, y2 = to.y
          const cy1 = y1 + (y2 - y1) * 0.4, cy2 = y1 + (y2 - y1) * 0.6
          const d = `M${x1},${y1} C${x1},${cy1} ${x2},${cy2} ${x2},${y2}`
          const toActive = to.status === 'in_progress' || to.status === 'reviewing'
          return <g key={i}>
            <path d={d} fill="none" stroke={STATUS_FILL[from.status] || 'var(--muted)'} strokeWidth={1.5} strokeDasharray="4 3" markerEnd="url(#arrow)" opacity={0.5} />
            {toActive && <>
              <circle r={3} fill="var(--accent)"><animateMotion dur="1.5s" repeatCount="indefinite" path={d}/></circle>
              <circle r={3} fill="var(--accent)" opacity={0.4}><animateMotion dur="1.5s" begin="0.5s" repeatCount="indefinite" path={d}/></circle>
            </>}
          </g>
        })}
        {layout.positioned.map(n => {
          const ts = TYPE_STYLE[n.task_type || '']
          const effectiveStatus = n.requires_approval && n.status === 'pending' ? 'blocked' : n.status
          const stroke = ts?.stroke || STATUS_FILL[effectiveStatus] || 'var(--muted)'
          const fill = ts?.fill || 'var(--card)'
          const fillOpacity = ts ? TYPE_FILL_OPACITY : 1
          const icon = ts?.icon
          const dotColor = STATUS_DOT[effectiveStatus]?.color || 'var(--muted)'
          const isSelected = n.id === selectedId
          const hasPendingEdit = pendingEditIds?.has(n.id)
          return (
            <g key={n.id} className="cursor-pointer" onClick={() => onNodeClick(n.id)}>
              {/* Dashed, not just accent-coloured: a running node's stroke is accent
                  too, so the dash pattern is what tells "selected" from "running". */}
              {isSelected && <rect x={n.x - 3} y={n.y - 3} width={NODE_W + 6} height={NODE_H + 6} rx={10}
                fill="none" stroke="var(--accent)" strokeWidth={2} strokeDasharray="6 3" opacity={0.7} />}
              {approvalMap?.[Number(n.id)] && effectiveStatus === 'in_progress' && <rect x={n.x - 4} y={n.y - 4} width={NODE_W + 8} height={NODE_H + 8} rx={12}
                fill="none" stroke="var(--warn)" strokeWidth={3}>
                <animate attributeName="opacity" values="0.8;0.3;0.8" dur="1.5s" repeatCount="indefinite"/>
              </rect>}
              <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx={8}
                fill={fill} fillOpacity={fillOpacity} stroke={effectiveStatus === 'in_progress' ? 'url(#arcGrad)' : stroke} strokeWidth={effectiveStatus === 'in_progress' ? 2.5 : 2} />
              {icon ? (
                <foreignObject x={n.x + 6} y={n.y + 6} width={20} height={20}><div>{icon}</div></foreignObject>
              ) : (
                <circle cx={n.x + 14} cy={n.y + 16} r={4} fill={dotColor} />
              )}
              <text x={n.x + (icon ? 30 : 24)} y={n.y + 19} fontSize={10} fill={stroke} fontWeight="600">
                {effectiveStatus === 'blocked' ? 'needs approval' : n.status === 'reviewing' ? 'in progress' : n.status.replace('_', ' ')}
              </text>
              <foreignObject x={n.x + 8} y={n.y + 28} width={NODE_W - 16} height={24}>
                <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-strong)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {n.title}
                </div>
              </foreignObject>
              {/* Same token as PhasedView's EDIT_DOT, so an unsaved edit looks the same in both views. */}
              {hasPendingEdit && <circle cx={n.x + NODE_W - 8} cy={n.y + 8} r={5} fill="var(--warn)" stroke="var(--card)" strokeWidth={1.5} />}
              {approvalMap?.[Number(n.id)] && onApprove && effectiveStatus === 'in_progress' && (
                <foreignObject x={n.x} y={n.y + NODE_H + 4} width={NODE_W} height={24}>
                  <div style={{ display: 'flex', gap: 4, justifyContent: 'center' }}>
                    <button onClick={e => { e.stopPropagation(); onApprove(Number(n.id), 'approve'); }} style={{ padding: '2px 10px', fontSize: 10, fontWeight: 600, background: 'var(--ok)', color: 'var(--ok-fg)', border: 'none', borderRadius: 4, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 2 }}><Check size={10} /> {i18nT('pages.aidlc.dagView.approve')}</button>
                    <button onClick={e => { e.stopPropagation(); onApprove(Number(n.id), 'reject'); }} style={{ padding: '2px 10px', fontSize: 10, fontWeight: 600, background: 'var(--danger)', color: 'var(--danger-fg)', border: 'none', borderRadius: 4, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 2 }}><X size={10} /> {i18nT('pages.aidlc.dagView.deny')}</button>
                  </div>
                </foreignObject>
              )}
            </g>
          )
        })}
      </svg>
      </div>
      <div className="flex gap-4 mt-2 px-2 flex-wrap" style={{ fontSize: 11, color: 'var(--muted)' }}>
        {Object.values(STATUS_DOT).map(d => <span key={d.key}>{d.label}</span>)}
      </div>
    </div>
  )
}
