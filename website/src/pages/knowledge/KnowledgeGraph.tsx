import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Network, RotateCcw } from 'lucide-react'
import type { Simulation, SimulationNodeDatum, SimulationLinkDatum, ZoomBehavior, Selection } from 'd3'
import { EmptyState } from '../../components/ui'
import { knowledgeApi } from './api'
import type { GraphData } from './types'

import { i18nT } from '../../i18n/t'
const TYPE_COLORS: Record<string, string> = { service: '#3b82f6', technology: '#22c55e', concept: '#a855f7', org: '#f97316' }

/** A graph node augmented with the position/velocity fields d3 mutates in. */
type SimNode = GraphData['nodes'][number] & SimulationNodeDatum
/** A graph edge; d3 resolves source/target from id strings to SimNode objects
 *  during ForceLink init, so they are SimNode after the simulation runs. */
type SimEdge = SimulationLinkDatum<SimNode> & { type: string; weight?: number }
/** After ForceLink init, source/target are resolved node objects. */
const endpoint = (v: SimEdge['source'] | SimEdge['target']): SimNode => v as SimNode

export default function KnowledgeGraph({ onSelectEntity, highlightEntity }: { onSelectEntity?: (name: string) => void; highlightEntity?: string | null }) {
  const svgRef = useRef<SVGSVGElement>(null)
  const zoomRef = useRef<{ reset: () => void; zoomToNode?: (name: string) => void } | null>(null)
  const simRef = useRef<Simulation<SimNode, SimEdge> | null>(null)
  const renderedKeyRef = useRef<string>('')
  // Graph bounds + zoom/selection refs captured after layout so a container
  // resize (viewport change, devtools toggle, split view) can recompute the
  // fit transform from current SVG dimensions instead of cached stale values.
  const graphBoundsRef = useRef<{ cx: number; cy: number; bw: number; bh: number; pad: number } | null>(null)
  const d3ZoomRef = useRef<ZoomBehavior<SVGSVGElement, unknown> | null>(null)
  const d3SelectionRef = useRef<Selection<SVGSVGElement, unknown, null, undefined> | null>(null)
  const d3Ref = useRef<typeof import('d3') | null>(null)
  const onSelectRef = useRef(onSelectEntity)
  onSelectRef.current = onSelectEntity
  const highlightRef = useRef(highlightEntity)
  highlightRef.current = highlightEntity
  const { data: graph, isLoading } = useQuery({ queryKey: ['knowledge-graph'], queryFn: () => knowledgeApi<GraphData>('/graph?limit=200') })

  // Highlight a node when highlightEntity changes (without full re-render)
  useEffect(() => {
    if (!svgRef.current) return
    if (!highlightEntity) {
      // Reset all nodes to default style
      const style = getComputedStyle(document.documentElement)
      const textColor = style.getPropertyValue('--text').trim() || '#fff'
      const circles = svgRef.current.querySelectorAll('g[data-entity] circle')
      circles.forEach(circle => {
        circle.setAttribute('stroke', textColor)
        circle.setAttribute('stroke-width', '1.5')
        circle.setAttribute('r', '8')
      })
      return
    }
    const svg = svgRef.current
    const style = getComputedStyle(document.documentElement)
    const accent = style.getPropertyValue('--accent').trim() || '#fbbf24'
    const text = style.getPropertyValue('--text').trim() || '#fff'
    const nodes = svg.querySelectorAll('g[data-entity]')
    nodes.forEach(n => {
      const circle = n.querySelector('circle')
      if (!circle) return
      if (n.getAttribute('data-entity') === highlightEntity) {
        circle.setAttribute('stroke', accent)
        circle.setAttribute('stroke-width', '4')
        circle.setAttribute('r', '12')
      } else {
        circle.setAttribute('stroke', text)
        circle.setAttribute('stroke-width', '1.5')
        circle.setAttribute('r', '8')
      }
    })
    // Zoom to the highlighted node (poll until graph is ready on first load)
    if (zoomRef.current?.zoomToNode) {
      zoomRef.current.zoomToNode(highlightEntity)
    } else {
      let attempts = 0
      const interval = setInterval(() => {
        attempts++
        if (zoomRef.current?.zoomToNode) {
          zoomRef.current.zoomToNode(highlightEntity)
          clearInterval(interval)
        } else if (attempts > 20) {
          clearInterval(interval)
        }
      }, 200)
      return () => clearInterval(interval)
    }
  }, [highlightEntity])

  useEffect(() => {
    if (!graph || !graph.nodes.length || !svgRef.current) return
    const key = graph.nodes.map(n => n.id).join(',') + '|' + graph.edges.length
    if (key === renderedKeyRef.current) return
    renderedKeyRef.current = key

    const svg = svgRef.current
    while (svg.firstChild) svg.removeChild(svg.firstChild)

    const style = getComputedStyle(document.documentElement)
    const textColor = style.getPropertyValue('--text').trim() || '#ccc'
    const mutedColor = style.getPropertyValue('--muted').trim() || '#888'
    const accentColor = style.getPropertyValue('--accent').trim() || '#fbbf24'

    let aborted = false
    import('d3').then(d3 => {
      if (aborted) return
      d3Ref.current = d3
      const s = d3.select(svg)
      const g = s.append('g')

      const zoom = d3.zoom<SVGSVGElement, unknown>().scaleExtent([0.3, 4]).on('zoom', (e) => g.attr('transform', e.transform))
      s.call(zoom)

      // Deep-clone to avoid mutating React Query cache
      const simNodes: SimNode[] = graph.nodes.map(n => ({ ...n }))
      const simEdges: SimEdge[] = graph.edges.map(e => ({ ...e }))

      simRef.current?.stop()
      const sim = d3.forceSimulation<SimNode, SimEdge>(simNodes)
        .force('link', d3.forceLink<SimNode, SimEdge>(simEdges).id((d) => d.id).distance(80))
        .force('charge', d3.forceManyBody().strength(-200))
        .force('collision', d3.forceCollide(25))
        .alphaDecay(0.08)
        .stop()
      simRef.current = sim

      for (let i = 0; i < 300; i++) sim.tick()

      const width = svg.clientWidth || 800
      const height = svg.clientHeight || 500
      const xs = simNodes.map((d) => d.x ?? 0)
      const ys = simNodes.map((d) => d.y ?? 0)
      const x0 = Math.min(...xs), x1 = Math.max(...xs)
      const y0 = Math.min(...ys), y1 = Math.max(...ys)
      const pad = 60
      const bw = (x1 - x0) || 1, bh = (y1 - y0) || 1
      const scale = Math.min((width - pad * 2) / bw, (height - pad * 2) / bh, 1.5)
      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2
      const fitTransform = d3.zoomIdentity.translate(width / 2 - cx * scale, height / 2 - cy * scale).scale(scale)
      s.call(zoom.transform, fitTransform)

      // Store bounds + zoom refs for resize recomputation
      graphBoundsRef.current = { cx, cy, bw, bh, pad }
      d3ZoomRef.current = zoom
      d3SelectionRef.current = s

      zoomRef.current = {
        reset: () => {
          // Recompute fitTransform from current SVG dimensions
          const w = svg.clientWidth || 800, h = svg.clientHeight || 500
          const bounds = graphBoundsRef.current
          const d3m = d3Ref.current
          if (!bounds || !d3m) return
          const sc = Math.min((w - bounds.pad * 2) / bounds.bw, (h - bounds.pad * 2) / bounds.bh, 1.5)
          const ft = d3m.zoomIdentity.translate(w / 2 - bounds.cx * sc, h / 2 - bounds.cy * sc).scale(sc)
          s.transition().duration(300).call(zoom.transform, ft)
        },
        zoomToNode: (name: string) => {
          const target = simNodes.find((n) => n.name === name)
          if (!target) return
          const w = svg.clientWidth || 800, h = svg.clientHeight || 500
          const t = d3.zoomIdentity.translate(w / 2 - (target.x ?? 0) * 2, h / 2 - (target.y ?? 0) * 2).scale(2)
          s.transition().duration(500).call(zoom.transform, t)
        },
      }

      const link = g.append('g').attr('stroke', mutedColor).attr('stroke-opacity', 1)
        .selectAll<SVGLineElement, SimEdge>('line').data(simEdges).join('line').attr('stroke-width', (d) => Math.max(1.5, (d.weight || 1) * 1.5))
        .attr('x1', (d) => endpoint(d.source).x ?? 0).attr('y1', (d) => endpoint(d.source).y ?? 0)
        .attr('x2', (d) => endpoint(d.target).x ?? 0).attr('y2', (d) => endpoint(d.target).y ?? 0)

      const node = g.append('g').selectAll<SVGGElement, SimNode>('g').data(simNodes).join('g').attr('cursor', 'pointer')
        .attr('transform', (d) => `translate(${d.x},${d.y})`)
        .attr('data-entity', (d) => d.name)
        .call(d3.drag<SVGGElement, SimNode>().clickDistance(8).on('start', (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y })
          .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y })
          .on('end', (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null }))

      node.append('circle').attr('r', (d) => highlightRef.current === d.name ? 12 : 8)
        .attr('fill', (d) => TYPE_COLORS[d.type] || '#6b7280')
        .attr('stroke', (d) => highlightRef.current === d.name ? accentColor : textColor)
        .attr('stroke-width', (d) => highlightRef.current === d.name ? 4 : 1.5)
        .style('pointer-events', 'all')

      node.append('text').text((d) => d.name).attr('x', 12).attr('y', 4)
        .attr('font-size', '10px').attr('fill', textColor).attr('pointer-events', 'none')

      node.on('click', (_e, d) => { onSelectRef.current?.(d.name) })

      node.on('mouseenter', function() { d3.select(this).select('circle').attr('stroke', accentColor).attr('stroke-width', 3) })
        .on('mouseleave', function(this: SVGGElement, _e, d) {
          const isHighlighted = highlightRef.current === d.name
          d3.select(this).select('circle')
            .attr('stroke', isHighlighted ? accentColor : textColor)
            .attr('stroke-width', isHighlighted ? 4 : 1.5)
        })

      const edgeLabel = g.append('g').selectAll<SVGTextElement, SimEdge>('text').data(simEdges).join('text')
        .attr('font-size', '8px').attr('fill', mutedColor).attr('text-anchor', 'middle')
        .text((d) => d.type)
        .attr('x', (d) => ((endpoint(d.source).x ?? 0) + (endpoint(d.target).x ?? 0)) / 2).attr('y', (d) => ((endpoint(d.source).y ?? 0) + (endpoint(d.target).y ?? 0)) / 2)

      sim.on('tick', () => {
        link.attr('x1', (d) => endpoint(d.source).x ?? 0).attr('y1', (d) => endpoint(d.source).y ?? 0)
          .attr('x2', (d) => endpoint(d.target).x ?? 0).attr('y2', (d) => endpoint(d.target).y ?? 0)
        node.attr('transform', (d) => `translate(${d.x},${d.y})`)
        edgeLabel.attr('x', (d) => ((endpoint(d.source).x ?? 0) + (endpoint(d.target).x ?? 0)) / 2).attr('y', (d) => ((endpoint(d.source).y ?? 0) + (endpoint(d.target).y ?? 0)) / 2)
      })

      // If a node was pre-selected (e.g. from search), zoom to it now
      if (highlightRef.current) {
        zoomRef.current?.zoomToNode?.(highlightRef.current)
      }
    }).catch(() => {})
    return () => { aborted = true; simRef.current?.stop() }
  }, [graph])

  // Recompute fit on SVG resize (viewport changes, devtools toggle, split view)
  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const ro = new ResizeObserver(() => {
      const bounds = graphBoundsRef.current
      const zoom = d3ZoomRef.current
      const sel = d3SelectionRef.current
      const d3 = d3Ref.current
      if (!bounds || !zoom || !sel || !d3) return
      const w = svg.clientWidth || 800, h = svg.clientHeight || 500
      const sc = Math.min((w - bounds.pad * 2) / bounds.bw, (h - bounds.pad * 2) / bounds.bh, 1.5)
      const ft = d3.zoomIdentity.translate(w / 2 - bounds.cx * sc, h / 2 - bounds.cy * sc).scale(sc)
      sel.call(zoom.transform, ft)
    })
    ro.observe(svg)
    return () => ro.disconnect()
  }, [graph])

  if (isLoading) return <div className="text-muted text-sm p-4">{i18nT('pages.knowledge.knowledgeGraph.loading_graph')}</div>
  if (!graph || !graph.nodes.length) return <EmptyState icon={<Network size={40} />} title={i18nT('pages.knowledge.knowledgeGraph.no_graph_data_yet')} subtitle={i18nT('pages.knowledge.knowledgeGraph.ingest_documents_to_build_the_entity_graph')} />

  return (
    <div className="border border-border rounded-lg overflow-hidden flex flex-col flex-1 min-h-0">
      <div className="px-4 py-2 border-b border-border flex items-center gap-3 text-[12px] text-muted shrink-0">
        <span>{graph.nodes.length} {i18nT('pages.knowledge.knowledgeGraph.nodes')} {graph.edges.length} {i18nT('pages.knowledge.knowledgeGraph.edges')}</span>
        <button onClick={() => zoomRef.current?.reset()} className="px-2 py-0.5 text-[11px] border border-border rounded hover:bg-bg-elevated bg-transparent cursor-pointer text-muted flex items-center gap-1">
          <RotateCcw size={10} /> {i18nT('pages.knowledge.knowledgeGraph.recenter')}
        </button>
        <span className="ml-auto flex gap-2">
          {Object.entries(TYPE_COLORS).map(([t, c]) => <span key={t} className="flex items-center gap-1"><span className="w-2 h-2 rounded-full inline-block" style={{ background: c }} />{t}</span>)}
        </span>
      </div>
      <svg ref={svgRef} className="w-full flex-1 min-h-0 bg-bg-elevated" style={{ minHeight: '300px' }} />
    </div>
  )
}
