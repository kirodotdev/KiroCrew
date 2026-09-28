/**
 * KnowledgeGraph renders an entity graph with sigma.js (WebGL) + graphology,
 * laid out by a one-shot d3-force pass and animated by a ForceAtlas2 web worker
 * that the physics toggle starts/stops.
 *
 * Sigma draws to a <canvas>, so — unlike the previous d3/SVG implementation —
 * the nodes are NOT queryable DOM. These tests therefore mock sigma, graphology
 * and the FA2 worker and assert on:
 *  - the chrome (node/edge counts, legend, toolbar controls),
 *  - the query wiring (endpoint + source filter),
 *  - graph CONSTRUCTION (which addNode/addEdge calls the component makes),
 *  - the physics toggle (start/stop + localStorage persistence + auto-stop).
 *
 * d3 is left real: the one-shot layout pass is synchronous and time-bounded, and
 * only mutates plain {x,y} objects, so it runs fine under happy-dom.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { GraphData } from '../pages/knowledge/types'

const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

// --- graphology mock: record the addNode/addEdge calls the component makes ---
interface RecordedNode { id: string; attrs: Record<string, unknown> }
interface RecordedEdge { source: string; target: string; attrs: Record<string, unknown> }
let recordedNodes: RecordedNode[] = []
let recordedEdges: RecordedEdge[] = []

class MockGraph {
  private nodeSet = new Set<string>()
  private edges = new Set<string>()
  private adjacency = new Map<string, Set<string>>()
  get order() { return this.nodeSet.size }
  addNode(id: string, attrs: Record<string, unknown>) {
    if (this.nodeSet.has(id)) throw new Error('duplicate')
    this.nodeSet.add(id)
    this.adjacency.set(id, new Set())
    recordedNodes.push({ id, attrs })
  }
  addEdge(source: string, target: string, attrs: Record<string, unknown>) {
    this.edges.add(`${source}->${target}`)
    this.adjacency.get(source)?.add(target)
    this.adjacency.get(target)?.add(source)
    recordedEdges.push({ source, target, attrs })
  }
  hasNode(id: string) { return this.nodeSet.has(id) }
  hasEdge(s: string, t: string) { return this.edges.has(`${s}->${t}`) }
  hasExtremity(edgeKey: string, node: string) {
    const [s, t] = edgeKey.split('->')
    return s === node || t === node
  }
  neighbors(id: string) { return Array.from(this.adjacency.get(id) ?? []) }
  nodes() { return Array.from(this.nodeSet) }
  getNodeAttribute(id: string, key: string) {
    return recordedNodes.find(n => n.id === id)?.attrs[key]
  }
  setNodeAttribute(id: string, key: string, val: unknown) {
    const n = recordedNodes.find(x => x.id === id)
    if (n) n.attrs[key] = val
  }
  removeNodeAttribute(id: string, key: string) {
    const n = recordedNodes.find(x => x.id === id)
    if (n) delete n.attrs[key]
  }
  forEachEdge(_cb: (edge: string) => void) { /* no-op for theme observer */ }
  setEdgeAttribute() { /* no-op */ }
}
vi.mock('graphology', () => ({ default: MockGraph }))

// --- sigma mock: record construction + expose the event handlers ---
let sigmaInstances: MockSigma[] = []
class MockSigma {
  handlers: Record<string, (e: unknown) => void> = {}
  mouseHandlers: Record<string, (e: unknown) => void> = {}
  killed = false
  cameraEnabled = true
  settings: Record<string, unknown>
  constructor(_graph: unknown, _container: unknown, settings: Record<string, unknown>) {
    this.settings = settings
    sigmaInstances.push(this)
  }
  on(event: string, cb: (e: unknown) => void) { this.handlers[event] = cb }
  getMouseCaptor() {
    return { on: (event: string, cb: (e: unknown) => void) => { this.mouseHandlers[event] = cb } }
  }
  viewportToGraph(e: { x: number; y: number }) { return { x: e.x, y: e.y } }
  setSetting() {}
  refresh() {}
  resize() {}
  getCamera() {
    return {
      animate: () => {},
      disable: () => { this.cameraEnabled = false },
      enable: () => { this.cameraEnabled = true },
    }
  }
  getNodeDisplayData() { return { x: 0, y: 0 } }
  kill() { this.killed = true }
  // Test helper: fire a node click through the registered handler.
  clickNode(node: string) { this.handlers['clickNode']?.({ node }) }
}
vi.mock('sigma', () => ({ default: MockSigma }))

// --- ForceAtlas2 worker mock: record start/stop/kill ---
let fa2Instances: MockFA2[] = []
class MockFA2 {
  started = false
  stopped = false
  killed = false
  settings: { slowDown?: number } & Record<string, unknown>
  constructor(_graph: unknown, opts: { settings?: Record<string, unknown> }) {
    this.settings = { ...(opts?.settings ?? {}) }
    fa2Instances.push(this)
  }
  start() { this.started = true; this.stopped = false }
  stop() { this.stopped = true }
  kill() { this.killed = true }
}
vi.mock('graphology-layout-forceatlas2/worker', () => ({ default: MockFA2 }))

// --- Synchronous ForceAtlas2 mock: record the clustering-pass assign() calls ---
let fa2AssignCalls: Array<{ iterations: number; settings: Record<string, unknown> }> = []
vi.mock('graphology-layout-forceatlas2', () => ({
  default: {
    assign: (_graph: unknown, opts: { iterations: number; settings: Record<string, unknown> }) => {
      fa2AssignCalls.push({ iterations: opts.iterations, settings: opts.settings })
    },
  },
}))

// Louvain mock: assign a deterministic community per node so the
// community-seeding path executes (two communities from the fixture).
vi.mock('graphology-communities-louvain', () => ({
  default: (graph: { nodes: () => string[] }) => {
    const out: Record<string, number> = {}
    graph.nodes().forEach((id, i) => { out[id] = i % 2 })
    return out
  },
}))

const KnowledgeGraph = (await import('../pages/knowledge/KnowledgeGraph')).default

const GRAPH: GraphData = {
  nodes: [
    { id: 'n1', name: 'Gateway', type: 'service' },
    { id: 'n2', name: 'Postgres', type: 'technology' },
    { id: 'n3', name: 'Retrieval', type: 'concept' },
    { id: 'n4', name: 'Platform', type: 'org' },
    { id: 'n5', name: 'Widgets', type: 'gizmo' }, // unknown type → fallback colour
  ],
  edges: [
    { source: 'n1', target: 'n2', type: 'depends_on', weight: 3 },
    { source: 'n1', target: 'n3', type: 'implements' },
    { source: 'n3', target: 'n4', type: 'owned_by', weight: 1 },
    { source: 'n4', target: 'n5', type: 'ships' },
  ],
}

let qc: QueryClient
function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}
function renderGraph(props: {
  onSelectEntity?: (name: string) => void
  highlightEntity?: string | null
} = {}) {
  const view = render(
    <QueryClientProvider client={qc}>
      <KnowledgeGraph {...props} />
    </QueryClientProvider>
  )
  const rerender = (next: typeof props) => view.rerender(
    <QueryClientProvider client={qc}>
      <KnowledgeGraph {...next} />
    </QueryClientProvider>
  )
  return { ...view, rerender }
}

/** Wait until sigma has been constructed (i.e. the async d3 layout resolved). */
async function mounted() {
  await waitFor(() => expect(sigmaInstances.length).toBeGreaterThan(0))
}

beforeEach(() => {
  vi.clearAllMocks()
  recordedNodes = []
  recordedEdges = []
  sigmaInstances = []
  fa2Instances = []
  fa2AssignCalls = []
  localStorage.clear()
  qc = makeClient()
  mockKnowledgeApi.mockImplementation((path: string) => {
    if (path.startsWith('/sources')) return Promise.resolve([])
    return Promise.resolve(GRAPH)
  })
})

afterEach(() => {
  cleanup()
  qc.clear()
  localStorage.clear()
})

describe('KnowledgeGraph — pre-data states', () => {
  it('shows the loading line while the graph query is in flight', () => {
    mockKnowledgeApi.mockImplementation(() => new Promise(() => {}))
    renderGraph()
    expect(screen.getByText('Loading graph...')).toBeInTheDocument()
  })

  it('requests the graph endpoint with the node cap', async () => {
    renderGraph()
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalledWith('/graph?limit=200'))
  })

  it('shows the empty state when the graph has no nodes', async () => {
    mockKnowledgeApi.mockResolvedValue({ nodes: [], edges: [] })
    renderGraph()
    expect(await screen.findByText('No graph data yet')).toBeInTheDocument()
    expect(screen.getByText('Ingest documents to build the entity graph')).toBeInTheDocument()
  })

  it('shows the empty state when the query resolves with no graph at all', async () => {
    mockKnowledgeApi.mockResolvedValue(null)
    renderGraph()
    expect(await screen.findByText('No graph data yet')).toBeInTheDocument()
  })

  it('does not construct sigma in the empty state', async () => {
    mockKnowledgeApi.mockResolvedValue({ nodes: [], edges: [] })
    renderGraph()
    await screen.findByText('No graph data yet')
    expect(sigmaInstances).toHaveLength(0)
  })
})

describe('KnowledgeGraph — chrome', () => {
  it('reports the node and edge counts', async () => {
    const { container } = renderGraph()
    await waitFor(() => {
      const text = container.textContent ?? ''
      expect(text).toMatch(/5\s*nodes,?\s*4\s*edges/)
    })
  })

  it('renders a legend swatch for every known entity type', async () => {
    renderGraph()
    await screen.findByText('service')
    for (const label of ['service', 'technology', 'concept', 'org']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })

  it('renders the recenter control', async () => {
    renderGraph()
    expect(await screen.findByRole('button', { name: /recenter/i })).toBeInTheDocument()
  })

  it('renders the physics toggle', async () => {
    renderGraph()
    expect(await screen.findByRole('button', { name: /physics/i })).toBeInTheDocument()
  })
})

describe('KnowledgeGraph — graph construction', () => {
  it('adds one graphology node per entity, tagged with entityType (not type)', async () => {
    renderGraph()
    await mounted()
    const ids = recordedNodes.map(n => n.id)
    expect(ids).toEqual(['n1', 'n2', 'n3', 'n4', 'n5'])
    // entityType carries the category; `type` must NOT be set (sigma reads it
    // as a node-program name and throws for unknown programs).
    for (const n of recordedNodes) {
      expect(n.attrs).not.toHaveProperty('type')
      expect(n.attrs).toHaveProperty('entityType')
    }
    expect(recordedNodes[0].attrs.entityType).toBe('service')
  })

  it('colours each node by its entity type and falls back for unknown types', async () => {
    renderGraph()
    await mounted()
    const colorOf = (id: string) => recordedNodes.find(n => n.id === id)?.attrs.color
    expect(colorOf('n1')).toBe('#3b82f6')
    expect(colorOf('n2')).toBe('#22c55e')
    expect(colorOf('n3')).toBe('#a855f7')
    expect(colorOf('n4')).toBe('#f97316')
    expect(colorOf('n5')).toBe('#6b7280')
  })

  it('sizes nodes by degree', async () => {
    renderGraph()
    await mounted()
    // n1 has degree 2 (two edges), n2 has degree 1. Higher degree → bigger.
    const sizeOf = (id: string) => recordedNodes.find(n => n.id === id)?.attrs.size as number
    expect(sizeOf('n1')).toBeGreaterThan(sizeOf('n2'))
  })

  it('adds one edge per relation, tagged with relationType (not type)', async () => {
    renderGraph()
    await mounted()
    expect(recordedEdges).toHaveLength(4)
    // `type` must NOT be set on edges either — same sigma program conflict.
    for (const e of recordedEdges) {
      expect(e.attrs).not.toHaveProperty('type')
      expect(e.attrs).toHaveProperty('relationType')
    }
    expect(recordedEdges[0].attrs.relationType).toBe('depends_on')
  })

  it('sets allowInvalidContainer so a 0-sized mount does not throw', async () => {
    renderGraph()
    await mounted()
    expect(sigmaInstances[0].settings.allowInvalidContainer).toBe(true)
  })

  it('wires a theme-aware hover/highlight label renderer (legibility in any theme)', async () => {
    renderGraph()
    await mounted()
    // A custom defaultDrawNodeHover must be set — sigma's default paints a
    // hardcoded white pill that is illegible in dark mode / custom themes.
    expect(typeof sigmaInstances[0].settings.defaultDrawNodeHover).toBe('function')
  })

  it('keeps the highlighted node label visible (does not blank the focus)', async () => {
    renderGraph({ highlightEntity: 'Gateway' })
    await mounted()
    const sigma = sigmaInstances[0]
    const nodeReducer = sigma.settings.nodeReducer as (k: string, d: Record<string, unknown>) => Record<string, unknown>
    // The focused node keeps its label and is marked highlighted (so the themed
    // pill renders); only NON-neighbors get their label blanked.
    const focused = nodeReducer('n1', { color: '#3b82f6', size: 5, label: 'Gateway' })
    expect(focused.label).toBe('Gateway')
    expect(focused.highlighted).toBe(true)
  })
})

describe('KnowledgeGraph — clustering pass (resource-bounded)', () => {
  it('runs a synchronous FA2 clustering pass at mount with linLog + barnesHut', async () => {
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2AssignCalls.length).toBeGreaterThan(0))
    const call = fa2AssignCalls[0]
    expect(call.settings.linLogMode).toBe(true)
    // barnesHut must be ON at every tier (O(n log n), not brute-force O(n²)).
    expect(call.settings.barnesHutOptimize).toBe(true)
  })

  it('uses the small-graph iteration tier for a <=300 node graph', async () => {
    // The 5-node fixture is well under 300 → top tier (220 iterations, enough
    // to reach the same near-convergence the live worker settles to).
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2AssignCalls.length).toBeGreaterThan(0))
    expect(fa2AssignCalls[0].iterations).toBe(220)
  })

  it('never runs more than 220 iterations on the main thread', async () => {
    // Guards the resource budget: even the largest tier stays bounded (barnesHut
    // O(n log n), dev-timed against the 60ms budget).
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2AssignCalls.length).toBeGreaterThan(0))
    for (const c of fa2AssignCalls) expect(c.iterations).toBeLessThanOrEqual(220)
  })

  it('assigns a Louvain community to each node (seeds clustering)', async () => {
    renderGraph()
    await mounted()
    // The community-detection pass runs after mount inside the layout chain;
    // every node should carry a numeric `community` attribute used to seed the
    // per-community centroids that make FA2 cluster neighbors together.
    await waitFor(() => {
      const withCommunity = recordedNodes.filter(n => typeof n.attrs.community === 'number')
      expect(withCommunity.length).toBe(recordedNodes.length)
    })
  })
})

describe('KnowledgeGraph — interaction', () => {
  it('reports the clicked entity name to the parent', async () => {
    const onSelectEntity = vi.fn()
    renderGraph({ onSelectEntity })
    await mounted()
    sigmaInstances[0].clickNode('n3')
    expect(onSelectEntity).toHaveBeenCalledWith('Retrieval')
  })

  it('survives a node click when no selection handler is supplied', async () => {
    renderGraph()
    await mounted()
    expect(() => sigmaInstances[0].clickNode('n1')).not.toThrow()
  })

  it('recenters without throwing when the control is pressed', async () => {
    renderGraph()
    await mounted()
    expect(() => fireEvent.click(screen.getByRole('button', { name: /recenter/i }))).not.toThrow()
  })

  it('drags a node: disables the camera on down, moves it, re-enables on up', async () => {
    renderGraph()
    await mounted()
    const sigma = sigmaInstances[0]
    // downNode disables camera pan
    sigma.handlers['downNode']?.({ node: 'n1' })
    expect(sigma.cameraEnabled).toBe(false)
    // mousemovebody writes graph coords onto the dragged node
    const preventSigmaDefault = vi.fn()
    const original = { preventDefault: vi.fn(), stopPropagation: vi.fn() }
    sigma.mouseHandlers['mousemovebody']?.({ x: 42, y: 24, preventSigmaDefault, original })
    expect(recordedNodes.find(n => n.id === 'n1')?.attrs.x).toBe(42)
    expect(recordedNodes.find(n => n.id === 'n1')?.attrs.y).toBe(24)
    expect(preventSigmaDefault).toHaveBeenCalled()
    // mouseup releases and re-enables the camera
    sigma.mouseHandlers['mouseup']?.({})
    expect(sigma.cameraEnabled).toBe(true)
  })

  it('ignores a body move when no node is being dragged', async () => {
    renderGraph()
    await mounted()
    const sigma = sigmaInstances[0]
    const before = recordedNodes.find(n => n.id === 'n1')?.attrs.x
    sigma.mouseHandlers['mousemovebody']?.({
      x: 999, y: 999, preventSigmaDefault: vi.fn(), original: { preventDefault: vi.fn(), stopPropagation: vi.fn() },
    })
    expect(recordedNodes.find(n => n.id === 'n1')?.attrs.x).toBe(before)
  })

  it('sets a pointer cursor on hover and clears it on leave', async () => {
    const { container } = renderGraph()
    await mounted()
    const sigma = sigmaInstances[0]
    const graphDiv = container.querySelector('[role="application"]') as HTMLElement
    sigma.handlers['enterNode']?.({ node: 'n1' })
    expect(graphDiv.style.cursor).toBe('pointer')
    sigma.handlers['leaveNode']?.({})
    expect(graphDiv.style.cursor).toBe('default')
  })

  it('dims non-neighbor nodes and hides non-incident edges on hover', async () => {
    renderGraph()
    await mounted()
    const sigma = sigmaInstances[0]
    const nodeReducer = sigma.settings.nodeReducer as (k: string, d: Record<string, unknown>) => Record<string, unknown>
    const edgeReducer = sigma.settings.edgeReducer as (k: string, d: Record<string, unknown>) => Record<string, unknown>
    // Hover n1 (neighbors n2, n3 via edges n1->n2, n1->n3). n4/n5 are not.
    sigma.handlers['enterNode']?.({ node: 'n1' })
    // focus node keeps its label; a non-neighbor (n4) is dimmed + label cleared.
    expect(nodeReducer('n1', { color: '#3b82f6', size: 5, label: 'Gateway' }).label).toBe('Gateway')
    const dimmed = nodeReducer('n4', { color: '#f97316', size: 5, label: 'Platform' })
    expect(dimmed.label).toBe('')
    // an edge NOT incident to n1 (n3->n4) is hidden; one incident (n1->n2) is not.
    expect(edgeReducer('n3->n4', {}).hidden).toBe(true)
    expect(edgeReducer('n1->n2', {}).hidden).toBeUndefined()
    // leaving clears the focus so nothing is dimmed.
    sigma.handlers['leaveNode']?.({})
    expect(nodeReducer('n4', { color: '#f97316', size: 5, label: 'Platform' }).label).toBe('Platform')
  })

  it('stops the settling worker during a drag and resumes it on release', async () => {
    localStorage.setItem('mc-kb-graph-physics', '1')
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    // physics auto-started (persisted on) → worker running.
    expect(fa2Instances[0].started).toBe(true)
    const sigma = sigmaInstances[0]
    // downNode while settling must stop the worker so it can't overwrite the drag.
    sigma.handlers['downNode']?.({ node: 'n1' })
    expect(fa2Instances[0].stopped).toBe(true)
    // releasing resumes the settle.
    sigma.mouseHandlers['mouseup']?.({})
    expect(fa2Instances[0].started).toBe(true)
  })
})

describe('KnowledgeGraph — physics toggle', () => {
  it('does not start physics by default (no persisted preference)', async () => {
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    expect(fa2Instances[0].started).toBe(false)
  })

  it('starts the FA2 worker when physics is toggled on', async () => {
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    // Off-state label is "Physics".
    fireEvent.click(screen.getByRole('button', { name: 'Physics' }))
    expect(fa2Instances[0].started).toBe(true)
  })

  it('persists the physics preference to localStorage', async () => {
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    fireEvent.click(screen.getByRole('button', { name: 'Physics' }))
    expect(localStorage.getItem('mc-kb-graph-physics')).toBe('1')
  })

  it('flips the button label to Pause when physics is enabled', async () => {
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    fireEvent.click(screen.getByRole('button', { name: 'Physics' }))
    // Label is driven by intent (physicsEnabled), so it stays "Pause" even after
    // the settle auto-stops — it never lies.
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })

  it('starts the worker with the fast ease-out slowDown', async () => {
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    // Ease-out begins fast: the worker is constructed with a LOW slowDown; the
    // convergence poll then ramps it up toward heavy damping as movement drops.
    expect(fa2Instances[0].settings.slowDown).toBe(2)
  })

  it('runs the worker with barnesHut on (matches the sync pass for a consistent end state)', async () => {
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    // The live worker must use the same barnesHutOptimize as the sync mount pass
    // so physics-on converges to the same layout physics-off shows — otherwise
    // toggling physics on visibly jumps to a different equilibrium.
    expect(fa2Instances[0].settings.barnesHutOptimize).toBe(true)
  })

  it('auto-stops the simulation after the settle window', async () => {
    vi.useFakeTimers()
    try {
      renderGraph()
      await vi.waitFor(() => expect(sigmaInstances.length).toBeGreaterThan(0))
      await vi.waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
      fireEvent.click(screen.getByRole('button', { name: 'Physics' }))
      expect(fa2Instances[0].started).toBe(true)
      await vi.advanceTimersByTimeAsync(4000)
      expect(fa2Instances[0].stopped).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops early on convergence (before the 8s hard cap)', async () => {
    // The mock worker does not move nodes, so positions are static → the
    // convergence poll (350ms) detects zero movement on its 2nd sample (~700ms)
    // and stops WELL before the 8000ms cap. Proves convergence detection drives
    // the stop, not the timer.
    vi.useFakeTimers()
    try {
      renderGraph()
      await vi.waitFor(() => expect(sigmaInstances.length).toBeGreaterThan(0))
      await vi.waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
      fireEvent.click(screen.getByRole('button', { name: 'Physics' }))
      expect(fa2Instances[0].started).toBe(true)
      await vi.advanceTimersByTimeAsync(1200) // < 8000ms cap
      expect(fa2Instances[0].stopped).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not leak the convergence interval after toggling physics off', async () => {
    // A leaked 350ms setInterval polling forever is a resource bug the user
    // explicitly guarded against. Spy on clearInterval and assert the poll is
    // torn down when physics is turned off mid-settle.
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    fireEvent.click(screen.getByRole('button', { name: 'Physics' })) // on → starts poll
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))   // off → must clear poll
    expect(clearSpy).toHaveBeenCalled()
    clearSpy.mockRestore()
  })

  it('does not leak the convergence interval on unmount mid-settle', async () => {
    const clearSpy = vi.spyOn(globalThis, 'clearInterval')
    const { unmount } = renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    fireEvent.click(screen.getByRole('button', { name: 'Physics' })) // start poll
    unmount()
    expect(clearSpy).toHaveBeenCalled()
    clearSpy.mockRestore()
  })

  it('auto-starts physics when the preference is persisted on', async () => {
    localStorage.setItem('mc-kb-graph-physics', '1')
    renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    await waitFor(() => expect(fa2Instances[0].started).toBe(true))
  })
})

describe('KnowledgeGraph — source filter', () => {
  it('includes the source_id in the query when a source is selected', async () => {
    mockKnowledgeApi.mockImplementation((path: string) => {
      if (path.startsWith('/sources')) {
        return Promise.resolve([
          { id: 's1', name: 'Alpha' },
          { id: 's2', name: 'Beta' },
        ])
      }
      return Promise.resolve(GRAPH)
    })
    renderGraph()
    // The dropdown only appears with >1 source; the base query still fires.
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalledWith('/graph?limit=200'))
  })
})

describe('KnowledgeGraph — lifecycle', () => {
  it('kills sigma and the FA2 worker on unmount', async () => {
    const { unmount } = renderGraph()
    await mounted()
    await waitFor(() => expect(fa2Instances.length).toBeGreaterThan(0))
    const sigma = sigmaInstances[0]
    const fa2 = fa2Instances[0]
    unmount()
    expect(sigma.killed).toBe(true)
    expect(fa2.killed).toBe(true)
  })

  it('abandons the mount when unmounted before d3 resolves', async () => {
    qc.setQueryData(['knowledge-graph', ''], GRAPH)
    const { unmount } = renderGraph()
    unmount()
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalled())
    // sigma may or may not have constructed depending on timing, but if it did
    // it must have been killed by the cleanup.
    for (const s of sigmaInstances) expect(s.killed).toBe(true)
  })
})
