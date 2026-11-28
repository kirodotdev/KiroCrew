import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterAll, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'

// Install before importing Motion, which captures requestAnimationFrame once.
// The queue also owns frames scheduled before a test takes control.
const animationFrames = vi.hoisted(() => {
  const request = window.requestAnimationFrame.bind(window)
  const cancel = window.cancelAnimationFrame.bind(window)
  const pending = new Map<number, { cb: FrameRequestCallback; native?: number }>()
  let controlled = false, id = 0
  window.requestAnimationFrame = cb => {
    const key = ++id
    const entry: { cb: FrameRequestCallback; native?: number } = { cb }
    pending.set(key, entry)
    if (!controlled) entry.native = request(now => { pending.delete(key); cb(now) })
    return key
  }
  window.cancelAnimationFrame = key => {
    const entry = pending.get(key)
    if (entry?.native !== undefined) cancel(entry.native)
    pending.delete(key)
  }
  return {
    takeControl() {
      controlled = true
      for (const entry of pending.values()) if (entry.native !== undefined) cancel(entry.native)
    },
    step(now: number) {
      const callbacks = [...pending.values()]
      pending.clear()
      for (const { cb } of callbacks) cb(now)
    },
    restore() { window.requestAnimationFrame = request; window.cancelAnimationFrame = cancel },
    release() {
      controlled = false
      const callbacks = [...pending.values()]
      pending.clear()
      for (const { cb } of callbacks) window.requestAnimationFrame(cb)
    },
  }
})
afterAll(() => animationFrames.restore())

// cosmos.gl draws with WebGL, which the test DOM lacks: a stand-in records what
// the tab asks it to draw and lets a test fire its callbacks.
type Config = Record<string, unknown> & {
  onPointClick?: (i: number) => void
  onPointMouseOver?: (i: number) => void
}
const cosmos = vi.hoisted(() => {
  const instances: FakeGraph[] = []
  const state = { failNext: false, deviceFails: false }
  class FakeGraph {
    config: Config
    positions = new Float32Array()
    colors = new Float32Array()
    renders: (number | undefined)[] = []
    ready: Promise<void>
    constructor(_el: HTMLElement, config: Config) {
      if (state.failNext) { state.failNext = false; throw new Error('no WebGL') }
      this.config = { ...config }; instances.push(this)
      this.ready = state.deviceFails ? Promise.reject(new Error('no WebGL device')) : Promise.resolve()
    }
    drawnCounts: number[] = []
    setPointPositions(p: Float32Array) { this.positions = p; this.drawnCounts.push(p.length / 2) }
    colorUploads = 0
    colorHistory: Float32Array[] = []
    sizes = new Float32Array()
    sizeUploads = 0
    setPointColors(c: Float32Array) { this.colors = c; this.colorUploads++; this.colorHistory.push(c) }
    setPointSizes(s: Float32Array) { this.sizes = s; this.sizeUploads++ }
    shapes = new Float32Array()
    setPointShapes(s: Float32Array) { this.shapes = s }
    links = new Float32Array()
    linkColors = new Float32Array()
    setLinks(l: Float32Array) { this.links = l }
    setLinkColors(c: Float32Array) { this.linkColors = c }
    setLinkWidths() {}
    imageIndices = new Float32Array()
    imageSizes = new Float32Array()
    activityUploads = 0
    setPointActivity(at: Float32Array, strength: Float32Array) {
      // The patched cosmos setter refuses activity for a different point count.
      if (at.length !== this.positions.length / 2) return
      this.imageIndices = at; this.imageSizes = strength; this.activityUploads++
    }
    setPointImageIndices(a: Float32Array) { this.imageIndices = a }
    setPointImageSizes(a: Float32Array) { this.imageSizes = a }
    oneFrames = 0
    renderOneFrame() { this.oneFrames++ }
    setConfigPartial(c: Config) { Object.assign(this.config, c) }
    render(_alpha?: number, duration?: number) { this.renders.push(duration) }
    transforms = 0
    view = { k: 0, x: 0, y: 0 }
    ks: number[] = []
    setViewTransform(t: { k: number; x: number; y: number }) { this.transforms++; this.view = t; this.ks.push(t.k) }
    destroy() {}
  }
  return { instances, FakeGraph, set failNext(v: boolean) { state.failNext = v }, set deviceFails(v: boolean) { state.deviceFails = v } }
})
type FakeGraph = InstanceType<typeof cosmos.FakeGraph>
vi.mock('@cosmos.gl/graph', () => ({ Graph: cosmos.FakeGraph }))

vi.mock('../api/client', () => ({
  api: {
    memoryGraphOverview: vi.fn(), memoryGraphNodes: vi.fn(),
    memoryGraphSearch: vi.fn(), memoryGraphSelect: vi.fn(),
    memoryRecords: vi.fn(), memoryRecordHistory: vi.fn(),
    memoryEditPreview: vi.fn(), memoryEditApply: vi.fn(),
  },
}))
const motion = vi.hoisted(() => ({ reduced: false }))
vi.mock('../hooks/useReducedMotion', () => ({ useReducedMotion: () => motion.reduced }))

import { api } from '../api/client'
import MemoryGraphTab, { MOTION_MS, SEARCH_DEBOUNCE_MS } from '../pages/overview/MemoryGraphTab'
import { BreathLayer } from '../pages/overview/memoryGraph/graphBreathLayer'
import { BREATH_FPS } from '../pages/overview/memoryGraph/graphActivityRunner'
import { MemoryJumpContext } from '../pages/overview/memoryPage/memoryJump'
import { NODE_BUDGET } from '../pages/overview/memoryGraph/graphView'
import { DIM_ALPHA } from '../pages/overview/memoryGraph/graphLight'
import { NodeCache, PAGE_ROWS } from '../pages/overview/memoryGraph/nodeCache'

const node = (id: string, kind: 'episode' | 'fact' | 'lesson', x: number, y: number, weight = 1) => ({
  id, kind, x, y, cluster: 0, weight, label: `${id} text`, ref: `${id.padEnd(10, '0')}`.slice(0, 10),
  record: { kind: kind === 'lesson' ? 'directive' : kind, id },
})
const NODES = [node('aa', 'fact', -50, 0), node('bb', 'lesson', 50, 0), node('cc', 'episode', 0, 40, 0.1)]
const overview = (count = 3) => ({
  status: 'ready', layout: 'L1', clusters: [{ id: 0, x: 0, y: 0, r: 60, count, label: 'topic' }],
  // The kind totals hold the cluster's count, so a big cluster is a big store.
  totals: count === 3 ? { fact: 1, lesson: 1, episode: 1 } : { fact: count }, bounds: [-60, -60, 60, 60],
})

function renderTab(jump = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryJumpContext.Provider value={jump}><MemoryGraphTab store="member-a" /></MemoryJumpContext.Provider></QueryClientProvider>)
  return jump
}
const graph = () => cosmos.instances.at(-1)!
/**
 * Ceiling for a wait that crosses the canvas's lazy boundary: the overview
 * query resolves, the canvas effect commits, `import('@cosmos.gl/graph')`
 * resolves (the mocked module, still a dynamic import), and the graph is built.
 * A loaded coverage worker can take longer than RTL's default second for that.
 */
const LAZY_INIT = { timeout: 5000 }
/** The canvas once the visible nodes are drawn. */
async function drawn(n = NODES.length) {
  await waitFor(() => expect(graph().positions.length).toBe(n * 2), LAZY_INIT)
  return graph()
}
/** Installed after the async draw barrier: timers and camera frames share one
 * clock, but a frame runs only when the test explicitly delivers it. */
function cameraClock() {
  const start = performance.now()
  animationFrames.takeControl()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.advanceTimersByTime(start + 1000)
  onTestFinished(() => { vi.useRealTimers(); animationFrames.release() })
  act(() => animationFrames.step(performance.now()))
  return {
    async frame(ms: number) {
      await act(async () => {
        vi.advanceTimersByTime(ms)
        animationFrames.step(performance.now())
      })
    },
    async settle() { await act(async () => { vi.advanceTimersByTime(125); animationFrames.step(performance.now()) }) },
  }
}
const indexOf = (id: string) => NODES.findIndex(n => n.id === id)
const alphaOf = (g: FakeGraph, id: string) => g.colors[indexOf(id) * 4 + 3]
const pointer = (type: string, target: EventTarget, init: Record<string, unknown>) => {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init })
  Object.assign(event, { pointerId: 1, pointerType: 'mouse' })
  act(() => { target.dispatchEvent(event) })
}

beforeEach(() => {
  vi.clearAllMocks()
  cosmos.instances.length = 0
  motion.reduced = false
  cosmos.deviceFails = false
  vi.mocked(api.memoryGraphOverview).mockResolvedValue(overview() as never)
  vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: NODES, cursor: null } as never)
  vi.mocked(api.memoryGraphSearch).mockResolvedValue({ status: 'ready', layout: 'L1', matches: [], total: 0 } as never)
  vi.mocked(api.memoryGraphSelect).mockResolvedValue({ status: 'ready', layout: 'L1', count: 0 } as never)
  vi.mocked(api.memoryRecords).mockResolvedValue({ entries: [RECORD], total: 1, has_more: false } as never)
  vi.mocked(api.memoryRecordHistory).mockResolvedValue({ entries: [], has_more: false, current_revision: 1 } as never)
})
const RECORD = { kind: 'directive', id: 'bb', text: 'bb the whole lesson', revision: 'r1', source: 'manual' }
/** Zoom in with the + key `times` times, at once (reduced motion). */
async function zoomIn(g: FakeGraph, times: number) {
  const canvas = screen.getByTestId('memory-graph-canvas')
  for (let i = 0; i < times; i++) {
    const k = g.view.k
    fireEvent.keyDown(canvas, { key: '+' })
    await waitFor(() => expect(g.view.k).toBeGreaterThan(k))
  }
}

describe('the memory graph over every memory', () => {
  it('draws the server positions of the nodes in view, for the named store', async () => {
    renderTab()
    const g = await drawn()
    expect(vi.mocked(api.memoryGraphNodes).mock.calls[0][0]).toBe('member-a')
    expect([...g.positions]).toEqual(NODES.flatMap(n => [n.x, n.y]))
    expect(g.config.enableSimulation).toBe(false)
  })

  it('refetches the overview when the memory-graph query family is invalidated', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const tab = render(<QueryClientProvider client={client}><MemoryGraphTab store="member-a" /></QueryClientProvider>)
    try {
      await drawn()
      expect(api.memoryGraphOverview).toHaveBeenCalledExactlyOnceWith('member-a')
      vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), layout: 'L2' } as never)
      vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L2', nodes: NODES, cursor: null } as never)

      await act(async () => { await client.invalidateQueries({ queryKey: ['memory-graph'] }) })

      expect(api.memoryGraphOverview).toHaveBeenCalledTimes(2)
      await waitFor(() => expect(api.memoryGraphNodes).toHaveBeenCalledTimes(2), LAZY_INIT)
      await drawn()
    } finally {
      tab.unmount()
      client.clear()
    }
  })

  it('names a fact on the canvas by its key over two lines, never cut, and keeps the whole label for hover', async () => {
    const long = { ...node('dd', 'fact', 0, -40), label: 'pref.kirocrew.cli_help_probe_is_policy_blocked = true', record: { kind: 'fact', id: 'pref.kirocrew.cli_help_probe_is_policy_blocked' } }
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: [...NODES, long], cursor: null } as never)
    renderTab()
    await drawn(NODES.length + 1)
    const name = (await screen.findByText(/^cli help probe/, {}, LAZY_INIT)).parentElement!
    // Whole words over two lines: the full title, no ellipsis.
    expect(name.children.length).toBe(2)
    expect([...name.children].map(c => c.textContent).join(' ')).toBe('cli help probe is policy blocked')
    expect(name.className).not.toContain('truncate')
    expect(screen.queryByText(/pref\.kirocrew\.cli_help/)).toBeNull()
  })

  it('draws a thin link between related memories, once, faint at the overview and lit on hover', async () => {
    const linked = [
      { ...NODES[0], links: [['bb', 0.9]] }, { ...NODES[1], links: [['aa', 0.9], ['zz', 0.9]] }, NODES[2],
    ]
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: linked, cursor: null } as never)
    motion.reduced = true
    renderTab()
    const g = await drawn()
    // With the whole store in view a memory's own links wait for the first level.
    expect([...g.links]).toEqual([])
    await zoomIn(g, 3)
    // bb's link to zz, a memory not drawn, is left out; aa-bb is drawn once.
    await waitFor(() => expect([...g.links]).toEqual([indexOf('aa'), indexOf('bb')]))
    expect(g.config.curvedLinks).toBe(true)
    const overview = g.linkColors[3]
    expect(overview).toBeGreaterThan(0)
    expect(overview).toBeLessThan(0.2)
    act(() => g.config.onPointMouseOver!(indexOf('aa')))
    expect(g.linkColors[3]).toBeGreaterThan(overview)
  })

  it('opens a tapped memory on the Graph tab in the Memories detail view, and closing keeps the view', async () => {
    motion.reduced = true
    const jump = renderTab()
    const g = await drawn()
    await zoomIn(g, 2)
    const before = { ...g.view }
    act(() => g.config.onPointClick!(indexOf('bb')))
    const panel = await screen.findByTestId('memory-record-detail')
    expect(within(panel).getByText('bb the whole lesson')).toBeTruthy()
    expect(within(panel).getByRole('button', { name: 'Edit' })).toBeTruthy()
    expect(vi.mocked(api.memoryRecords).mock.calls[0][0]).toBe('member-a')
    expect(vi.mocked(api.memoryRecords).mock.calls[0][1]).toEqual({ q: '', kind: 'all', filters: { ref: [NODES[1].ref] } })
    expect(jump).not.toHaveBeenCalled()
    // The graph is still drawn behind it, and closing returns to the same view.
    expect(screen.getByTestId('memory-graph-canvas')).toBeTruthy()
    fireEvent.click(within(panel).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByTestId('memory-record-detail')).toBeNull())
    expect(g.view).toEqual(before)
  })

  it.each(['forget', 'edit'] as const)('refreshes cached nodes after a detail %s with the same layout revision', async action => {
    const cached = vi.spyOn(NodeCache.prototype, 'all')
    onTestFinished(() => cached.mockRestore())
    motion.reduced = true
    // Keep the edited memory on screen at the zoom this test preserves.
    const linked = [{ ...NODES[0], links: [['bb', 0.9]] }, { ...NODES[1], x: 10, links: [['aa', 0.9]] }, NODES[2]]
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: linked, cursor: null } as never)
    const changed = { ...RECORD, text: 'Updated lesson', revision: 'r2' }
    vi.mocked(api.memoryEditPreview).mockResolvedValue({
      preview_id: 'detail-preview', expires_at: '2026-12-31T12:00:00Z',
      matched_count: 1, changed_count: 1, unchanged_count: 0,
      entries: [{ before: RECORD, after: action === 'forget' ? null : changed, operation: action === 'forget' ? 'forget' : 'set' }],
      preview_offset: 0, preview_limit: 25, preview_has_more: false, warnings: [],
    } as never)
    const nextNodes = action === 'forget' ? [NODES[0], NODES[2]] : linked.map(n => n.id === 'bb' ? { ...n, label: changed.text } : n)
    vi.mocked(api.memoryEditApply).mockImplementation(async () => {
      vi.mocked(api.memoryRecords).mockResolvedValue({ entries: action === 'forget' ? [] : [changed], total: action === 'forget' ? 0 : 1, has_more: false } as never)
      vi.mocked(api.memoryGraphOverview).mockResolvedValue((action === 'forget' ? { ...overview(), totals: { fact: 1, episode: 1 }, clusters: [{ ...overview().clusters[0], count: 2 }] } : overview()) as never)
      vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: nextNodes, cursor: null } as never)
      return { changed_count: 1 } as never
    })
    renderTab()
    const g = await drawn()
    await zoomIn(g, 3)
    await waitFor(() => expect([...g.links]).toEqual([0, 1]))
    const view = { ...g.view }
    act(() => g.config.onPointClick!(indexOf('bb')))
    const panel = await screen.findByTestId('memory-record-detail')
    fireEvent.click(within(panel).getByRole('button', { name: action === 'forget' ? 'Forget' : 'Edit', exact: true }))
    const dialog = await screen.findByRole('dialog')
    if (action === 'edit') fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: changed.text } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Preview changes', exact: true }))
    fireEvent.click(await within(dialog).findByRole('button', { name: action === 'forget' ? 'Forget selected memories: 1' : 'Apply changes (1)', exact: true }))
    await waitFor(() => expect(api.memoryEditApply).toHaveBeenCalledExactlyOnceWith('member-a', 'detail-preview'))
    await waitFor(() => expect(vi.mocked(api.memoryGraphOverview).mock.calls.length).toBeGreaterThan(1))
    await waitFor(() => expect(cached.mock.results.at(-1)?.value).toEqual(nextNodes), LAZY_INIT)
    if (action === 'forget') {
      await waitFor(() => expect([...g.positions]).toEqual(nextNodes.flatMap(n => [n.x, n.y])), LAZY_INIT)
      expect([...g.links]).toEqual([])
      await waitFor(() => expect(screen.queryByTestId('memory-record-detail')).toBeNull())
    } else {
      act(() => g.config.onPointMouseOver!(indexOf('bb')))
      expect(screen.getByTestId('memory-graph-hover').textContent).toBe(changed.text)
    }
    expect(api.memoryGraphNodes).toHaveBeenCalledTimes(2)
    expect(g.view).toEqual(view)
  })

  it('offers the agent hand-off when reading a tapped memory fails', async () => {
    vi.mocked(api.memoryRecords).mockRejectedValue(Object.assign(new Error('Memory read refused'), { status: 403 }))
    renderTab()
    const g = await drawn()
    act(() => g.config.onPointClick!(indexOf('bb')))
    const panel = await screen.findByTestId('memory-graph-detail')
    expect(within(panel).getByText('Memory read refused')).toBeTruthy()
    expect(within(panel).getByRole('button', { name: 'Ask the agent' })).toBeTruthy()
    expect(screen.getByTestId('memory-graph-canvas')).toBeTruthy()
  })

  it('tells a mouse user to hold Space to pan', async () => {
    renderTab()
    await drawn()
    expect(screen.getByTestId('memory-graph-pan-hint').textContent).toBe('Hold Space to pan')
  })

  it('shows no Space hint on a touch screen, which has no Space key', async () => {
    const was = window.matchMedia
    window.matchMedia = ((q: string) => ({ matches: q === '(pointer: coarse)', media: q, addEventListener() {}, removeEventListener() {} })) as never
    try {
      renderTab()
      await drawn()
      expect(screen.queryByTestId('memory-graph-pan-hint')).toBeNull()
    } finally { window.matchMedia = was }
  })

  it('joins neighbouring topics, not memories, with the whole store in view', async () => {
    const two = [node('a1', 'fact', -10, 0), node('a2', 'fact', -14, 2), { ...node('b1', 'fact', 10, 0), cluster: 1 }, { ...node('b2', 'fact', 14, 2), cluster: 1 }]
    two[1] = { ...two[1], links: [['b2', 0.9], ['a1', 0.8]] } as never
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), totals: { fact: 4 }, clusters: [{ id: 0, x: -10, y: 0, r: 8, count: 2, label: 'a' }, { id: 1, x: 10, y: 0, r: 8, count: 2, label: 'b' }], bounds: [-20, -20, 20, 20] } as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: two, cursor: null } as never)
    renderTab()
    const g = await drawn(4)
    // One link, between the memories at the two topics' centres (a1 and b1).
    await waitFor(() => expect([...g.links]).toEqual([0, 2]))
  })

  it('fades an experience by its recall weight and keeps facts and lessons solid', async () => {
    renderTab()
    const g = await drawn()
    const solid = alphaOf(g, 'aa')
    expect(solid).toBeGreaterThan(0.5)
    expect(alphaOf(g, 'bb')).toBe(solid)
    expect(alphaOf(g, 'cc')).toBeLessThan(solid)
    expect(alphaOf(g, 'cc')).toBeGreaterThan(0.1)
  })

  it('opens a cluster into its nodes from the cluster centre, animated', async () => {
    renderTab()
    const g = await drawn()
    expect(g.renders.at(-1)).toBeGreaterThan(0)
  })

  it('swaps clusters for nodes at once under reduced motion', async () => {
    motion.reduced = true
    renderTab()
    const g = await drawn()
    expect(g.renders.every(d => d === 0)).toBe(true)
  })

  it('lights what the server found by similarity, dims and greys the rest, and names only lit topics', async () => {
    vi.mocked(api.memoryGraphSearch).mockResolvedValue({ status: 'ready', layout: 'L1', matches: [{ id: 'bb', x: 50, y: 0, cluster: 0, score: 1 }, { id: 'cc', x: 0, y: 40, cluster: 0, score: 0.5 }], total: 2 } as never)
    renderTab()
    const g = await drawn()
    const solid = alphaOf(g, 'bb')
    const [r, gr, b] = g.colors.slice(indexOf('aa') * 4, indexOf('aa') * 4 + 3)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'needle' } })
    await waitFor(() => expect(api.memoryGraphSearch).toHaveBeenCalledWith('member-a', 'needle', { kinds: undefined }), { timeout: 3000 })
    await waitFor(() => expect(alphaOf(g, 'aa')).toBeCloseTo(DIM_ALPHA))
    // The best hit is brightest; the weaker one still stands out from the dimmed.
    expect(alphaOf(g, 'bb')).toBeGreaterThanOrEqual(solid)
    expect(alphaOf(g, 'cc')).toBeLessThan(alphaOf(g, 'bb'))
    expect(alphaOf(g, 'cc')).toBeGreaterThan(DIM_ALPHA)
    // A dimmed mark loses its colour: its channels move toward one grey.
    const dim = g.colors.slice(indexOf('aa') * 4, indexOf('aa') * 4 + 3)
    expect(Math.max(...dim) - Math.min(...dim)).toBeLessThan(Math.max(r, gr, b) - Math.min(r, gr, b))
    expect((await screen.findByTestId('memory-graph-lit')).textContent).toBe('2 found')
  })

  it('names only the topics holding hits while a set is lit, each with its hit count', async () => {
    const two = { ...overview(), clusters: [{ id: 0, x: 50, y: 0, r: 20, count: 50, label: 'alpha' }, { id: 1, x: 50, y: 25, r: 20, count: 350, label: 'beta' }] }
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(two as never)
    const left = (i: number) => i < 50
    const many = Array.from({ length: 400 }, (_, i) => ({ ...node(`n${i}`, 'fact', (left(i) ? -55 : 45) + (i % 20) * 0.5, -5 + Math.floor(i / 20) * 0.5), cluster: left(i) ? 0 : 1 }))
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: many, cursor: null } as never)
    const hits = (ids: typeof many, cluster?: number) => ({ status: 'ready', layout: 'L1', matches: ids.map(n => ({ id: n.id, x: n.x, y: n.y, cluster: cluster ?? n.cluster, score: 1 })), total: ids.length })
    vi.mocked(api.memoryGraphSearch).mockResolvedValue(hits(many) as never)
    renderTab()
    await drawn(400)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'needle' } })
    expect(await screen.findByText('alpha · 50', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.getByText('beta · 350')).toBeTruthy()
    // beta's memories are lit but counted to alpha: beta holds no hit and loses its name.
    vi.mocked(api.memoryGraphSearch).mockResolvedValue(hits(many.filter((_, i) => !left(i)), 0) as never)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'other' } })
    expect(await screen.findByText('alpha · 350', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText(/^beta/)).toBeNull()
  })

  it('fades into the lit set and back out when the search is cleared, in steps', async () => {
    vi.mocked(api.memoryGraphSearch).mockResolvedValue({ status: 'ready', layout: 'L1', matches: [{ id: 'bb', x: 50, y: 0, cluster: 0, score: 1 }], total: 1 } as never)
    renderTab()
    const g = await drawn()
    const solid = alphaOf(g, 'aa')
    const clock = cameraClock()
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'needle' } })
    await clock.frame(SEARCH_DEBOUNCE_MS)
    expect(api.memoryGraphSearch).toHaveBeenCalledWith('member-a', 'needle', { kinds: undefined })
    expect(alphaOf(g, 'aa')).toBeCloseTo(solid)
    await clock.frame(MOTION_MS / 2)
    expect(alphaOf(g, 'aa')).toBeCloseTo(solid + (DIM_ALPHA - solid) * 0.875)
    await clock.frame(MOTION_MS / 2)
    expect(alphaOf(g, 'aa')).toBeCloseTo(DIM_ALPHA)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: '' } })
    await clock.frame(SEARCH_DEBOUNCE_MS)
    expect(alphaOf(g, 'aa')).toBeCloseTo(DIM_ALPHA)
    await clock.frame(MOTION_MS / 2)
    expect(alphaOf(g, 'aa')).toBeCloseTo(DIM_ALPHA + (solid - DIM_ALPHA) * 0.875)
    await clock.frame(MOTION_MS / 2)
    expect(alphaOf(g, 'aa')).toBeCloseTo(solid)
    expect(screen.queryByTestId('memory-graph-lit')).toBeNull()
  })

  const read = (detail: Record<string, unknown>) => act(() => { window.dispatchEvent(new CustomEvent('mc:memory-activity', { detail })) })

  it('lights a memory this store recalled, as it happens, and breathes on its own clock', async () => {
    renderTab()
    const g = await drawn()
    const uniforms = (g.config.kcActivity as { clock: number[]; light: number[] })
    // Breathing is on: a non-zero amplitude, and frames drawn without any read.
    expect(uniforms.clock[2]).toBeGreaterThan(0)
    await waitFor(() => expect(g.oneFrames).toBeGreaterThan(0))
    read({ store: 'other-store', kind: 'recall', ids: ['bb'], ts: 1 })
    read({ store: 'member-a', kind: 'recall', ids: ['bb', 'not-drawn'], ts: 1 })
    await waitFor(() => expect(g.imageIndices[indexOf('bb')]).toBeGreaterThanOrEqual(0))
    // The hit at full strength; the frame for another store lit nothing of its own.
    expect(Math.abs(g.imageSizes[indexOf('bb')]) % 1).toBeGreaterThan(0.9)
  })

  it('neither breathes nor lights a read under reduced motion or with the tab hidden', async () => {
    motion.reduced = true
    renderTab()
    const g = await drawn()
    const clock = cameraClock()
    const uniforms = (g.config.kcActivity as { clock: number[]; light: number[] })
    expect(uniforms.clock[2]).toBe(0)
    expect(uniforms.light[2]).toBe(0)
    read({ store: 'member-a', kind: 'recall', ids: ['bb'], ts: 1 })
    await clock.frame(1000 / BREATH_FPS)
    expect(g.imageIndices.length).toBe(0)
  })

  it('stops while the tab is hidden and drops what was read then', async () => {
    renderTab()
    const g = await drawn()
    const clock = cameraClock()
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    onTestFinished(() => visibility.mockRestore())
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    const uniforms = (g.config.kcActivity as { clock: number[] })
    expect(uniforms.clock[2]).toBe(0)
    read({ store: 'member-a', kind: 'recall', ids: ['bb'], ts: 1 })
    await clock.frame(1000 / BREATH_FPS)
    expect(g.imageIndices.length).toBe(0)
    visibility.mockReturnValue('visible')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(uniforms.clock[2]).toBeGreaterThan(0)
    await clock.frame(1000 / BREATH_FPS)
    expect(g.imageIndices.length).toBe(0)
    visibility.mockRestore()
  })

  it('stops while the graph is off screen (scrolled away or its tab hidden) and starts again on it', async () => {
    const observers: IntersectionObserverCallback[] = []
    class FakeObserver { constructor(cb: IntersectionObserverCallback) { observers.push(cb) } observe() {} disconnect() {} }
    vi.stubGlobal('IntersectionObserver', FakeObserver)
    onTestFinished(() => { vi.unstubAllGlobals() })
    renderTab()
    const g = await drawn()
    const uniforms = (g.config.kcActivity as { clock: number[] })
    expect(uniforms.clock[2]).toBeGreaterThan(0)
    const seen = (on: boolean) => act(() => { for (const cb of observers) cb([{ isIntersecting: on } as IntersectionObserverEntry], {} as IntersectionObserver) })
    seen(false)
    expect(uniforms.clock[2]).toBe(0)
    read({ store: 'member-a', kind: 'recall', ids: ['bb'], ts: 1 })
    expect(g.imageIndices.length).toBe(0)
    seen(true)
    expect(uniforms.clock[2]).toBeGreaterThan(0)
  })

  it('hands the canvas only the clusters around the view, with links and lights on the right memories', async () => {
    const at = (id: string, x: number, cluster: number, links?: [string, number][]) => ({ ...node(id, 'fact', x, 0), cluster, links })
    const store = [at('l1', -1000, 0), at('l2', -996, 0), at('m1', -3, 1, [['m2', 0.9], ['r1', 0.95]]), at('m2', 3, 1), at('r1', 1000, 2), at('r2', 996, 2)]
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), totals: { fact: 6 },
      clusters: [{ id: 0, x: -998, y: 0, r: 6, count: 2, label: 'l' }, { id: 1, x: 0, y: 0, r: 6, count: 2, label: 'm' }, { id: 2, x: 998, y: 0, r: 6, count: 2, label: 'r' }],
      bounds: [-1010, -10, 1010, 10] } as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: store, cursor: null } as never)
    renderTab()
    const g = await drawn(6)
    const xs = () => Array.from({ length: g.positions.length / 2 }, (_, i) => g.positions[i * 2])
    // Zoomed in on the middle topic: the outer two leave the canvas once it settles.
    await zoomIn(g, 4)
    await waitFor(() => expect(xs()).toEqual([-3, 3]), { timeout: 3000 })
    // Its own link joins its two drawn memories; the link to an undrawn one is gone.
    await waitFor(() => expect([...g.links]).toEqual([0, 1]))
    read({ store: 'member-a', kind: 'recall', ids: ['m2'], ts: 1 })
    await waitFor(() => expect(g.imageIndices[1]).toBeGreaterThanOrEqual(0))
    expect(g.imageIndices).toHaveLength(2)
    // Zoomed back out past the drawn set: every topic is handed over again.
    const canvas = screen.getByTestId('memory-graph-canvas')
    fireEvent.keyDown(canvas, { key: '0' })
    await waitFor(() => expect(xs()).toHaveLength(6), { timeout: 3000 })
  })

  it.each([false, true])('keeps recall lights through a camera repick (changed subset: %s)', async changed => {
    const store = [-1000, -996, -3, 3, 996, 1000].map((x, i) => ({ ...node(`m${i}`, 'fact', x, 0), cluster: Math.floor(i / 2) }))
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), totals: { fact: 6 },
      clusters: [-998, 0, 998].map((x, id) => ({ id, x, y: 0, r: changed ? 6 : 1200, count: 2, label: `topic${id}` })),
      bounds: [-1010, -10, 1010, 10] } as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: store, cursor: null } as never)
    renderTab()
    const g = await drawn(6), clock = cameraClock()
    const canvas = screen.getByTestId('memory-graph-canvas')
    const xs = () => Array.from({ length: g.positions.length / 2 }, (_, i) => g.positions[i * 2])
    for (let i = 0; i < 4; i++) {
      fireEvent.keyDown(canvas, { key: '+' })
      await clock.frame(MOTION_MS)
      await clock.settle()
    }
    expect(xs()).toEqual(changed ? [-3, 3] : store.map(n => n.x))
    read({ store: 'member-a', kind: 'recall', ids: ['m3'], ts: 1 })
    await clock.frame(16)
    const oldIndex = xs().indexOf(3)
    const at = g.imageIndices[oldIndex], hold = g.imageSizes[oldIndex]
    expect(at).toBeGreaterThanOrEqual(0)
    const uploads = g.drawnCounts.length, activityUploads = g.activityUploads, colours = g.colorUploads
    const all = vi.spyOn(NodeCache.prototype, 'all')
    onTestFinished(() => all.mockRestore())
    const pan = async (x: number) => {
      act(() => { canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaX: x * g.view.k })) })
      await clock.frame(16)
      await clock.settle()
    }
    // Leave the previous pick's box. The middle memory stays drawn, at a new index if the left topic joins.
    await pan(-500)
    expect(all).toHaveBeenCalledTimes(1)
    expect(xs()).toEqual(changed ? [-1000, -996, -3, 3] : store.map(n => n.x))
    const nextIndex = xs().indexOf(3)
    expect(g.imageIndices[nextIndex]).toBe(at)
    expect(g.imageSizes[nextIndex]).toBe(hold)
    expect(g.drawnCounts.length).toBe(uploads + (changed ? 1 : 0))
    expect(g.activityUploads).toBe(activityUploads + (changed ? 1 : 0))
    expect(g.colorUploads).toBe(colours + (changed ? 1 : 0))
    if (changed) expect([...g.imageIndices].slice(0, 2)).toEqual([-1, -1])
    // Even an all-kept pick records its box: a nearby pan needs no further pick or upload.
    await pan(20)
    expect(all).toHaveBeenCalledTimes(1)
    expect(g.imageIndices[nextIndex]).toBe(at)
    expect(g.drawnCounts.length).toBe(uploads + (changed ? 1 : 0))
    expect(g.activityUploads).toBe(activityUploads + (changed ? 1 : 0))
  })

  it.each(['recall', 'session_start'])('lights an uncached memory at cluster zoom for %s', async kind => {
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({
      ...overview(250000),
      clusters: [
        { id: 7, x: -30, y: 0, r: 20, count: 125000, label: 'left' },
        { id: 9, x: 30, y: 0, r: 20, count: 125000, label: 'right' },
      ],
    } as never)
    renderTab()
    const g = await drawn(2)
    const clock = cameraClock()
    expect(api.memoryGraphNodes).not.toHaveBeenCalled()
    read({ store: 'other-store', kind, ids: ['aa'], ts: 1, layout: 'L1', clusters: { aa: 7 } })
    read({ store: 'member-a', kind, ids: ['bb'], ts: 1, layout: 'L1', clusters: { bb: 9 } })
    await clock.frame(1000 / BREATH_FPS)
    expect([...g.imageIndices].map(at => at >= 0)).toEqual([false, true])
    expect(g.imageSizes[1]).toBeGreaterThan(0)
    expect(api.memoryGraphNodes).not.toHaveBeenCalled()
  })

  it('does not apply cluster identities from a different layout', async () => {
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(overview(250000) as never)
    renderTab()
    const g = await drawn(1)
    const clock = cameraClock()
    read({ store: 'member-a', kind: 'recall', ids: ['bb'], ts: 1, layout: 'old-layout', clusters: { bb: 0 } })
    await clock.frame(1000 / BREATH_FPS)
    expect([...g.imageIndices]).toEqual([-1])
    expect(api.memoryGraphNodes).not.toHaveBeenCalled()
  })

  it('jumps straight to the lit set under reduced motion', async () => {
    motion.reduced = true
    vi.mocked(api.memoryGraphSearch).mockResolvedValue({ status: 'ready', layout: 'L1', matches: [{ id: 'bb', x: 50, y: 0, cluster: 0, score: 1 }], total: 1 } as never)
    renderTab()
    const g = await drawn()
    const solid = alphaOf(g, 'aa')
    g.colorHistory.length = 0
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'needle' } })
    await waitFor(() => expect(alphaOf(g, 'aa')).toBeCloseTo(DIM_ALPHA), { timeout: 3000 })
    expect(g.colorHistory.map(c => c[indexOf('aa') * 4 + 3]).every(a => Math.abs(a - solid) < 1e-6 || Math.abs(a - DIM_ALPHA) < 1e-6)).toBe(true)
  })

  it('counts a box selection on the server and hands the box itself to the Memories tab', async () => {
    vi.mocked(api.memoryGraphSelect).mockResolvedValue({ status: 'ready', layout: 'L1', count: 4321 } as never)
    const jump = renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    await waitFor(() => expect(api.memoryGraphSelect).toHaveBeenCalled())
    const [store, bbox] = vi.mocked(api.memoryGraphSelect).mock.calls[0]
    expect(store).toBe('member-a')
    expect(bbox[0]).toBeLessThan(bbox[2])
    const status = await screen.findByRole('status')
    fireEvent.click(within(status).getByRole('button'))
    expect(jump).toHaveBeenCalledWith([], undefined, { bbox: bbox.join(','), layout: 'L1', count: 4321, kinds: undefined })
  })

  it('clears a box selection on Escape anywhere on the page', async () => {
    vi.mocked(api.memoryGraphSelect).mockResolvedValue({ status: 'ready', layout: 'L1', count: 7 } as never)
    renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    expect(await screen.findByRole('status')).toBeTruthy()
    act(() => { document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    expect(screen.queryByTestId('memory-graph-box')).toBeNull()
  })

  it.each(['page', 'canvas'])('Escape on the %s cancels a drag before pointer-up', async target => {
    renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    expect(screen.getByTestId('memory-graph-box')).toBeTruthy()
    fireEvent.keyDown(target === 'page' ? document.body : canvas, { key: 'Escape' })
    expect(screen.queryByTestId('memory-graph-box')).toBeNull()
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    expect(api.memoryGraphSelect).not.toHaveBeenCalled()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it.each(['page', 'canvas'])('Escape on the %s drops a pending box count', async target => {
    let answer: (v: unknown) => void = () => {}
    vi.mocked(api.memoryGraphSelect).mockReturnValueOnce(new Promise(r => { answer = r }) as never)
    renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    expect(api.memoryGraphSelect).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(target === 'page' ? document.body : canvas, { key: 'Escape' })
    await act(async () => { answer({ status: 'ready', layout: 'L1', count: 99 }) })
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByTestId('memory-graph-box')).toBeNull()
  })

  it('moves keyboard focus between nodes with the arrows and opens one with Enter', async () => {
    const jump = renderTab()
    const g = await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    canvas.focus()
    fireEvent.keyDown(canvas, { key: 'ArrowLeft' })
    await waitFor(() => expect(g.config.focusedPointIndex).toBeDefined())
    const first = g.config.focusedPointIndex as number
    fireEvent.keyDown(canvas, { key: 'ArrowRight' })
    await waitFor(() => expect(g.config.focusedPointIndex).not.toBe(first))
    fireEvent.keyDown(canvas, { key: 'Enter' })
    const focused = NODES[g.config.focusedPointIndex as number]
    await screen.findByTestId('memory-record-detail')
    expect(vi.mocked(api.memoryRecords).mock.calls[0][1]).toEqual({ q: '', kind: 'all', filters: { ref: [focused.ref] } })
    expect(jump).not.toHaveBeenCalled()
  })

  it('shows clusters, not nodes, when the view holds more than the node budget', async () => {
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(overview(250000) as never)
    renderTab()
    await waitFor(() => expect(cosmos.instances.length).toBe(1), LAZY_INIT)
    await waitFor(() => expect(graph().positions.length).toBe(2), LAZY_INIT)
    expect(api.memoryGraphNodes).not.toHaveBeenCalled()
    // The topic is named over its cluster, in its colour, by name alone, on
    // lines of whole words, never cut.
    const name = (await screen.findByText('topic')).parentElement!
    expect(name.style.color).not.toBe('')
    expect(name.className).not.toContain('truncate')
    expect(name.className).toContain('font-semibold')
  })

  it('reads and draws a small selected kind at the overview of a large store', async () => {
    const large = { ...overview(100100), totals: { fact: 100000, lesson: 100 } }
    const lessons = Array.from({ length: 100 }, (_, i) => node(`lesson${i}`, 'lesson', i - 50, 0))
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(large as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: lessons, cursor: null } as never)
    renderTab()
    const g = await drawn(1)
    const view = { ...g.view }
    expect(api.memoryGraphNodes).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('radio', { name: /Lessons/ }))

    await drawn(100)
    expect(api.memoryGraphNodes).toHaveBeenCalledExactlyOnceWith('member-a', large.bounds, { kinds: ['lesson'], cursor: null })
    expect([...g.positions]).toEqual(lessons.flatMap(n => [n.x, n.y]))
    expect([...g.shapes]).toEqual(Array(100).fill(1))
    expect(g.view).toEqual(view)
  })

  it('waits for the first layout build, then draws', async () => {
    vi.mocked(api.memoryGraphOverview).mockResolvedValueOnce({ status: 'building' } as never)
    renderTab()
    await drawn()
    expect(vi.mocked(api.memoryGraphOverview).mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('keeps search and filters when the canvas cannot start', async () => {
    cosmos.failNext = true
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    renderTab()
    expect(await screen.findByRole('searchbox')).toBeTruthy()
    expect(screen.getAllByRole('radio').length).toBeGreaterThan(0)
    await waitFor(() => expect(warn).toHaveBeenCalled(), LAZY_INIT)
    expect(await screen.findByTestId('memory-graph-canvas-error', {}, LAZY_INIT)).toBeTruthy()
    warn.mockRestore()
  })

  it('shows a failed read with a retry instead of an empty graph', async () => {
    vi.mocked(api.memoryGraphOverview).mockRejectedValue(new Error('down'))
    renderTab()
    expect(await screen.findByTestId('memory-graph-load-error')).toBeTruthy()
    expect(cosmos.instances.length).toBe(0)
  })

  it('reads every page of a tile before drawing it', async () => {
    vi.mocked(api.memoryGraphNodes)
      .mockResolvedValueOnce({ status: 'ready', layout: 'L1', nodes: NODES.slice(0, 2), cursor: 'next' } as never)
      .mockResolvedValueOnce({ status: 'ready', layout: 'L1', nodes: NODES.slice(2), cursor: null } as never)
    renderTab()
    const g = await drawn()
    expect(vi.mocked(api.memoryGraphNodes).mock.calls[1][2]).toMatchObject({ cursor: 'next' })
    expect([...g.positions]).toEqual(NODES.flatMap(n => [n.x, n.y]))
  })

  it('reads a tile past the drawing budget and caches only its exhausted cursor', async () => {
    const cached = vi.spyOn(NodeCache.prototype, 'add')
    let finish: (value: unknown) => void = () => {}
    const last = new Promise(resolve => { finish = resolve })
    const pageCount = Math.ceil(NODE_BUDGET / PAGE_ROWS)
    // Repeated ids keep the draw small while exercising full-sized API pages.
    vi.mocked(api.memoryGraphNodes).mockImplementation((_store, _box, opts) => {
      const page = Number(opts?.cursor || 0)
      if (page === pageCount) return last as never
      return Promise.resolve({ status: 'ready', layout: 'L1', nodes: Array(PAGE_ROWS).fill(NODES[0]), cursor: String(page + 1) }) as never
    })
    try {
      renderTab()
      await waitFor(() => expect(api.memoryGraphNodes).toHaveBeenCalledTimes(pageCount + 1), LAZY_INIT)
      expect(cached).not.toHaveBeenCalled()
      await act(async () => { finish({ status: 'ready', layout: 'L1', nodes: [NODES[1]], cursor: null }) })
      await waitFor(() => expect(cached).toHaveBeenCalledTimes(1))
      expect(cached.mock.calls[0][1]).toHaveLength(pageCount * PAGE_ROWS + 1)
      expect(cached.mock.calls[0][1].at(-1)).toEqual(NODES[1])
      await drawn(2)
    } finally {
      finish({ status: 'ready', layout: 'L1', nodes: [], cursor: null })
      cached.mockRestore()
    }
  })

  it('stacks Fit above a separate vertical zoom group', async () => {
    renderTab()
    await drawn()
    const fit = screen.getByRole('button', { name: 'Fit to view' })
    const plus = screen.getByRole('button', { name: 'Zoom in' })
    const minus = screen.getByRole('button', { name: 'Zoom out' })
    const fitGroup = fit.parentElement!, zoomGroup = plus.parentElement!
    expect(fitGroup).not.toBe(zoomGroup)
    expect(minus.parentElement).toBe(zoomGroup)
    expect(zoomGroup.classList.contains('flex-col')).toBe(true)
    const controls = fitGroup.parentElement!
    expect(controls).toBe(zoomGroup.parentElement)
    expect(controls.classList.contains('flex-col')).toBe(true)
    expect(controls.classList.contains('gap-2')).toBe(true)
    expect([...controls.children]).toEqual([fitGroup, zoomGroup])
  })

  it('reports a failed node read and reads it again on Retry', async () => {
    vi.mocked(api.memoryGraphNodes).mockRejectedValueOnce(new Error('nodes down'))
    renderTab()
    const notice = await screen.findByTestId('memory-graph-read-error', {}, LAZY_INIT)
    fireEvent.click(within(notice).getByRole('button', { name: /Retry/ }))
    await drawn()
    expect(screen.queryByTestId('memory-graph-read-error')).toBeNull()
  })

  it('reports a failed search instead of dimming every memory', async () => {
    vi.mocked(api.memoryGraphSearch).mockRejectedValueOnce(new Error('search down'))
    renderTab()
    const g = await drawn()
    const solid = alphaOf(g, 'aa')
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'needle' } })
    const notice = await screen.findByTestId('memory-graph-read-error', {}, { timeout: 3000 })
    expect(alphaOf(g, 'aa')).toBe(solid)
    fireEvent.click(within(notice).getByRole('button', { name: /Retry/ }))
    await waitFor(() => expect(api.memoryGraphSearch).toHaveBeenCalledTimes(2))
  })

  it('reports a failed box count and counts it again on Retry', async () => {
    vi.mocked(api.memoryGraphSelect).mockRejectedValueOnce(new Error('select down')).mockResolvedValue({ status: 'ready', layout: 'L1', count: 7 } as never)
    renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    const notice = await screen.findByTestId('memory-graph-read-error')
    fireEvent.click(within(notice).getByRole('button', { name: /Retry/ }))
    await waitFor(() => expect(api.memoryGraphSelect).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.memoryGraphSelect).mock.calls[1][1]).toEqual(vi.mocked(api.memoryGraphSelect).mock.calls[0][1])
  })

  it('does not retry a failed box count after its drawn layout was replaced', async () => {
    vi.mocked(api.memoryGraphSelect).mockRejectedValueOnce(new Error('select down')).mockResolvedValue({ status: 'ready', layout: 'L2', count: 99 } as never)
    renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    const notice = await screen.findByTestId('memory-graph-read-error')
    const moved = NODES.map(n => ({ ...n, x: n.x + 5 }))
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), layout: 'L2' } as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L2', nodes: moved, cursor: null } as never)
    vi.mocked(api.memoryGraphSearch).mockResolvedValue({ status: 'ready', layout: 'L2', matches: [], total: 0 } as never)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'needle' } })
    await waitFor(() => expect([...graph().positions]).toEqual(moved.flatMap(n => [n.x, n.y])), LAZY_INIT)
    await act(async () => { fireEvent.click(within(notice).getByRole('button', { name: /Retry/ })) })
    expect(api.memoryGraphSelect).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/selected/)).toBeNull()
    expect(screen.queryByTestId('memory-graph-box')).toBeNull()
  })

  it('reads the nodes in view again on Refresh, even when the overview is the same', async () => {
    renderTab()
    await drawn()
    const before = vi.mocked(api.memoryGraphNodes).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(vi.mocked(api.memoryGraphNodes).mock.calls.length).toBeGreaterThan(before), { timeout: 3000 })
    await drawn()
  })

  it('stores a node reply under the filter it was asked for, not the one picked since', async () => {
    let answer: (v: unknown) => void = () => {}
    vi.mocked(api.memoryGraphNodes)
      .mockReturnValueOnce(new Promise(r => { answer = r }) as never)
      .mockResolvedValue({ status: 'ready', layout: 'L1', nodes: [NODES[0]], cursor: null } as never)
    renderTab()
    await waitFor(() => expect(api.memoryGraphNodes).toHaveBeenCalledTimes(1), LAZY_INIT)
    fireEvent.click(screen.getByRole('radio', { name: /Facts/ }))
    await act(async () => { answer({ status: 'ready', layout: 'L1', nodes: NODES, cursor: null }) })
    await waitFor(() => expect(vi.mocked(api.memoryGraphNodes).mock.calls.at(-1)?.[2]).toMatchObject({ kinds: ['fact'] }), LAZY_INIT)
    await drawn(1)
  })

  it('drops a box count that comes back after the filter changed', async () => {
    let answer: (v: unknown) => void = () => {}
    vi.mocked(api.memoryGraphSelect).mockReturnValueOnce(new Promise(r => { answer = r }) as never)
    renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    await waitFor(() => expect(api.memoryGraphSelect).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('radio', { name: /Facts/ }))
    await act(async () => { answer({ status: 'ready', layout: 'L1', count: 99 }) })
    expect(screen.queryByText(/99/)).toBeNull()
  })

  it('shows a failed box count as a blocking error', async () => {
    vi.mocked(api.memoryGraphSelect).mockRejectedValueOnce(new Error('select down'))
    renderTab()
    await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    const notice = await screen.findByTestId('memory-graph-read-error')
    expect(notice.getAttribute('role') ?? notice.querySelector('[role]')?.getAttribute('role')).toBe('alert')
  })

  it('draws each kind as its own shape and keys the kind pills with it', async () => {
    renderTab()
    const g = await drawn()
    expect([...g.shapes]).toEqual([0, 1, 2])
    for (const tab of ['fact', 'directive', 'episode']) expect(screen.getByTestId(`kind-mark-${tab}`).querySelector('svg')).toBeTruthy()
  })

  it('colours memories by their topic, not by their kind', async () => {
    renderTab()
    const g = await drawn()
    const rgb = (id: string) => [...g.colors.slice(indexOf(id) * 4, indexOf(id) * 4 + 3)]
    expect(rgb('aa')).toEqual(rgb('bb'))
    expect(rgb('aa')).toEqual(rgb('cc'))
  })

  it('clears the breath layer when a kind filter leaves no shown memories', async () => {
    motion.reduced = true
    const layer = { capture: vi.fn(), draw: vi.fn(), clear: vi.fn(), dispose: vi.fn(), hold: vi.fn(), release: vi.fn() }
    const create = vi.spyOn(BreathLayer, 'create').mockReturnValue(layer as unknown as BreathLayer)
    onTestFinished(() => create.mockRestore())
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: [NODES[0]], cursor: null } as never)
    renderTab()
    const g = await drawn(1)
    const source = document.createElement('canvas')
    Object.assign(g, { canvas: source })
    const afterDraw = g.config.kcAfterDraw as () => void
    act(afterDraw)
    expect(layer.capture).toHaveBeenCalledExactlyOnceWith(source)
    expect(layer.draw).toHaveBeenCalled()
    layer.draw.mockClear()
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes: [], cursor: null } as never)
    fireEvent.click(screen.getByRole('radio', { name: /Lessons/ }))
    await drawn(0)
    expect(g.links).toHaveLength(0)
    act(afterDraw)
    expect(layer.clear).toHaveBeenCalledOnce()
    expect(layer.capture).toHaveBeenCalledTimes(1)
    expect(layer.draw).not.toHaveBeenCalled()
  })

  it('disposes the breath layer when the WebGL device fails after the canvas starts', async () => {
    const canvas = document.createElement('canvas')
    const dispose = vi.fn(() => canvas.remove())
    const create = vi.spyOn(BreathLayer, 'create').mockImplementation(host => {
      host.appendChild(canvas)
      return { dispose, draw: vi.fn() } as unknown as BreathLayer
    })
    onTestFinished(() => create.mockRestore())
    cosmos.deviceFails = true
    renderTab()
    expect(await screen.findByTestId('memory-graph-canvas-error', {}, LAZY_INIT)).toBeTruthy()
    expect(create).toHaveBeenCalledOnce()
    expect(dispose).toHaveBeenCalledOnce()
    expect(canvas.isConnected).toBe(false)
    expect(screen.getByRole('searchbox')).toBeTruthy()
  })

  it('pans and zooms over the clusters a Refresh brought, not the first ones', async () => {
    renderTab()
    await drawn()
    const bigger = { ...overview(250000), clusters: [{ id: 0, x: 10, y: 5, r: 60, count: 250000, label: 'topic' }] }
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(bigger as never)
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect([...graph().positions]).toEqual([10, 5]), LAZY_INIT)
    const canvas = screen.getByTestId('memory-graph-canvas')
    const before = graph().transforms
    act(() => { canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -20, ctrlKey: true, clientX: 50, clientY: 50 })) })
    // The frame that applies the zoom also decides clusters or nodes, from the clusters it knows.
    await waitFor(() => expect(graph().transforms).toBeGreaterThan(before))
    expect([...graph().positions]).toEqual([10, 5])
  })

  it('keeps the drawn graph when a Refresh fails, and Retry brings it back', async () => {
    renderTab()
    const g = await drawn()
    vi.mocked(api.memoryGraphOverview).mockRejectedValueOnce(new Error('overview down'))
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    const notice = await screen.findByTestId('memory-graph-load-error', {}, { timeout: 3000 })
    expect(screen.getByTestId('memory-graph-canvas')).toBeTruthy()
    fireEvent.click(within(notice).getByRole('button', { name: /Retry/ }))
    await waitFor(() => expect(screen.queryByTestId('memory-graph-load-error')).toBeNull(), { timeout: 3000 })
    // The same renderer still draws, attached to the canvas on the page.
    expect(cosmos.instances.at(-1)).toBe(g)
    expect(screen.getByTestId('memory-graph-canvas').isConnected).toBe(true)
  })

  it('reads everything again when a reply comes from a newer build of the layout', async () => {
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), layout: 'L1' } as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', nodes: NODES, cursor: null, layout: 'L1' } as never)
    renderTab()
    await drawn()
    const moved = NODES.map(n => ({ ...n, x: n.x + 5 }))
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), layout: 'L2' } as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', nodes: moved, cursor: null, layout: 'L2' } as never)
    vi.mocked(api.memoryGraphSelect).mockResolvedValue({ status: 'ready', count: 77, layout: 'L2' } as never)
    const canvas = screen.getByTestId('memory-graph-canvas')
    pointer('pointerdown', canvas, { button: 0, clientX: 10, clientY: 10 })
    pointer('pointermove', window, { clientX: 200, clientY: 150 })
    pointer('pointerup', window, { clientX: 200, clientY: 150 })
    await waitFor(() => expect([...graph().positions]).toEqual(moved.flatMap(n => [n.x, n.y])), LAZY_INIT)
    expect(screen.queryByText(/77/)).toBeNull()
  })

  it('keeps the cluster drawn until its memories arrive, then opens them from it', async () => {
    let answer: (v: unknown) => void = () => {}
    vi.mocked(api.memoryGraphNodes).mockReturnValueOnce(new Promise(r => { answer = r }) as never)
    renderTab()
    await waitFor(() => expect(api.memoryGraphNodes).toHaveBeenCalledTimes(1), LAZY_INIT)
    // The one cluster, at its centre, while the tile is still loading.
    expect([...graph().positions]).toEqual([0, 0])
    await act(async () => { answer({ status: 'ready', layout: 'L1', nodes: NODES, cursor: null }) })
    const g = await drawn()
    expect(g.renders.at(-1)).toBeGreaterThan(0)
  })

  it('reads the tiles in the middle of the view first, and zoomed in shows the first one that arrives', async () => {
    motion.reduced = true
    // A store read whole in 16 tiles; only the one at the top right of the middle answers.
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(overview(5000) as never)
    const near = [node('n1', 'fact', 5, 5), node('n2', 'lesson', 10, 8)]
    vi.mocked(api.memoryGraphNodes).mockImplementation((_s, bbox) => (bbox[0] === 0 && bbox[1] === 0
      ? Promise.resolve({ status: 'ready', layout: 'L1', nodes: near, cursor: null })
      : new Promise(() => {})) as never)
    renderTab()
    await waitFor(() => expect(api.memoryGraphNodes).toHaveBeenCalledTimes(4), LAZY_INIT)
    // The four tiles that meet at the middle of the view went out first.
    for (const [, bbox] of vi.mocked(api.memoryGraphNodes).mock.calls.slice(0, 4)) {
      expect(bbox[0] <= 0 && bbox[2] >= 0 && bbox[1] <= 0 && bbox[3] >= 0).toBe(true)
    }
    // At the overview the cluster stays until the view's tiles are all in.
    expect([...graph().positions]).toEqual([0, 0])
    const canvas = screen.getByTestId('memory-graph-canvas')
    for (let i = 0; i < 8; i++) fireEvent.keyDown(canvas, { key: '+' })
    await waitFor(() => expect([...graph().positions]).toEqual([5, 5, 10, 8]))
  })

  it('resizes and recolours the marks together during a zoom, and once more when it settles', async () => {
    motion.reduced = true
    renderTab()
    const g = await drawn()
    const clock = cameraClock()
    const colours = g.colorUploads, sizes = g.sizeUploads, k0 = g.view.k
    fireEvent.keyDown(screen.getByTestId('memory-graph-canvas'), { key: '-' })
    await clock.frame(16)
    expect(g.view.k).toBeLessThan(k0)
    expect(g.sizeUploads).toBeGreaterThan(sizes)
    expect(g.colorUploads).toBe(colours + 1)
    await clock.settle()
    expect(g.colorUploads).toBe(colours + 2)
  })

  it('keeps every drawn memory on the canvas through a pan and a zoom, reading nothing again', async () => {
    renderTab()
    const g = await drawn()
    const reads = vi.mocked(api.memoryGraphNodes).mock.calls.length
    const draws = g.drawnCounts.length
    const canvas = screen.getByTestId('memory-graph-canvas')
    const wheel = (init: WheelEventInit) => act(() => { canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: 50, clientY: 50, ...init })) })
    for (let i = 0; i < 6; i++) {
      const before = g.transforms
      wheel({ deltaX: 30, deltaY: 10 })
      await waitFor(() => expect(g.transforms).toBeGreaterThan(before))
    }
    for (let i = 0; i < 3; i++) {
      const before = g.transforms
      wheel({ deltaY: -40, ctrlKey: true })
      await waitFor(() => expect(g.transforms).toBeGreaterThan(before))
    }
    // Nothing redrew with fewer points, and no tile was read a second time.
    expect(g.drawnCounts.slice(draws).every(n => n === NODES.length)).toBe(true)
    expect(vi.mocked(api.memoryGraphNodes).mock.calls.length).toBe(reads)
    expect(g.positions.length).toBe(NODES.length * 2)
  })

  it('animates a button zoom through the frames between, ending where it was going', async () => {
    renderTab()
    const g = await drawn()
    const clock = cameraClock()
    const k0 = g.view.k
    g.ks.length = 0
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    await clock.frame(50)
    expect(g.view.k).toBeGreaterThan(k0)
    expect(g.view.k).toBeLessThan(k0 * 1.6)
    await clock.frame(50)
    const between = [...g.ks]
    expect(between).toHaveLength(2)
    expect([...between].sort((a, b) => a - b)).toEqual(between)
    await clock.frame(150)
    expect(g.view.k).toBeCloseTo(k0 * 1.6, 5)
    await clock.settle()
  })

  it.each([
    { reduced: false, titlesFit: false, topicX: 0 }, { reduced: true, titlesFit: false, topicX: 0 },
    { reduced: false, titlesFit: true, topicX: 0 }, { reduced: true, titlesFit: true, topicX: 0 },
    { reduced: false, titlesFit: false, topicX: 130 },
  ])('keeps labels on every frame of button zooms and after settling (reduced $reduced, titles fit $titlesFit, topic x $topicX)', async ({ reduced, titlesFit, topicX }) => {
    motion.reduced = reduced
    // Cross the dense/sparse boundary: the initial 400 dots name their topic.
    // Short titles replace it; titles too long to fit still need the topic.
    const nodes = Array.from({ length: 400 }, (_, i) => ({ ...node(`e${i}`, 'episode', topicX + (i % 20) * 6 - 57, Math.floor(i / 20) * 6 - 57), label: titlesFit ? `Memory ${i}` : 'A memory whose entire first clause is much too long to fit over two short lines on this canvas' }))
    const summary = overview(400)
    if (topicX) { summary.clusters[0].x = topicX; summary.bounds = [-190, -60, 190, 60] }
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(summary as never)
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes, cursor: null } as never)
    renderTab()
    const g = await drawn(400)
    const layer = screen.getByTestId('memory-graph-labels')
    await waitFor(() => expect(layer.children.length).toBeGreaterThan(0))
    const clock = cameraClock()
    const present = () => {
      expect(layer.children.length).toBeGreaterThan(0)
      expect([...layer.children].some(el => Number((el as HTMLElement).style.opacity || 1) > 0)).toBe(true)
    }
    for (const direction of ['Zoom in', 'Zoom out']) {
      const k = g.view.k
      const old = [...layer.children]
      fireEvent.click(screen.getByRole('button', { name: direction }))
      present()
      for (let frame = 0; frame < 25; frame++) {
        await clock.frame(10)
        present()
        // The old set is held, not put into an exit animation at the first frame.
        if (!reduced || frame < 12) expect([...layer.children]).toEqual(old)
      }
      expect(g.view.k).toBeCloseTo(direction === 'Zoom in' ? k * 1.6 : k / 1.6, 5)
      // Deliver every frame through the settle and the label crossfade too.
      for (let frame = 0; frame < 40; frame++) {
        await clock.frame(10)
        present()
        if (titlesFit && frame === 15) {
          const incoming = [...layer.children].filter(el => !old.includes(el)) as HTMLElement[]
          expect(incoming.length).toBeGreaterThan(0)
          const opacity = Number(incoming[0].style.opacity)
          if (reduced) expect(opacity).toBe(1)
          else { expect(opacity).toBeGreaterThan(0); expect(opacity).toBeLessThan(1) }
        }
      }
      if (titlesFit) expect([...layer.children]).not.toEqual(old)
      present()
    }
  })

  it('names the topic when a sparse view has no whole title that fits', async () => {
    const nodes = NODES.map(n => ({ ...n, kind: 'episode', label: 'word '.repeat(80) }))
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes, cursor: null } as never)
    renderTab()
    await drawn()
    const clock = cameraClock()
    for (let frame = 0; frame < 5; frame++) await clock.frame(100)
    expect(screen.getByTestId('memory-graph-labels').textContent).toBe('topic')
  })

  it('wraps hover labels inside the canvas and omits incomplete or oversized text', async () => {
    const nodes = [
      { ...node('edge', 'episode', 125, 0), label: 'After the ACP repair the graph is readable again after zooming' },
      { ...node('cut', 'episode', 0, 0), label: 'After the ACP repai…' },
      { ...node('long', 'episode', 0, 0), label: 'word '.repeat(80) },
    ]
    vi.mocked(api.memoryGraphNodes).mockResolvedValue({ status: 'ready', layout: 'L1', nodes, cursor: null } as never)
    renderTab()
    const g = await drawn()
    act(() => g.config.onPointMouseOver!(0))
    const hover = screen.getByTestId('memory-graph-hover')
    expect([...hover.children].map(c => c.textContent).join(' ')).toBe(nodes[0].label)
    expect(hover.children).toHaveLength(2)
    expect(parseFloat(hover.style.left) + parseFloat(hover.style.width)).toBeLessThanOrEqual(800)
    expect(parseFloat(hover.style.top) + parseFloat(hover.style.height)).toBeLessThanOrEqual(420)
    expect(parseFloat(hover.style.left)).toBeGreaterThanOrEqual(0)
    expect(parseFloat(hover.style.top)).toBeGreaterThanOrEqual(0)
    for (const el of [hover, ...hover.querySelectorAll('span')]) {
      expect(el.className).not.toMatch(/truncate|line-clamp|text-ellipsis/)
      expect(getComputedStyle(el).textOverflow).not.toBe('ellipsis')
      expect(el.textContent).not.toContain('…')
    }
    for (const index of [1, 2]) {
      act(() => g.config.onPointMouseOver!(index))
      expect(screen.queryByTestId('memory-graph-hover')).toBeNull()
    }
  })

  it('zooms in, out and back to the whole store from its buttons and keys', async () => {
    motion.reduced = true
    renderTab()
    const g = await drawn()
    const k0 = g.view.k
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    await waitFor(() => expect(g.view.k).toBeGreaterThan(k0))
    const kIn = g.view.k
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }))
    await waitFor(() => expect(g.view.k).toBeLessThan(kIn))
    const canvas = screen.getByTestId('memory-graph-canvas')
    fireEvent.keyDown(canvas, { key: '+' })
    await waitFor(() => expect(g.view.k).toBeGreaterThan(k0))
    fireEvent.click(screen.getByRole('button', { name: 'Fit to view' }))
    await waitFor(() => expect(g.view.k).toBeCloseTo(k0, 5))
  })

  const finger = (type: string, target: EventTarget, x: number, y: number) => {
    const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y })
    Object.assign(event, { pointerId: 7, pointerType: 'touch' })
    act(() => { target.dispatchEvent(event) })
  }

  it('keeps each name on its place in every frame of a one-finger pan, and keeps the same names', async () => {
    renderTab()
    const g = await drawn()
    await zoomIn(g, 3)
    const layer = screen.getByTestId('memory-graph-labels')
    await waitFor(() => expect(layer.children.length).toBeGreaterThan(0), LAZY_INIT)
    const clock = cameraClock()
    // The zoom's flight ends and its names are placed before the finger lands.
    for (let i = 0; i < 60; i++) await clock.frame(16)
    await clock.settle()
    for (let i = 0; i < 10; i++) await clock.frame(16)
    const names = [...layer.children] as HTMLElement[]
    expect(names.length).toBeGreaterThan(0)
    const start = { ...g.view }
    const canvas = screen.getByTestId('memory-graph-canvas')
    finger('pointerdown', canvas, 300, 200)
    for (let i = 1; i <= 8; i++) {
      finger('pointermove', window, 300 - 9 * i, 200 + 4 * i)
      await clock.frame(16)
      // Shorter than a settle apart, as a phone sends them: nothing is re-chosen.
      expect([...layer.children]).toEqual(names)
      for (const el of names) {
        const [x, y] = el.style.translate.split(' ').map(parseFloat)
        expect(x).toBeCloseTo(g.view.x - start.x, 5)
        expect(y).toBeCloseTo(g.view.y - start.y, 5)
      }
    }
    expect(Math.hypot(g.view.x - start.x, g.view.y - start.y)).toBeGreaterThan(40)
    finger('pointerup', window, 228, 232)
  })

  it('shows the live canvas, not the breath layer’s copy, through every frame of a pan', async () => {
    const layer = { capture: vi.fn(), draw: vi.fn(), clear: vi.fn(), dispose: vi.fn(), hold: vi.fn(), release: vi.fn() }
    const create = vi.spyOn(BreathLayer, 'create').mockReturnValue(layer as unknown as BreathLayer)
    onTestFinished(() => create.mockRestore())
    renderTab()
    const g = await drawn()
    await zoomIn(g, 3)
    const clock = cameraClock()
    await clock.settle()
    for (let i = 0; i < 4; i++) await clock.frame(16)
    layer.hold.mockClear(); layer.release.mockClear()
    const canvas = screen.getByTestId('memory-graph-canvas')
    finger('pointerdown', canvas, 300, 200)
    for (let i = 1; i <= 6; i++) {
      const held = layer.hold.mock.calls.length
      finger('pointermove', window, 300 - 9 * i, 200)
      await clock.frame(16)
      // Each moved frame holds the layer before cosmos.gl draws it; none releases it.
      expect(layer.hold.mock.calls.length).toBeGreaterThan(held)
      expect(layer.release).not.toHaveBeenCalled()
    }
    finger('pointerup', window, 246, 200)
    for (let i = 0; i < 10; i++) await clock.frame(16)
    await clock.settle()
    for (let i = 0; i < 10; i++) await clock.frame(16)
    expect(layer.release).toHaveBeenCalled()
    expect(layer.release.mock.invocationCallOrder.at(-1)!).toBeGreaterThan(layer.hold.mock.invocationCallOrder.at(-1)!)
  })

  it('names no memory under a finger, and still names one under a mouse', async () => {
    renderTab()
    const g = await drawn()
    const canvas = screen.getByTestId('memory-graph-canvas')
    finger('pointerdown', canvas, 300, 200)
    act(() => g.config.onPointMouseOver!(0))
    expect(screen.queryByTestId('memory-graph-hover')).toBeNull()
    finger('pointerup', window, 300, 200)
    pointer('pointermove', canvas, { clientX: 300, clientY: 200 })
    act(() => g.config.onPointMouseOver!(0))
    expect(screen.getByTestId('memory-graph-hover')).toBeTruthy()
  })

  it('keeps every topic name whose cluster stays in view through a pan and its settle', async () => {
    // A 6 x 4 grid of big clusters: the overview names a few of them.
    const clusters = Array.from({ length: 24 }, (_, i) => ({ id: i, x: (i % 6) * 40 - 100, y: Math.floor(i / 6) * 40 - 60, r: 18, count: 20000, label: `Topic ${String.fromCharCode(65 + i)}` }))
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(480000), clusters, bounds: [-120, -80, 120, 80] } as never)
    renderTab()
    const g = await drawn(24)
    const layer = screen.getByTestId('memory-graph-labels')
    await waitFor(() => expect(layer.children.length).toBeGreaterThan(2), LAZY_INIT)
    const clock = cameraClock()
    // Past the names' fade-in.
    for (let i = 0; i < 30; i++) await clock.frame(16)
    const names = () => [...layer.children].filter(el => Number((el as HTMLElement).style.opacity || 1) > 0.99).map(el => el.textContent!)
    let before = names()
    const canvas = screen.getByTestId('memory-graph-canvas')
    for (const [dx, dy] of [[-37, 13], [29, -21], [-23, -17]]) {
      const at = { ...g.view }
      finger('pointerdown', canvas, 400, 210)
      for (let i = 1; i <= 6; i++) { finger('pointermove', window, 400 + dx * i / 6, 210 + dy * i / 6); await clock.frame(16) }
      finger('pointerup', window, 400 + dx, 210 + dy)
      await clock.settle()
      for (let i = 0; i < 30; i++) await clock.frame(16)
      expect(Math.hypot(g.view.x - at.x, g.view.y - at.y)).toBeGreaterThan(10)
      // Each name shown before is still shown while its cluster is on screen.
      const shown = names()
      // The camera back from cosmos.gl's transform (MemoryGraphTab's applyView), centred on its 4096 space.
      const w = 800, h = 420, o = 2048, { k, x, y } = g.view
      const cx = (w / 2 - x + k * o - (k * w) / 2) / k, cy = (y - h / 2 + k * o + (k * h) / 2) / k
      let kept = 0
      for (const name of before) {
        const c = clusters.find(c => c.label === name)!
        const sx = w / 2 + (c.x - cx) * k, sy = h / 2 - (c.y - cy) * k
        if (sx < 60 || sx > w - 60 || sy < 40 || sy > h - 40) continue
        expect(shown).toContain(name)
        kept++
      }
      expect(kept).toBeGreaterThan(0)
      before = shown
    }
  })

  it('inks the marks as it sizes them through a zoom, so the look does not jump when it stops', async () => {
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), clusters: [{ id: 0, x: 0, y: 0, r: 0.3, count: 3, label: 'topic' }] } as never)
    renderTab()
    const g = await drawn()
    const clock = cameraClock()
    for (let i = 0; i < 10; i++) await clock.frame(16)
    // The facts' ink share: the most solid mark drawn.
    const ink = () => Math.max(...[...g.colors].filter((_, i) => i % 4 === 3))
    const start = ink(), uploads = g.colorUploads, k = g.view.k
    // Three button zooms, frames PAINT_MS apart, each past the camera's ease, and
    // all shorter than a settle apart: one zoom that has not stopped.
    for (let z = 0; z < 3; z++) {
      fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
      for (let i = 0; i < 3; i++) await clock.frame(100)
    }
    expect(g.view.k).toBeCloseTo(k * 1.6 ** 3, 3)
    // Mid-move the colours already follow the zoom, not only the sizes.
    expect(g.colorUploads).toBeGreaterThan(uploads)
    const during = ink()
    expect(during).toBeGreaterThan(start)
    await clock.settle()
    for (let i = 0; i < 10; i++) await clock.frame(16)
    expect(ink()).toBeCloseTo(during, 5)
  })

  it('holds clusters or memories for a whole pinch, either way, and decides once it settles', async () => {
    // Above the node budget with the whole store in view, under it zoomed in.
    vi.mocked(api.memoryGraphOverview).mockResolvedValue(overview(75000) as never)
    renderTab()
    const g = await drawn(1)
    const clock = cameraClock()
    for (let i = 0; i < 10; i++) await clock.frame(16)
    const canvas = screen.getByTestId('memory-graph-canvas')
    const two = (type: string, target: EventTarget, spread: number) => {
      for (const [id, x] of [[7, 400 - spread], [8, 400 + spread]]) {
        const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: 210 })
        Object.assign(event, { pointerId: id, pointerType: 'touch' })
        act(() => { target.dispatchEvent(event) })
      }
    }
    const pinch = async (from: number, to: number) => {
      two('pointerdown', canvas, from)
      for (let i = 1; i <= 8; i++) {
        two('pointermove', window, from + (to - from) * i / 8)
        await clock.frame(16)
        // The level never changes under the fingers.
        expect(g.positions.length).toBe(level * 2)
      }
      two('pointerup', window, to)
    }
    let level = 1
    const k0 = g.view.k
    await pinch(20, 100)
    expect(g.view.k).toBeGreaterThan(k0 * 4)
    // Settled: the clusters open into their memories once.
    await clock.settle()
    for (let i = 0; i < 40 && g.positions.length !== NODES.length * 2; i++) await clock.frame(50)
    expect(g.positions.length).toBe(NODES.length * 2)
    level = NODES.length
    await pinch(100, 20)
    expect(g.view.k).toBeLessThan(k0 * 1.5)
    await clock.settle()
    for (let i = 0; i < 20; i++) await clock.frame(16)
    expect(g.positions.length).toBe(2)
  })

  it('draws a crowded cluster light with the whole store in view and more solid zoomed in', async () => {
    motion.reduced = true
    vi.mocked(api.memoryGraphOverview).mockResolvedValue({ ...overview(), clusters: [{ id: 0, x: 0, y: 0, r: 0.3, count: 3, label: 'topic' }] } as never)
    renderTab()
    const g = await drawn()
    const crowded = alphaOf(g, 'aa')
    expect(crowded).toBeLessThan(0.5)
    for (let i = 0; i < 5; i++) fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    await waitFor(() => expect(alphaOf(g, 'aa')).toBeGreaterThan(crowded))
  })
})
