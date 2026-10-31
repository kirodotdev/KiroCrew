/**
 * A failed memory-graph read says so, with a retry.
 *
 * The query used to fold any failure into `{ nodes: [] }`, so an outage showed
 * the "no memory data, add preferences first" empty state. The empty state stays
 * for a read that succeeded with nothing in it. sigma and graphology are
 * WebGL-bound and irrelevant here, so they are stubbed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('sigma', () => ({
  default: class {
    kill() {}
    refresh() {}
    on() {}
    getCamera() {
      return { on() {} }
    }
  },
}))
vi.mock('graphology', () => ({
  default: class {
    addNode() {}
    addEdge() {}
    hasNode() {
      return false
    }
    hasEdge() {
      return false
    }
    forEachNode() {}
  },
}))
vi.mock('d3', () => ({}))
vi.mock('../api/client', () => ({ api: { memoryGraph: vi.fn() } }))

import { api } from '../api/client'
import MemoryGraphTab from '../pages/overview/MemoryGraphTab'

const EMPTY = 'No memory data to visualize. Add preferences, projects, or lessons first.'

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryGraphTab />
    </QueryClientProvider>,
  )
}

describe('MemoryGraphTab failed read', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the error with a retry instead of the empty state', async () => {
    vi.mocked(api.memoryGraph).mockRejectedValueOnce(new Error('HTTP 500: graph read failed'))
    renderTab()

    const notice = await screen.findByTestId('memory-graph-load-error')
    expect(notice.textContent).toContain('Could not load the memory graph')
    expect(notice.textContent).toContain('HTTP 500: graph read failed')
    expect(screen.queryByText(EMPTY)).toBeNull()

    vi.mocked(api.memoryGraph).mockResolvedValueOnce({ nodes: [], edges: [] } as never)
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByText(EMPTY)).toBeInTheDocument()
    expect(screen.queryByTestId('memory-graph-load-error')).toBeNull()
    expect(api.memoryGraph).toHaveBeenCalledTimes(2)
  })

  it('keeps a loaded graph and flags a failed Refresh with a retry', async () => {
    const NODE = { id: 'n1', label: 'project.alpha', group: 'semantic', title: 'project.alpha = 1' }
    vi.mocked(api.memoryGraph).mockResolvedValueOnce({ nodes: [NODE], edges: [] } as never)
    renderTab()
    expect(await screen.findByLabelText('Search memory nodes')).toBeInTheDocument()

    vi.mocked(api.memoryGraph).mockRejectedValueOnce(new Error('HTTP 500: graph read failed'))
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    const notice = await screen.findByTestId('memory-graph-load-error')
    expect(notice.textContent).toContain('HTTP 500: graph read failed')
    // The graph from the earlier read stays: search and its node count remain.
    expect(screen.getByLabelText('Search memory nodes')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument()
  })

  it('keeps the empty state for a read that succeeded with no nodes', async () => {
    vi.mocked(api.memoryGraph).mockResolvedValue({ nodes: [], edges: [] } as never)
    renderTab()
    expect(await screen.findByText(EMPTY)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('memory-graph-load-error')).toBeNull())
  })
})
