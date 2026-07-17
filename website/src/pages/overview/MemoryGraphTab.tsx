import { useState, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Network as NetworkIcon, RefreshCw } from 'lucide-react'
import Graph from 'graphology'
import Sigma from 'sigma'
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

interface GraphNode { id: string; label: string; group: string; title: string; x?: number; y?: number }
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

  // Build the graph + sigma instance once per data load. We compute a
  // deterministic golden-angle (sunflower) disc layout in O(n) instead of
  // running a force simulation: ~99% of memory nodes are disconnected, so a
  // force sim adds no structure and its O(n²) cost was the original freeze.
  // sigma renders via WebGL (gl.compileShader — GPU-side, no JS eval, so it
  // is CSP-safe, unlike regl-based libraries).
  useEffect(() => {
    if (!containerRef.current || nodes.length === 0) return
    if (sigmaRef.current) { sigmaRef.current.kill(); sigmaRef.current = null }

    const graph = new Graph()
    const GOLDEN = Math.PI * (3 - Math.sqrt(5)) // ~2.39996 rad
    const spread = 12
    nodes.forEach((n, i) => {
      const radius = spread * Math.sqrt(i + 0.5)
      const angle = i * GOLDEN
      try {
        graph.addNode(n.id, {
          // Prefer server-provided deterministic coords (_assign_layout_coords);
          // fall back to client-side golden-angle disc if absent.
          x: n.x ?? Math.cos(angle) * radius,
          y: n.y ?? Math.sin(angle) * radius,
          size: 3,
          color: GROUP_COLORS[n.group] || GROUP_COLORS.history,
          label: n.label,
          group: n.group,
        })
      } catch { /* duplicate id — skip */ }
    })
    for (const e of edges) {
      if (graph.hasNode(e.from) && graph.hasNode(e.to) && !graph.hasEdge(e.from, e.to)) {
        try { graph.addEdge(e.from, e.to, { color: 'rgba(120,120,120,0.4)', size: 0.5 }) } catch { /* noop */ }
      }
    }
    graphRef.current = graph

    let sigma: Sigma | undefined
    try {
      sigma = new Sigma(graph, containerRef.current, {
        renderLabels: true,
        // Labels only appear once a node is large enough on screen (i.e. zoomed
        // in), so the default zoomed-out view isn't a wall of overlapping text.
        labelRenderedSizeThreshold: 12,
        labelColor: { color: '#cbd5e1' },
        labelSize: 11,
        // Big paint-cost wins at thousands of nodes: drop edges/labels while
        // the user is panning/zooming, restore them when the camera settles.
        hideEdgesOnMove: true,
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
    } catch (err) {
      // eslint-disable-next-line no-console -- intentional init-failure diagnostic
      console.warn('MemoryGraph: sigma init failed', err)
      if (sigma) { try { sigma.kill() } catch { /* noop */ } }
      sigma = undefined
    }
    return () => {
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
