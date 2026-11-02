/**
 * The memory graph's sigma wiring: the themed hover pill, the node reducer that
 * dims by group and by search text, node and stage clicks, the group filter and
 * the All button, and the theme observer that recolours the live graph. sigma is
 * replaced by a recorder so each handler can be fired directly; graphology and
 * d3 run for real so the node and edge data is the one the tab builds.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

interface FakeSigma {
  graph: { order: number; size: number; getNodeAttribute(node: string, attr: string): unknown }
  options: {
    nodeReducer: (key: string, data: Record<string, unknown>) => Record<string, unknown>
    defaultDrawNodeHover: (
      ctx: CanvasRenderingContext2D,
      data: { x: number; y: number; size: number; label: string | null; color: string },
      settings: { labelSize: number; labelFont: string; labelWeight: string },
    ) => void
  }
  handlers: Record<string, (event: unknown) => void>
  kill: Mock
  refresh: Mock
  setSetting: Mock
}

const sigmaState = vi.hoisted(() => ({ instances: [] as FakeSigma[] }))

vi.mock('sigma', () => ({
  default: class RecordingSigma {
    graph: unknown
    options: unknown
    handlers: Record<string, (event: unknown) => void> = {}
    kill = vi.fn()
    refresh = vi.fn()
    setSetting = vi.fn()
    constructor(graph: unknown, _container: unknown, options: unknown) {
      this.graph = graph
      this.options = options
      sigmaState.instances.push(this as unknown as FakeSigma)
    }
    on(event: string, handler: (event: unknown) => void) {
      this.handlers[event] = handler
    }
    getCamera() {
      return { on() {} }
    }
  },
}))
vi.mock('../api/client', () => ({ api: { memoryGraph: vi.fn() } }))

import { api } from '../api/client'
import MemoryGraphTab from '../pages/overview/MemoryGraphTab'

const GRAPH = {
  nodes: [
    { id: 'n1', label: 'project.alpha', group: 'semantic', title: 'project.alpha = 1' },
    { id: 'n2', label: 'lesson one', group: 'lesson', title: 'Use tabs' },
    { id: 'n3', label: 'pref one', group: 'preference', title: 'Prefers dark mode' },
  ],
  edges: [
    { from: 'n1', to: 'n2' },
    { from: 'n2', to: 'n3' },
  ],
}

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryGraphTab />
    </QueryClientProvider>,
  )
}

// Chain: the api.memoryGraph query resolves -> the build effect runs -> import('d3')
// resolves and the force layout settles -> new Sigma. Bounded by this rather than
// RTL's 1000 ms default, which a loaded coverage shard can exceed.
const SIGMA_CHAIN_TIMEOUT_MS = 5000

// Chain: typing in the search box -> setSearchImmediate -> the tab's 300 ms
// debounce timer -> setSearch -> the filter effect writes the search text the
// node reducer reads.
const SEARCH_DEBOUNCE_TIMEOUT_MS = 5000

const THEME_VARS = ['--bg-elevated', '--border', '--text']

async function mountedSigma(): Promise<FakeSigma> {
  await waitFor(() => expect(sigmaState.instances.length).toBeGreaterThan(0), { timeout: SIGMA_CHAIN_TIMEOUT_MS })
  return sigmaState.instances[sigmaState.instances.length - 1]
}

beforeEach(() => {
  sigmaState.instances.length = 0
  vi.mocked(api.memoryGraph).mockResolvedValue(GRAPH as never)
})

afterEach(() => {
  document.documentElement.removeAttribute('data-theme')
  for (const name of THEME_VARS) document.documentElement.style.removeProperty(name)
  vi.clearAllMocks()
})

describe('MemoryGraphTab sigma wiring', () => {
  it('builds one graph node per memory node and one edge per link, coloured by group', async () => {
    renderTab()
    const sigma = await mountedSigma()
    expect(sigma.graph.order).toBe(3)
    expect(sigma.graph.size).toBe(2)
    expect(sigma.graph.getNodeAttribute('n1', 'color')).toBe('#a855f7')
    expect(sigma.graph.getNodeAttribute('n2', 'color')).toBe('#f97316')
  })

  it('shows the clicked node in the detail panel and clears it on a stage click', async () => {
    renderTab()
    const sigma = await mountedSigma()
    act(() => sigma.handlers.clickNode({ node: 'n2' }))
    expect(await screen.findByText('Use tabs')).toBeInTheDocument()
    act(() => sigma.handlers.clickStage({}))
    await waitFor(() => expect(screen.queryByText('Use tabs')).toBeNull())
  })

  it('dims nodes outside the chosen group and restores them on All', async () => {
    renderTab()
    const sigma = await mountedSigma()
    const allButton = screen.getByRole('button', { name: /^All/ })
    const semanticButton = screen.getByRole('button', { name: /^Semantic/ })
    fireEvent.click(semanticButton)
    await waitFor(() => expect(sigma.refresh).toHaveBeenCalled())
    expect(sigma.options.nodeReducer('n2', { group: 'lesson', label: 'lesson one', color: '#f97316' }))
      .toMatchObject({ color: 'rgba(120,120,120,0.12)', label: '' })
    expect(sigma.options.nodeReducer('n1', { group: 'semantic', label: 'project.alpha', color: '#a855f7' }))
      .toMatchObject({ color: '#a855f7', label: 'project.alpha' })

    fireEvent.click(allButton)
    expect(sigma.options.nodeReducer('n2', { group: 'lesson', label: 'lesson one', color: '#f97316' }))
      .toMatchObject({ color: '#f97316', label: 'lesson one' })
  })

  it('dims nodes whose label lacks the typed search text, ignoring case', async () => {
    renderTab()
    const sigma = await mountedSigma()
    fireEvent.change(screen.getByLabelText('Search memory nodes'), { target: { value: 'LESSON' } })
    // The reducer reads the search text only after the debounce has applied it, so the
    // dimmed non-match is the positive signal that the text arrived.
    await waitFor(
      () => expect(sigma.options.nodeReducer('n1', { group: 'semantic', label: 'project.alpha', color: '#a855f7' }))
        .toMatchObject({ color: 'rgba(120,120,120,0.12)', label: '' }),
      { timeout: SEARCH_DEBOUNCE_TIMEOUT_MS },
    )
    expect(sigma.options.nodeReducer('n2', { group: 'lesson', label: 'lesson one', color: '#f97316' }))
      .toMatchObject({ color: '#f97316', label: 'lesson one' })
  })

  it('draws the themed hover pill with the node dot and label text', async () => {
    document.documentElement.style.setProperty('--bg-elevated', '#101820')
    document.documentElement.style.setProperty('--border', '#334455')
    document.documentElement.style.setProperty('--text', '#f5f0e6')
    renderTab()
    const sigma = await mountedSigma()
    const fills: unknown[] = []
    const strokes: unknown[] = []
    const ctx = {
      font: '',
      fillStyle: '' as unknown,
      strokeStyle: '' as unknown,
      lineWidth: 0,
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      arcTo: vi.fn(),
      closePath: vi.fn(),
      fill: vi.fn(() => {
        fills.push(ctx.fillStyle)
      }),
      stroke: vi.fn(() => {
        strokes.push(ctx.strokeStyle)
      }),
      arc: vi.fn(),
      fillText: vi.fn(),
      measureText: vi.fn(() => ({ width: 30 })),
    }
    sigma.options.defaultDrawNodeHover(
      ctx as unknown as CanvasRenderingContext2D,
      { x: 50, y: 40, size: 4, label: 'alpha', color: '#22c55e' },
      { labelSize: 11, labelFont: 'sans-serif', labelWeight: '400' },
    )
    expect(ctx.font).toBe('400 11px sans-serif')
    expect(ctx.arcTo).toHaveBeenCalledTimes(4)
    // The pill is filled with --bg-elevated and outlined with --border; the dot is then
    // filled with the node colour.
    expect(fills).toEqual(['#101820', '#22c55e'])
    expect(strokes).toEqual(['#334455'])
    expect(ctx.arc).toHaveBeenCalledWith(50, 40, 4, 0, Math.PI * 2)
    // The label is painted last, in --text.
    expect(ctx.fillStyle).toBe('#f5f0e6')
    expect(ctx.fillText).toHaveBeenCalledWith('alpha', 59, expect.closeTo(43.67, 2))
  })

  it('recolours the live graph when the theme attribute changes', async () => {
    renderTab()
    const sigma = await mountedSigma()
    document.documentElement.style.setProperty('--text', '#c0ffee')
    document.documentElement.setAttribute('data-theme', 'dark')
    await waitFor(() => expect(sigma.setSetting).toHaveBeenCalledWith('labelColor', { color: '#c0ffee' }))
    expect(sigma.refresh).toHaveBeenCalled()
  })
})
