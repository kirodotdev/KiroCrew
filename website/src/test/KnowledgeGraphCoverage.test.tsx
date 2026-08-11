/**
 * KnowledgeGraph renders an entity graph with real d3: the force layout is
 * pre-ticked 300 times synchronously and then drawn, so the whole draw path is
 * reachable under happy-dom without waiting on animation frames.
 *
 * Two deliberate harness choices:
 *  - d3 is NOT mocked. The component's draw code passes accessor callbacks to
 *    d3 (`.attr('x1', d => ...)`), and only the real selection API invokes them.
 *  - The React Query cache is seeded in some tests so the first committed render
 *    already has data. That makes the svg exist on mount, which is the only way
 *    the highlight effect can observe an un-initialised zoom handle and take its
 *    polling branch.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { GraphData } from '../pages/knowledge/types'

const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

const KnowledgeGraph = (await import('../pages/knowledge/KnowledgeGraph')).default

// The shared setup file installs an inert ResizeObserver stub that never fires.
// Swap in a capturing one so the resize-refit effect body can be exercised.
let resizeCallbacks: ResizeObserverCallback[] = []
const realResizeObserver = globalThis.ResizeObserver
class CapturingResizeObserver {
  constructor(cb: ResizeObserverCallback) { resizeCallbacks.push(cb) }
  observe() {}
  unobserve() {}
  disconnect() {}
}

// happy-dom exposes SVGSVGElement.createSVGPoint but the point it returns has no
// matrixTransform, so d3's pointer() throws on its preferred path. Removing the
// method makes d3 take its documented getBoundingClientRect fallback, which in
// turn needs clientLeft/clientTop — also absent on happy-dom's SVG elements, and
// omitting them turns every pointer coordinate into NaN.
// Vitest isolates the environment per test file, so neither patch leaks.
Reflect.deleteProperty(SVGSVGElement.prototype, 'createSVGPoint')
for (const prop of ['clientLeft', 'clientTop'] as const) {
  if (!(prop in SVGElement.prototype)) {
    Object.defineProperty(SVGElement.prototype, prop, { get: () => 0, configurable: true })
  }
}

/** Read the numeric pair out of a `translate(x,y)` transform attribute. */
function translateOf(el: Element): [number, number] {
  const m = /translate\(([^,]+),([^)]+)\)/.exec(el.getAttribute('transform') ?? '')
  if (!m) throw new Error(`no translate on ${el.getAttribute('data-entity')}`)
  return [Number(m[1]), Number(m[2])]
}

const GRAPH: GraphData = {
  nodes: [
    { id: 'n1', name: 'Gateway', type: 'service' },
    { id: 'n2', name: 'Postgres', type: 'technology' },
    { id: 'n3', name: 'Retrieval', type: 'concept' },
    { id: 'n4', name: 'Platform', type: 'org' },
    // Unknown type exercises the TYPE_COLORS fallback colour.
    { id: 'n5', name: 'Widgets', type: 'gizmo' },
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

/** Wait until d3 has finished the async draw for every node. */
async function drawn(container: HTMLElement, count = GRAPH.nodes.length) {
  await waitFor(() => {
    expect(container.querySelectorAll('g[data-entity]')).toHaveLength(count)
  })
}

function circleOf(container: HTMLElement, name: string): SVGCircleElement {
  const circle = container.querySelector(`g[data-entity="${name}"] circle`)
  if (!circle) throw new Error(`no circle for ${name}`)
  return circle as SVGCircleElement
}

beforeEach(() => {
  vi.clearAllMocks()
  resizeCallbacks = []
  globalThis.ResizeObserver = CapturingResizeObserver as unknown as typeof ResizeObserver
  qc = makeClient()
  mockKnowledgeApi.mockResolvedValue(GRAPH)
})

afterEach(() => {
  cleanup()
  globalThis.ResizeObserver = realResizeObserver
  qc.clear()
})

describe('KnowledgeGraph — pre-data states', () => {
  it('shows the loading line while the graph query is in flight', () => {
    mockKnowledgeApi.mockImplementation(() => new Promise(() => {}))
    renderGraph()
    expect(screen.getByText('Loading graph...')).toBeInTheDocument()
  })

  it('requests the graph endpoint with the node cap', async () => {
    renderGraph()
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalled())
    expect(mockKnowledgeApi).toHaveBeenCalledWith('/graph?limit=200')
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

  it('draws no svg in the empty state, so neither layout effect can run', async () => {
    mockKnowledgeApi.mockResolvedValue({ nodes: [], edges: [] })
    const { container } = renderGraph()
    await screen.findByText('No graph data yet')
    expect(container.querySelector('svg[class*="bg-bg-elevated"]')).toBeNull()
  })
})

describe('KnowledgeGraph — chrome', () => {
  it('reports the node and edge counts', async () => {
    renderGraph()
    expect(await screen.findByText('5 nodes, 4 edges')).toBeInTheDocument()
  })

  it('renders a legend swatch for every known entity type', async () => {
    renderGraph()
    await screen.findByText('5 nodes, 4 edges')
    for (const label of ['service', 'technology', 'concept', 'org']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })

  it('renders the recenter control', async () => {
    renderGraph()
    expect(await screen.findByRole('button', { name: /recenter/i })).toBeInTheDocument()
  })
})

describe('KnowledgeGraph — d3 draw', () => {
  it('draws one group per node, tagged with the entity name', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const names = Array.from(container.querySelectorAll('g[data-entity]'))
      .map(g => g.getAttribute('data-entity'))
    expect(names).toEqual(['Gateway', 'Postgres', 'Retrieval', 'Platform', 'Widgets'])
  })

  it('colours each node by its entity type and falls back for unknown types', async () => {
    const { container } = renderGraph()
    await drawn(container)
    expect(circleOf(container, 'Gateway').getAttribute('fill')).toBe('#3b82f6')
    expect(circleOf(container, 'Postgres').getAttribute('fill')).toBe('#22c55e')
    expect(circleOf(container, 'Retrieval').getAttribute('fill')).toBe('#a855f7')
    expect(circleOf(container, 'Platform').getAttribute('fill')).toBe('#f97316')
    expect(circleOf(container, 'Widgets').getAttribute('fill')).toBe('#6b7280')
  })

  it('gives unhighlighted nodes the default radius and stroke width', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const circle = circleOf(container, 'Gateway')
    expect(circle.getAttribute('r')).toBe('8')
    expect(circle.getAttribute('stroke-width')).toBe('1.5')
  })

  it('labels every node with its name', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const labels = Array.from(container.querySelectorAll('g[data-entity] text'))
      .map(t => t.textContent)
    expect(labels).toEqual(['Gateway', 'Postgres', 'Retrieval', 'Platform', 'Widgets'])
  })

  it('draws one line per edge with positions resolved from the layout', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const lines = Array.from(container.querySelectorAll('line'))
    expect(lines).toHaveLength(GRAPH.edges.length)
    for (const line of lines) {
      for (const attr of ['x1', 'y1', 'x2', 'y2']) {
        expect(Number.isFinite(Number(line.getAttribute(attr)))).toBe(true)
      }
    }
  })

  it('scales edge stroke width with the relation weight', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const widths = Array.from(container.querySelectorAll('line'))
      .map(l => l.getAttribute('stroke-width'))
    // weight 3 -> 4.5; weight 1 and missing weight both floor at 1.5.
    expect(widths).toEqual(['4.5', '1.5', '1.5', '1.5'])
  })

  it('labels each edge with its relation type at the segment midpoint', async () => {
    const { container } = renderGraph()
    await drawn(container)
    for (const type of ['depends_on', 'implements', 'owned_by', 'ships']) {
      const label = Array.from(container.querySelectorAll('text'))
        .find(t => t.textContent === type)
      expect(label, `edge label ${type}`).toBeTruthy()
      expect(Number.isFinite(Number(label?.getAttribute('x')))).toBe(true)
      expect(Number.isFinite(Number(label?.getAttribute('y')))).toBe(true)
    }
  })

  it('applies a fit transform to the drawn group', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const outer = container.querySelector('svg > g')
    expect(outer?.getAttribute('transform')).toMatch(/translate\(.+\)\s*scale\(.+\)/)
  })

  it('positions every node group with a translate transform', async () => {
    const { container } = renderGraph()
    await drawn(container)
    for (const g of container.querySelectorAll('g[data-entity]')) {
      expect(g.getAttribute('transform')).toMatch(/^translate\(-?[\d.]+,-?[\d.]+\)$/)
    }
  })

  it('does not redraw when new graph data carries the same node ids and edge count', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const first = container.querySelector('g[data-entity="Gateway"]')
    // Renaming a node changes the data identity, so the effect re-runs — but the
    // render key is derived from ids plus edge count only, so the existing
    // drawing is kept and the new name is never picked up.
    qc.setQueryData(['knowledge-graph'], {
      nodes: GRAPH.nodes.map(n => (n.id === 'n1' ? { ...n, name: 'Renamed' } : { ...n })),
      edges: GRAPH.edges.map(e => ({ ...e })),
    })
    await waitFor(() => {
      expect(container.querySelectorAll('g[data-entity]')).toHaveLength(GRAPH.nodes.length)
    })
    expect(container.querySelector('g[data-entity="Gateway"]')).toBe(first)
    expect(container.querySelector('g[data-entity="Renamed"]')).toBeNull()
  })

  it('abandons the draw when the component unmounts before d3 resolves', async () => {
    qc.setQueryData(['knowledge-graph'], GRAPH)
    const { container, unmount } = renderGraph()
    // No await between mount and unmount, so the dynamic d3 import has not had a
    // microtask to settle and must observe the aborted flag.
    unmount()
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalled())
    expect(container.querySelectorAll('g[data-entity]')).toHaveLength(0)
  })

  it('redraws from scratch when the node set changes', async () => {
    const { container } = renderGraph()
    await drawn(container)
    qc.setQueryData(['knowledge-graph'], {
      nodes: [{ id: 'z1', name: 'Solo', type: 'service' }],
      edges: [],
    })
    await drawn(container, 1)
    expect(container.querySelector('g[data-entity="Gateway"]')).toBeNull()
    expect(container.querySelectorAll('line')).toHaveLength(0)
  })
})

describe('KnowledgeGraph — interaction', () => {
  it('reports the clicked entity name to the parent', async () => {
    const onSelectEntity = vi.fn()
    const { container } = renderGraph({ onSelectEntity })
    await drawn(container)
    fireEvent.click(container.querySelector('g[data-entity="Retrieval"]')!)
    expect(onSelectEntity).toHaveBeenCalledWith('Retrieval')
  })

  it('survives a click when no selection handler is supplied', async () => {
    const { container } = renderGraph()
    await drawn(container)
    expect(() => fireEvent.click(container.querySelector('g[data-entity="Gateway"]')!)).not.toThrow()
  })

  it('accents a node on hover and restores it on leave', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const group = container.querySelector('g[data-entity="Postgres"]')!
    fireEvent.mouseEnter(group)
    expect(circleOf(container, 'Postgres').getAttribute('stroke')).toBe('#fbbf24')
    expect(circleOf(container, 'Postgres').getAttribute('stroke-width')).toBe('3')
    fireEvent.mouseLeave(group)
    expect(circleOf(container, 'Postgres').getAttribute('stroke')).toBe('#ccc')
    expect(circleOf(container, 'Postgres').getAttribute('stroke-width')).toBe('1.5')
  })

  it('restores a hovered node to the highlighted style, not the default one', async () => {
    const { container, rerender } = renderGraph()
    await drawn(container)
    rerender({ highlightEntity: 'Postgres' })
    const group = container.querySelector('g[data-entity="Postgres"]')!
    fireEvent.mouseEnter(group)
    fireEvent.mouseLeave(group)
    expect(circleOf(container, 'Postgres').getAttribute('stroke-width')).toBe('4')
  })

  it('recenters without throwing when the control is pressed', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const before = container.querySelector('svg > g')?.getAttribute('transform')
    fireEvent.click(screen.getByRole('button', { name: /recenter/i }))
    expect(container.querySelector('svg > g')?.getAttribute('transform')).toBe(before)
  })

  it('pins a dragged node to the pointer and releases it on drop', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const group = container.querySelector('g[data-entity="Gateway"]') as SVGGElement
    const line = container.querySelector('line') as SVGLineElement
    const [x0, y0] = translateOf(group)
    // Fake timers drive the force simulation the drag restarts, so the redraw a
    // running layout performs is exercised without waiting on real frames.
    vi.useFakeTimers()
    try {
      fireEvent.mouseDown(group, { button: 0, clientX: 10, clientY: 10, view: window, bubbles: true })
      await vi.advanceTimersByTimeAsync(64)
      fireEvent.mouseMove(window, { clientX: 90, clientY: 70, view: window, bubbles: true })
      await vi.advanceTimersByTimeAsync(64)
      // While pinned, the node sits exactly at the pointer delta from where it
      // started, overriding whatever the layout forces want.
      const [x1, y1] = translateOf(group)
      expect(Math.round(x1 - x0)).toBe(80)
      expect(Math.round(y1 - y0)).toBe(60)
      fireEvent.mouseUp(window, { clientX: 90, clientY: 70, view: window, bubbles: true })
      await vi.advanceTimersByTimeAsync(64)
    } finally {
      vi.useRealTimers()
    }
    // Released: the layout owns the node again and every drawn element stays finite.
    const [x2, y2] = translateOf(group)
    expect(Number.isFinite(x2) && Number.isFinite(y2)).toBe(true)
    for (const attr of ['x1', 'y1', 'x2', 'y2']) {
      expect(Number.isFinite(Number(line.getAttribute(attr)))).toBe(true)
    }
  })

  it('ignores the recenter control before the layout has been measured', async () => {
    mockKnowledgeApi.mockImplementation(() => new Promise(() => {}))
    renderGraph()
    await screen.findByText('Loading graph...')
    // No button yet — the loading branch owns the tree.
    expect(screen.queryByRole('button', { name: /recenter/i })).toBeNull()
  })
})

describe('KnowledgeGraph — highlight', () => {
  it('enlarges and accents the highlighted node while shrinking the others', async () => {
    const { container, rerender } = renderGraph()
    await drawn(container)
    rerender({ highlightEntity: 'Platform' })
    const target = circleOf(container, 'Platform')
    expect(target.getAttribute('r')).toBe('12')
    expect(target.getAttribute('stroke-width')).toBe('4')
    expect(target.getAttribute('stroke')).toBe('#fbbf24')
    const other = circleOf(container, 'Gateway')
    expect(other.getAttribute('r')).toBe('8')
    expect(other.getAttribute('stroke-width')).toBe('1.5')
    expect(other.getAttribute('stroke')).toBe('#fff')
  })

  it('resets every node when the highlight is cleared', async () => {
    const { container, rerender } = renderGraph()
    await drawn(container)
    rerender({ highlightEntity: 'Platform' })
    expect(circleOf(container, 'Platform').getAttribute('r')).toBe('12')
    rerender({ highlightEntity: null })
    for (const name of ['Gateway', 'Postgres', 'Retrieval', 'Platform', 'Widgets']) {
      expect(circleOf(container, name).getAttribute('r')).toBe('8')
      expect(circleOf(container, name).getAttribute('stroke-width')).toBe('1.5')
      expect(circleOf(container, name).getAttribute('stroke')).toBe('#fff')
    }
  })

  it('leaves the drawing intact when the highlight names an unknown entity', async () => {
    const { container, rerender } = renderGraph()
    await drawn(container)
    rerender({ highlightEntity: 'NotInGraph' })
    for (const name of ['Gateway', 'Widgets']) {
      expect(circleOf(container, name).getAttribute('r')).toBe('8')
    }
  })

  it('pre-accents the highlighted node on the very first draw', async () => {
    const { container } = renderGraph({ highlightEntity: 'Widgets' })
    await drawn(container)
    const target = circleOf(container, 'Widgets')
    expect(target.getAttribute('r')).toBe('12')
    expect(target.getAttribute('stroke-width')).toBe('4')
    expect(target.getAttribute('stroke')).toBe('#fbbf24')
  })

  it('polls for the zoom handle when the highlight is set before d3 has drawn', async () => {
    // Seeding the cache means the svg exists on the first committed render, so
    // the highlight effect runs while the zoom handle is still uninitialised.
    qc.setQueryData(['knowledge-graph'], GRAPH)
    vi.useFakeTimers()
    try {
      const { container } = renderGraph({ highlightEntity: 'Gateway' })
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(200)
      await vi.advanceTimersByTimeAsync(200)
      expect(container.querySelectorAll('g[data-entity]')).toHaveLength(GRAPH.nodes.length)
      expect(circleOf(container, 'Gateway').getAttribute('r')).toBe('12')
    } finally {
      vi.useRealTimers()
    }
  })
  it('gives up polling for the zoom handle after twenty attempts', async () => {
    qc.setQueryData(['knowledge-graph'], GRAPH)
    vi.useFakeTimers()
    try {
      renderGraph({ highlightEntity: 'Gateway' })
      // Advancing without awaiting keeps the microtask queue frozen, so the d3
      // import never settles and every poll observes an unset zoom handle.
      vi.advanceTimersByTime(200 * 22)
      // One more interval period proves the poll was cleared, not still firing.
      expect(() => vi.advanceTimersByTime(200)).not.toThrow()
    } finally {
      vi.useRealTimers()
    }
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalled())
  })
})

describe('KnowledgeGraph — resize refit', () => {
  it('observes the svg once the graph is drawn', async () => {
    const { container } = renderGraph()
    await drawn(container)
    expect(resizeCallbacks.length).toBeGreaterThan(0)
  })

  it('recomputes the fit transform when the svg is resized', async () => {
    const { container } = renderGraph()
    await drawn(container)
    const before = container.querySelector('svg > g')?.getAttribute('transform')
    const observer = { disconnect() {}, observe() {}, unobserve() {} } as unknown as ResizeObserver
    for (const cb of resizeCallbacks) cb([], observer)
    const after = container.querySelector('svg > g')?.getAttribute('transform')
    expect(after).toMatch(/translate\(.+\)\s*scale\(.+\)/)
    expect(after).toBe(before)
  })

  it('ignores a resize that arrives before any layout has been measured', async () => {
    mockKnowledgeApi.mockResolvedValue({ nodes: [], edges: [] })
    renderGraph()
    await screen.findByText('No graph data yet')
    const observer = { disconnect() {}, observe() {}, unobserve() {} } as unknown as ResizeObserver
    expect(() => { for (const cb of resizeCallbacks) cb([], observer) }).not.toThrow()
  })

  it('ignores a resize that lands while the svg exists but d3 has not drawn', async () => {
    qc.setQueryData(['knowledge-graph'], GRAPH)
    const { container } = renderGraph()
    // Fired synchronously after mount: the observer is registered but the d3
    // import has had no microtask to record the graph bounds yet.
    const observer = { disconnect() {}, observe() {}, unobserve() {} } as unknown as ResizeObserver
    expect(resizeCallbacks.length).toBeGreaterThan(0)
    for (const cb of resizeCallbacks) cb([], observer)
    expect(container.querySelector('svg > g')).toBeNull()
    await drawn(container)
  })
})
