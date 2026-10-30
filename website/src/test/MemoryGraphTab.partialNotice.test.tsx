/**
 * The memory graph says when the server capped what it drew.
 *
 * `GET /api/memory/graph` reads at most a fixed number of semantic rows and lessons
 * and reports `partial` per population. The tab's search only finds the nodes that
 * were sent, so a capped graph must say it is partial; an uncapped one says nothing.
 * sigma and graphology are WebGL-bound and irrelevant to the notice, so they are
 * stubbed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
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

const NODE = { id: 'n1', label: 'project.alpha', group: 'semantic', title: 'project.alpha = 1' }
const NOTICE = 'Only part of your memory is drawn here, so search finds only these nodes.'

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryGraphTab />
    </QueryClientProvider>,
  )
}

describe('MemoryGraphTab partial notice', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('says the graph is partial when the server capped a population', async () => {
    vi.mocked(api.memoryGraph).mockResolvedValue({
      nodes: [NODE],
      edges: [],
      partial: { semantic: true, lessons: false },
    } as never)
    renderTab()
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
  })

  it('says nothing when every population fit', async () => {
    vi.mocked(api.memoryGraph).mockResolvedValue({
      nodes: [NODE],
      edges: [],
      partial: { semantic: false, lessons: false },
    } as never)
    renderTab()
    expect(await screen.findByLabelText('Search memory nodes')).toBeInTheDocument()
    expect(screen.queryByText(NOTICE)).toBeNull()
  })
})
