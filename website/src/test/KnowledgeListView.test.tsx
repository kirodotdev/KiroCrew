import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// Mock the knowledge API so we control /items, /sources and /source-counts.
const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

// Must import after the mock is registered
const { default: KnowledgePage } = await import('../pages/knowledge/index')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

// Reproduces the reported bug shape: three sources of wildly different sizes.
// Under the old shared item pager, `Artifacts` (11 items) only appeared on
// whichever page its items happened to land on -- page 5 of 10 -- so it was
// invisible from page 1.
const SOURCES = [
  { id: 's1', name: 'PersonalKnowledgeBase', source_type: 'local_folder', uri: '/pkb', sync_status: 'synced', item_count: 378 },
  { id: 's2', name: 'WorkforceEmploymentKnowledgeBase', source_type: 'local_folder', uri: '/wfe', sync_status: 'synced', item_count: 542 },
  { id: 's3', name: 'Artifacts', source_type: 'artifact', uri: 'artifact://', sync_status: 'synced', item_count: 11 },
]

const COUNTS = { s1: 378, s2: 542, s3: 11, __none__: 22 }

function item(id: string, sourceId: string, title: string) {
  return {
    id, title, item_type: 'document', status: 'active', source_id: sourceId,
    created_at: '2026-01-01', updated_at: '2026-01-01',
  }
}

let itemCalls: string[] = []

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  itemCalls = []
  mockKnowledgeApi.mockImplementation((path: string) => {
    const p = String(path)
    if (p.startsWith('/items')) {
      itemCalls.push(p)
      const sid = new URLSearchParams(p.split('?')[1]).get('source_id')
      if (sid) {
        const total = COUNTS[sid as keyof typeof COUNTS] ?? 0
        return Promise.resolve({ items: [item(`${sid}-a`, sid, `${sid} item A`)], total })
      }
      // Flat search branch
      return Promise.resolve({ items: [item('f1', 's1', 'search hit')], total: 1 })
    }
    if (p.startsWith('/source-counts')) {
      return Promise.resolve({ counts: COUNTS, total: 953 })
    }
    if (p === '/sources') return Promise.resolve(SOURCES)
    if (p === '/stats') return Promise.resolve({ items: 953, entities: 0, relations: 0, sources: 3 })
    if (p === '/namespaces') return Promise.resolve([])
    if (p === '/config') return Promise.resolve({ enabled: true, supported_formats: ['.md'] })
    return Promise.resolve([])
  })
})

describe('Knowledge List View — source-first rows', () => {
  it('shows every source at once, no scrolling through a shared pager', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    expect(await screen.findByText('PersonalKnowledgeBase')).toBeInTheDocument()
    expect(await screen.findByText('WorkforceEmploymentKnowledgeBase')).toBeInTheDocument()
    // The 11-item source used to be stranded on page 5. It is now on the first screen.
    expect(await screen.findByText('Artifacts')).toBeInTheDocument()
  })

  it('renders a bucket for items that belong to no source', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    expect(await screen.findByText('No source')).toBeInTheDocument()
    expect(await screen.findByText('22')).toBeInTheDocument()
  })

  it('badges show the per-source count from /source-counts', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    expect(await screen.findByText('378')).toBeInTheDocument()
    expect(await screen.findByText('542')).toBeInTheDocument()
    expect(await screen.findByText('11')).toBeInTheDocument()
  })

  it('does not render a top-level pager in source-first mode', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')
    // 953 items / 100 would have been "Page 1 of 10" under the old shared pager.
    expect(screen.queryByText(/^Page 1 of 10$/)).not.toBeInTheDocument()
  })

  it('fetches no items until a source row is expanded', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')
    await waitFor(() => expect(mockKnowledgeApi).toHaveBeenCalled())
    expect(itemCalls).toHaveLength(0)
  })

  it('expanding a source fetches only that source, scoped and paged', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    const row = await screen.findByText('Artifacts')
    await userEvent.click(row)
    await waitFor(() => expect(itemCalls.length).toBeGreaterThan(0))
    expect(itemCalls[0]).toContain('source_id=s3')
    expect(itemCalls[0]).toContain('limit=100')
    expect(itemCalls[0]).toContain('page=1')
  })

  it('shows an in-group pager scoped to the expanded source total', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByText('PersonalKnowledgeBase'))
    // 378 items at 100/page -> 4 pages, from this source alone (not 953/100 = 10).
    expect(await screen.findByText('Page 1 of 4')).toBeInTheDocument()
  })

  it('a small source gets no in-group pager', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByText('Artifacts'))
    await waitFor(() => expect(itemCalls.length).toBeGreaterThan(0))
    // 11 items fit on one page.
    expect(screen.queryByText(/^Page 1 of 1$/)).not.toBeInTheDocument()
  })

  it('forwards active filters to /source-counts so badges stay truthful', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await waitFor(() => {
      const call = mockKnowledgeApi.mock.calls.find(c => String(c[0]).startsWith('/source-counts'))
      expect(call).toBeTruthy()
      // statusFilter defaults to 'active'
      expect(String(call![0])).toContain('status=active')
    })
  })

  it('copies content of items selected inside a group', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByText('Artifacts'))
    // Check the group's item, then use the page-level bulk action.
    await userEvent.click(await screen.findByLabelText(/^Select s3 item A$/))
    await userEvent.click(await screen.findByText(/Copy Content/))
    // Regression: `items` is empty in source-first mode, so a page-array-based
    // implementation copied an empty string here.
    await waitFor(() => expect(writeText).toHaveBeenCalled())
    expect(writeText.mock.calls[0][0]).toContain('s3 item A')
  })

  it('select-all reaches only items rendered on screen', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    // Expand one source, then collapse it: its react-query cache is retained.
    const artifacts = await screen.findByText('Artifacts')
    await userEvent.click(artifacts)
    await screen.findByLabelText(/^Select s3 item A$/)
    await userEvent.click(artifacts)
    await waitFor(() => expect(screen.queryByLabelText(/^Select s3 item A$/)).not.toBeInTheDocument())
    // Expand a different source and select all.
    await userEvent.click(await screen.findByText('PersonalKnowledgeBase'))
    await screen.findByLabelText(/^Select s1 item A$/)
    await userEvent.keyboard('{Control>}a{/Control}')
    // Regression: reading the query cache would also select the collapsed
    // source's retained item, which a bulk Delete would then destroy unseen.
    await waitFor(() => expect(screen.getByText(/\d+ selected/)).toBeInTheDocument())
    expect(screen.getByText('1 selected')).toBeInTheDocument()
  })

  it('drops selected IDs when their source collapses off screen', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    const artifacts = await screen.findByText('Artifacts')
    await userEvent.click(artifacts)
    await userEvent.click(await screen.findByLabelText(/^Select s3 item A$/))
    expect(await screen.findByText('1 selected')).toBeInTheDocument()
    // Collapsing hides the item. Regression: keeping its ID in the selection
    // let a bulk Delete destroy an item the user could no longer see.
    await userEvent.click(artifacts)
    await waitFor(() => expect(screen.queryByText(/\d+ selected/)).not.toBeInTheDocument())
  })

  it('keys per-source queries under the knowledge-items prefix so mutations invalidate them', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByText('Artifacts'))
    await waitFor(() => expect(itemCalls.length).toBeGreaterThan(0))
    const keys = qc.getQueryCache().getAll().map(q => q.queryKey)
    // Both new caches must sit under the prefix every existing
    // invalidateQueries(['knowledge-items']) call site already targets.
    expect(keys.some(k => k[0] === 'knowledge-items' && k[1] === 'source-items')).toBe(true)
    expect(keys.some(k => k[0] === 'knowledge-items' && k[1] === 'source-counts')).toBe(true)
    // Confirm a prefix invalidation matches them.
    const matched = qc.getQueryCache().findAll({ queryKey: ['knowledge-items'] })
    expect(matched.length).toBeGreaterThanOrEqual(2)
  })

  it('searching falls back to a flat list with a top-level pager', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    const input = await screen.findByPlaceholderText(/Search knowledge/i)
    await userEvent.type(input, 'hit{Enter}')
    // Flat mode: the search branch is queried without a source_id scope.
    await waitFor(() => {
      expect(itemCalls.some(c => c.includes('q=hit') && !c.includes('source_id='))).toBe(true)
    })
    expect(await screen.findByText('search hit')).toBeInTheDocument()
  })
})
