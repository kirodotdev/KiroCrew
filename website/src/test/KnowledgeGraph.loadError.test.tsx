/**
 * A failed knowledge-graph read says so, with a retry.
 *
 * `isError` was never read, so an outage rendered "No graph data yet / ingest
 * documents to build the entity graph" to a user who already has documents. The
 * empty state stays for a read that succeeded with no nodes. sigma and graphology
 * are WebGL-bound and never reached on these paths, so they are stubbed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

  it('keeps the pressed Retry focusable and mounted while the retry runs', async () => {
    let settle: (value: { nodes: never[]; edges: never[] }) => void = () => {}
    let graphCalls = 0
    mockKnowledgeApi.mockImplementation((path: string) => {
      if (path === '/sources') return Promise.resolve([])
      graphCalls += 1
      if (graphCalls === 1) return Promise.reject(new Error('502 Bad Gateway'))
      // Hold the retry open so the button can be observed mid-flight.
      return new Promise(resolve => { settle = resolve })
    })
    const u = userEvent.setup()
    renderGraph()

    const retry = await screen.findByRole('button', { name: /Retry/ })
    retry.focus()
    await u.click(retry)

    // With nothing cached the retry returns the query to isLoading; the fix
    // computes `failed` before the loading return, so the Retry stays mounted
    // (aria-disabled keeps it in the tab order) instead of being replaced by
    // the loading placeholder and dropping focus to the body.
    await waitFor(() => expect(retry).toHaveAttribute('aria-busy', 'true'))
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).not.toBeDisabled()
    expect(retry).toHaveFocus()

    settle({ nodes: [], edges: [] })
    await waitFor(() => expect(screen.queryByTestId('knowledge-graph-load-error')).toBeNull())
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
