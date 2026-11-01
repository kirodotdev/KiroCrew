/**
 * A failed knowledge-graph read says so, with a retry.
 *
 * `isError` was never read, so an outage rendered "No graph data yet / ingest
 * documents to build the entity graph" to a user who already has documents. The
 * empty state stays for a read that succeeded with no nodes. sigma and graphology
 * are WebGL-bound and never reached on these paths, so they are stubbed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))
vi.mock('sigma', () => ({ default: class { kill() {} refresh() {} on() {} } }))
vi.mock('graphology', () => ({ default: class {} }))

import KnowledgeGraph from '../pages/knowledge/KnowledgeGraph'

const EMPTY = 'No graph data yet'

function renderGraph() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <KnowledgeGraph />
    </QueryClientProvider>,
  )
}

describe('KnowledgeGraph failed read', () => {
  beforeEach(() => {
    mockKnowledgeApi.mockReset()
  })

  it('shows the error with a retry instead of the empty state', async () => {
    let graphCalls = 0
    mockKnowledgeApi.mockImplementation((path: string) => {
      if (path === '/sources') return Promise.resolve([])
      graphCalls += 1
      return graphCalls === 1
        ? Promise.reject(new Error('502 Bad Gateway'))
        : Promise.resolve({ nodes: [], edges: [] })
    })
    renderGraph()

    const notice = await screen.findByTestId('knowledge-graph-load-error')
    expect(notice.textContent).toContain('Could not load the knowledge graph')
    expect(notice.textContent).toContain('502 Bad Gateway')
    expect(screen.queryByText(EMPTY)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByText(EMPTY)).toBeInTheDocument()
    expect(screen.queryByTestId('knowledge-graph-load-error')).toBeNull()
    expect(graphCalls).toBe(2)
  })

  it('keeps the empty state for a read that succeeded with no nodes', async () => {
    mockKnowledgeApi.mockImplementation((path: string) =>
      Promise.resolve(path === '/sources' ? [] : { nodes: [], edges: [] }),
    )
    renderGraph()
    expect(await screen.findByText(EMPTY)).toBeInTheDocument()
    expect(screen.queryByTestId('knowledge-graph-load-error')).toBeNull()
  })
})
