import { useState, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Network as NetworkIcon, RefreshCw } from 'lucide-react'
import Graph from 'graphology'
import Sigma from 'sigma'
import type { SimulationNodeDatum, SimulationLinkDatum } from 'd3'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Badge } from '../../components/ui'
import InfoTip from '../../components/InfoTip'

// Hex per group, fed straight to sigma's WebGL node program.
const GROUP_COLORS: Record<string, string> = {
  preference: '#3b82f6',
  project:    '#22c55e',
  semantic:   '#a855f7',
  lesson:     '#f97316',
  history:    '#6b7280',
}
const DIM_COLOR = 'rgba(120,120,120,0.12)'

const StatusDot = ({ color }: { color: string }) => <span className="inline-block w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
const GROUP_LABELS: Record<string, ReactNode> = {
  preference: <><StatusDot color="#3b82f6" /> Preferences</>,
  project: <><StatusDot color="#22c55e" /> Projects</>,
  semantic: <><StatusDot color="#a855f7" /> Semantic</>,
  lesson: <><StatusDot color="#f97316" /> Lessons</>,
  history: <><StatusDot color="#9ca3af" /> History</>,
}

interface GraphNode { id: string; label: string; group: string; title: string }
interface GraphEdge { from: string; to: string }

export default function MemoryGraphTab() {
  const containerRef = useRef<HTMLDivElement>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  const graphRef = useRef<Graph | null>(null)
  const [selected, setSelected] = useState<GraphNode | null>(null)
  const [filter, setFilter] = useState<string | null>(null)
  const [searchImmediate, setSearchImmediate] = useState('')
  const [search, setSearch] = useState('')
  // Latest filter/search for the reducer closure (avoids rebuilding sigma).
  const filterRef = useRef<string | null>(null)
  const searchRef = useRef('')

  const { data, isLoading: loading, refetch: load } = useQuery({
    queryKey: ['memory-graph'],
    queryFn: async () => {
      const r = await api.memoryGraph().catch(() => ({ nodes: [], edges: [] }))
      return r as { nodes: GraphNode[]; edges: GraphEdge[] }
    },
  })
  // Memoize the derived arrays so the `?? []` fallback doesn't hand the sigma
  // -building effect a fresh reference on every render (which would tear down
  // and rebuild the sigma instance needlessly). Keyed on the react-query
  // `data`, which is itself reference-stable between renders until a refetch.
  const nodes = useMemo(() => data?.nodes ?? [], [data])
  const edges = useMemo(() => data?.edges ?? [], [data])

  useEffect(() => {
    const t = setTimeout(() => setSearch(searchImmediate), 300)
    return () => clearTimeout(t)
  }, [searchImmediate])

  // Build the graph + sigma instance once per data load.
  //
  // Layout: a ONE-SHOT d3-force pass (settle within a time budget, then stop)
  // — the same "compute a diagram once, never run a live solver" model the
  // Knowledge Graph tab uses. d3's forceManyBody uses a Barnes-Hut quadtree
  // (O(n log n)) and the pass is time-bounded + one-time, so it does NOT
  // reintroduce the old freeze (vis-network's O(n²) forceAtlas2 that ran every
  // frame on the main thread). Real edges (from the fixed backend edge rule)
  // give connected memory clusters; disconnected nodes are kept in view by a
  // gentle forceX/forceY gravity. A golden-angle (sunflower) disc seeds initial
  // positions and is the fallback if d3 fails to load. The client fully owns
  // layout — the server sends only nodes/edges, no coordinates.
  //
  // sigma renders via WebGL (gl.compileShader — GPU-side, no JS eval, so it is
  // CSP-safe, unlike regl-based libraries) and handles thousands of nodes.
  useEffect(() => {
    const container = containerRef.current
    if (!container || nodes.length === 0) return
    if (sigmaRef.current) { sigmaRef.current.kill(); sigmaRef.current = null }
    let sigma: Sigma | undefined
    let aborted = false
    let themeObserver: MutationObserver | undefined

    // Read theme-aware colors from the design-system CSS vars on <html> so the
    // graph is legible in BOTH light and dark modes (the Knowledge Graph tab
    // reads the same vars). Hardcoding colors made text/edges/the hover pill
    // wrong in one mode — sigma's default hover label paints a hardcoded WHITE
    // pill, which is jarring and low-contrast against light labels in dark mode.
    const readColors = () => {
      const cs = getComputedStyle(document.documentElement)
      return {
        label: cs.getPropertyValue('--text').trim() || '#e5e7eb',
        edge: cs.getPropertyValue('--muted').trim() || '#9ca3af',
        hoverBg: cs.getPropertyValue('--bg-elevated').trim() || '#1f2430',
        border: cs.getPropertyValue('--border').trim() || 'rgba(255,255,255,0.12)',
      }
    }
    // Mutable so the theme observer can refresh it in place; the hover renderer
    // closure reads it live on the next hover paint.
    let colors = readColors()

    // Themed replacement for sigma's default node-hover renderer (which fills a
    // hardcoded white pill). Rounded pill in the panel color + subtle border,
    // the group-colored node dot, and the themed label text.
    const drawThemedHover = (
      context: CanvasRenderingContext2D,
      data: { x: number; y: number; size: number; label: string | null; color: string },
      settings: { labelSize: number; labelFont: string; labelWeight: string },
    ) => {
      const label = data.label
      if (!label) return
      const size = settings.labelSize
      context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`
      const PAD = 5
      const textWidth = context.measureText(label).width
      const boxH = size + PAD * 2
      const boxX = data.x - data.size - PAD
      const boxY = data.y - boxH / 2
      const boxW = data.size + PAD + textWidth + PAD * 2
      const r = boxH / 2
      context.beginPath()
      context.moveTo(boxX + r, boxY)
      context.arcTo(boxX + boxW, boxY, boxX + boxW, boxY + boxH, r)
      context.arcTo(boxX + boxW, boxY + boxH, boxX, boxY + boxH, r)
      context.arcTo(boxX, boxY + boxH, boxX, boxY, r)
      context.arcTo(boxX, boxY, boxX + boxW, boxY, r)
      context.closePath()
      context.fillStyle = colors.hoverBg
      context.fill()
      context.lineWidth = 1
      context.strokeStyle = colors.border
      context.stroke()
      context.beginPath()
      context.arc(data.x, data.y, data.size, 0, Math.PI * 2)
      context.fillStyle = data.color
      context.fill()
      context.fillStyle = colors.label
      context.fillText(label, data.x + data.size + PAD, data.y + size / 3)
    }

    // Golden-angle disc: deterministic seed for the force pass + fallback.
    const GOLDEN = Math.PI * (3 - Math.sqrt(5)) // ~2.39996 rad
    const spread = 12
    const seed = new Map<string, { x: number; y: number }>()
    nodes.forEach((n, i) => {
      const radius = spread * Math.sqrt(i + 0.5)
      seed.set(n.id, { x: Math.cos(i * GOLDEN) * radius, y: Math.sin(i * GOLDEN) * radius })
    })

    const mountSigma = (coords: Map<string, { x: number; y: number }>) => {
      if (aborted) return
      const graph = new Graph()
      nodes.forEach(n => {
        const p = coords.get(n.id) ?? seed.get(n.id) ?? { x: 0, y: 0 }
        try {
          graph.addNode(n.id, {
            x: p.x, y: p.y, size: 3,
            color: GROUP_COLORS[n.group] || GROUP_COLORS.history,
            label: n.label, group: n.group,
          })
        } catch { /* duplicate id — skip */ }
      })
      for (const e of edges) {
        if (graph.hasNode(e.from) && graph.hasNode(e.to) && !graph.hasEdge(e.from, e.to)) {
          try { graph.addEdge(e.from, e.to, { color: colors.edge, size: 0.5 }) } catch { /* noop */ }
        }
      }
      graphRef.current = graph
      try {
        sigma = new Sigma(graph, container, {
          renderLabels: true,
          // Labels only appear once a node is large enough on screen (i.e.
          // zoomed in), so the default view isn't a wall of overlapping text.
          labelRenderedSizeThreshold: 12,
          labelColor: { color: colors.label },
          labelSize: 11,
          // Themed hover pill (sigma's default hover paints a white background).
          defaultDrawNodeHover: drawThemedHover,
          // Keep edges painted while panning/zooming — hiding them made the
          // whole edge set vanish and redraw on every interaction. Labels stay
          // gated on move (they are heavier and zoom-thresholded anyway).
          hideEdgesOnMove: false,
          hideLabelsOnMove: true,
          // Dim/recolor without rebuilding the graph: the reducer reads the live
          // filter/search refs on every render.
          nodeReducer: (_nodeKey, data) => {
            const res = { ...data }
            const flt = filterRef.current
            const srch = searchRef.current
            const dimmed = (!!flt && data.group !== flt) ||
              (!!srch && !String(data.label).toLowerCase().includes(srch))
            if (dimmed) { res.color = DIM_COLOR; res.label = '' }
            return res
          },
        })
        sigma.on('clickNode', ({ node }) => {
          const n = nodes.find(x => x.id === node)
          setSelected(n || null)
        })
        sigma.on('clickStage', () => setSelected(null))
        sigmaRef.current = sigma
        // Recolor live when the user switches light/dark (the app flips
        // <html data-theme>, so the CSS vars we read above change value).
        themeObserver = new MutationObserver(() => {
          colors = readColors()
          const g = graphRef.current
          if (g) g.forEachEdge(edge => g.setEdgeAttribute(edge, 'color', colors.edge))
          sigmaRef.current?.setSetting('labelColor', { color: colors.label })
          sigmaRef.current?.refresh()
        })
        themeObserver.observe(document.documentElement, {
          attributes: true, attributeFilter: ['data-theme'],
        })
      } catch (err) {
        // eslint-disable-next-line no-console -- intentional init-failure diagnostic
        console.warn('MemoryGraph: sigma init failed', err)
        if (sigma) { try { sigma.kill() } catch { /* noop */ } }
        sigma = undefined
      }
    }

    // One-shot force layout (code-split d3, like the Knowledge Graph tab).
    import('d3').then(d3 => {
      if (aborted) return
      type LNode = SimulationNodeDatum & { id: string }
      type LEdge = SimulationLinkDatum<LNode>
      const simNodes: LNode[] = nodes.map(n => {
        const p = seed.get(n.id)!
        return { id: n.id, x: p.x, y: p.y }
      })
      const simEdges: LEdge[] = edges.map(e => ({ source: e.from, target: e.to }))
      const sim = d3.forceSimulation<LNode, LEdge>(simNodes)
        .force('link', d3.forceLink<LNode, LEdge>(simEdges).id(d => d.id).distance(40).strength(0.6))
        // distanceMax bounds repulsion range: tighter clusters + cheaper ticks.
        .force('charge', d3.forceManyBody<LNode>().strength(-34).distanceMax(400))
        .force('collide', d3.forceCollide<LNode>(7))
        // Gentle gravity keeps disconnected nodes on-screen instead of drifting.
        .force('x', d3.forceX<LNode>(0).strength(0.03))
        .force('y', d3.forceY<LNode>(0).strength(0.03))
        .alphaDecay(0.08)
        .stop()
      // Bound the pass by WALL-CLOCK time, not node count: the one-time layout
      // can never block the main thread for more than ~BUDGET_MS no matter how
      // large the memory store grows (a structural no-freeze guarantee, not one
      // that holds only at today's store sizes). MAX_TICKS caps cost/quality on
      // small graphs where the budget would never be hit.
      const MAX_TICKS = 300
      const BUDGET_MS = 250
      const nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
      const deadline = nowMs() + BUDGET_MS
      for (let i = 0; i < MAX_TICKS && nowMs() < deadline; i++) sim.tick()
      const coords = new Map<string, { x: number; y: number }>(
        simNodes.map(n => [n.id, { x: n.x ?? 0, y: n.y ?? 0 }]),
      )
      mountSigma(coords)
    }).catch(() => mountSigma(seed)) // d3 unavailable → static disc fallback

    return () => {
      aborted = true
      themeObserver?.disconnect()
      if (sigma) { try { sigma.kill() } catch { /* noop */ } }
      sigmaRef.current = null
      graphRef.current = null
    }
  }, [nodes, edges])

  // Filter/search just refresh the existing sigma (re-runs the reducer); no
  // graph rebuild, no per-node mutation loop.
  useEffect(() => {
    filterRef.current = filter
    searchRef.current = search.toLowerCase()
    sigmaRef.current?.refresh()
  }, [filter, search, nodes])

  const counts = nodes.reduce<Record<string, number>>((acc, n) => {
    acc[n.group] = (acc[n.group] || 0) + 1
    return acc
  }, {})

  if (loading) return <Card><CardTitle><NetworkIcon className="lucide-inline" /> Memory Graph</CardTitle><p className="text-muted text-sm">Loading graph data…</p></Card>
  if (nodes.length === 0) return <Card><CardTitle><NetworkIcon className="lucide-inline" /> Memory Graph</CardTitle><p className="text-muted text-sm">No memory data to visualize. Add preferences, projects, or lessons first.</p></Card>

  return (<>
    <Card>
      <CardTitle><NetworkIcon className="lucide-inline" /> Memory Graph <InfoTip text="GPU-rendered visualization of all KiroCrew memory. Nodes are color-coded by type. Zoom in to reveal labels, click a node to inspect, use filters to focus." />
        <Btn onClick={() => load()} className="ml-2"><RefreshCw className="lucide-inline" /> Refresh</Btn>
      </CardTitle>
      <div className="flex gap-2 flex-wrap mb-3 items-center">
        <input
          aria-label="Search memory nodes"
          className="bg-bg-elevated border border-border rounded-md px-3 py-1.5 text-text text-sm font-body outline-none transition-colors focus-ring flex-1 min-w-[200px]"
          placeholder="Search nodes…" value={searchImmediate} onChange={e => setSearchImmediate(e.target.value)}
        />
        <Btn onClick={() => setFilter(null)} className={!filter ? '!border-accent !text-accent' : ''}>All ({nodes.length})</Btn>
        {Object.entries(GROUP_LABELS).map(([key, label]) => counts[key] ? (
          <Btn key={key} onClick={() => setFilter(filter === key ? null : key)} className={filter === key ? '!border-accent !text-accent' : ''}>{label} ({counts[key]})</Btn>
        ) : null)}
      </div>
      <div ref={containerRef} className="w-full border border-border rounded-md bg-bg-elevated" style={{ height: '500px' }} />
      {selected && (
        <div className="mt-3 p-3 bg-bg-elevated border border-border rounded-md">
          <div className="flex items-center gap-2 mb-1">
            <Badge variant={selected.group === 'lesson' ? 'warn' : selected.group === 'semantic' ? 'aim' : 'ok'}>{selected.group}</Badge>
            <span className="text-sm font-medium text-text-strong">{selected.label}</span>
          </div>
          <p className="text-sm text-muted break-words whitespace-pre-wrap">{selected.title}</p>
        </div>
      )}
    </Card>
  </>)
}
