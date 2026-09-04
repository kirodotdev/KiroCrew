import { beforeEach, describe, it, expect, vi } from 'vitest'
import { act, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { mockMemoryGraph } from './mocks/server'

const memoryGraph = vi.hoisted(() => vi.fn())
const sigmaConstructorFailure = vi.hoisted(() => ({ current: null as Error | null }))
const sigmaInstances = vi.hoisted(() => [] as Array<{
  graph: { order: number; size: number }
  on: ReturnType<typeof vi.fn>
  refresh: ReturnType<typeof vi.fn>
  kill: ReturnType<typeof vi.fn>
  setSetting: ReturnType<typeof vi.fn>
}>)

vi.mock('../src/api/client', () => ({
  api: { memoryGraph },
}))

// sigma renders via WebGL, which jsdom doesn't provide. Mock Sigma so the
// component's UI shell (filters, search, counts) can be tested headlessly.
// graphology is pure JS (no WebGL) so it does not need mocking.
vi.mock('sigma', () => {
  class MockSigma {
    readonly on = vi.fn()
    readonly refresh = vi.fn()
    readonly kill = vi.fn()
    readonly setSetting = vi.fn()

    constructor(readonly graph: { order: number; size: number }) {
      if (sigmaConstructorFailure.current) throw sigmaConstructorFailure.current
      sigmaInstances.push(this)
    }
  }
  return { default: MockSigma }
})

// Layout is not this integration test's subject. Keep the dynamic-import seam,
// but replace d3's real 250ms force pass with the same fluent contract and a
// synchronous tick so a loaded runner cannot decide when the UI becomes ready.
vi.mock('d3', () => {
  const force = () => {
    const instance = {
      id: () => instance,
      distance: () => instance,
      strength: () => instance,
      distanceMax: () => instance,
    }
    return instance
  }
  return {
    forceSimulation: () => {
      const simulation = {
        force: () => simulation,
        alphaDecay: () => simulation,
        stop: () => simulation,
        tick: () => simulation,
      }
      return simulation
    },
    forceLink: force,
    forceManyBody: force,
    forceCollide: force,
    forceX: force,
    forceY: force,
  }
})

import MemoryGraphTab from '../src/pages/overview/MemoryGraphTab'

describe('MemoryGraphTab Integration Tests', () => {
  beforeEach(() => {
    memoryGraph.mockReset()
    memoryGraph.mockResolvedValue(mockMemoryGraph)
    sigmaConstructorFailure.current = null
    sigmaInstances.length = 0
  })

  async function graphReady() {
    expect(await screen.findByText(/All \(7\)/)).toBeInTheDocument()
    await waitFor(() => expect(sigmaInstances).toHaveLength(1))
    return sigmaInstances[0]
  }

  it('renders graph container with nodes after loading', async () => {
    renderWithProviders(<MemoryGraphTab />)

    const sigma = await graphReady()
    expect(sigma.graph.order).toBe(7)
    expect(sigma.graph.size).toBe(2)
  })

  it('keeps the data controls available when the renderer cannot initialize', async () => {
    const failure = new Error('WebGL unavailable')
    sigmaConstructorFailure.current = failure
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    try {
      renderWithProviders(<MemoryGraphTab />)

      expect(await screen.findByText(/All \(7\)/)).toBeInTheDocument()
      await waitFor(() => {
        expect(warn).toHaveBeenCalledWith('MemoryGraph: sigma init failed', failure)
      })
      expect(sigmaInstances).toHaveLength(0)
    } finally {
      warn.mockRestore()
    }
  })

  it('displays filter buttons with correct group counts', async () => {
    renderWithProviders(<MemoryGraphTab />)

    await graphReady()
    expect(screen.getByText(/Preferences \(2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Projects \(2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Semantic \(1\)/)).toBeInTheDocument()
    expect(screen.getByText(/Lessons \(1\)/)).toBeInTheDocument()
    expect(screen.getByText(/History \(1\)/)).toBeInTheDocument()
  })

  it('filters nodes when clicking a group button', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryGraphTab />)

    await graphReady()

    await user.click(screen.getByText(/Projects \(2\)/))
    // Active filter button gets accent styling
    expect(screen.getByText(/Projects \(2\)/).className).toContain('!border-accent')
  })

  it('toggles filter off when clicking same group again', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryGraphTab />)

    await graphReady()

    const btn = screen.getByText(/Lessons \(1\)/)
    await user.click(btn)
    expect(btn.className).toContain('!border-accent')
    await user.click(btn)
    expect(btn.className).not.toContain('!border-accent')
  })

  it('has a working search input', async () => {
    renderWithProviders(<MemoryGraphTab />)

    await graphReady()

    fireEvent.change(screen.getByPlaceholderText('Search nodes…'), { target: { value: 'typescript' } })
    expect(screen.getByPlaceholderText('Search nodes…')).toHaveValue('typescript')
  })

  it('shows empty state when API returns no data', async () => {
    memoryGraph.mockResolvedValueOnce({ nodes: [], edges: [] })
    renderWithProviders(<MemoryGraphTab />)

    expect(await screen.findByText(/No memory data to visualize/)).toBeInTheDocument()
  })

  it('shows loading state initially', async () => {
    let release!: (value: typeof mockMemoryGraph) => void
    const pending = new Promise<typeof mockMemoryGraph>(resolve => { release = resolve })
    memoryGraph.mockReturnValueOnce(pending)
    renderWithProviders(<MemoryGraphTab />)
    expect(screen.getByText(/Loading graph data/)).toBeInTheDocument()

    await act(async () => {
      release(mockMemoryGraph)
      await pending
    })
    await graphReady()
  })

  it('handles API error gracefully', async () => {
    memoryGraph.mockRejectedValueOnce(new Error('memory graph unavailable'))
    renderWithProviders(<MemoryGraphTab />)

    expect(await screen.findByText(/No memory data to visualize/)).toBeInTheDocument()
  })

  it('refresh button reloads data', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MemoryGraphTab />)

    await graphReady()
    expect(memoryGraph).toHaveBeenCalledTimes(1)

    await user.click(screen.getByText(/Refresh/))
    await waitFor(() => expect(memoryGraph).toHaveBeenCalledTimes(2))
    expect(screen.getByText(/All \(7\)/)).toBeInTheDocument()
  })

  it('renders the graph title and info tooltip', async () => {
    renderWithProviders(<MemoryGraphTab />)

    await graphReady()
    expect(screen.getByText(/Memory Graph/)).toBeInTheDocument()
  })
})
