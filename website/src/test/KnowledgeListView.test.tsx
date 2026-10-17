import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
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

// Three sources of wildly different sizes. Under a shared item pager, `Artifacts`
// (11 items) would only appear on whichever page its items happened to land on --
// page 5 of 10 -- so it was invisible from page 1. Source-first rows show it up front.
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
// Knobs the filter-dropdown tests below vary. Defaults reproduce the original
// fixture exactly, so the source-first tests are unaffected by their presence.
let namespacesFixture: { name: string; count: number }[] = []
let flatTotal = 1

function defaultApi(path: string) {
  const p = String(path)
  if (p.startsWith('/items')) {
    itemCalls.push(p)
    const sid = new URLSearchParams(p.split('?')[1]).get('source_id')
    if (sid) {
      const total = COUNTS[sid as keyof typeof COUNTS] ?? 0
      return Promise.resolve({ items: [item(`${sid}-a`, sid, `${sid} item A`)], total })
    }
    // Flat search branch
    return Promise.resolve({ items: [item('f1', 's1', 'search hit')], total: flatTotal })
  }
  if (p.startsWith('/source-counts')) {
    return Promise.resolve({ counts: COUNTS, total: 953 })
  }
  if (p === '/sources') return Promise.resolve(SOURCES)
  if (p === '/stats') return Promise.resolve({ items: 953, entities: 0, relations: 0, sources: 3 })
  if (p === '/namespaces') return Promise.resolve(namespacesFixture)
  if (p === '/config') return Promise.resolve({ enabled: true, supported_formats: ['.md'] })
  return Promise.resolve([])
}

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  itemCalls = []
  namespacesFixture = []
  flatTotal = 1
  mockKnowledgeApi.mockImplementation((path: string) => defaultApi(path))
})

describe('Knowledge List View — source-first rows', () => {
  it('shows every source at once, no scrolling through a shared pager', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    expect(await screen.findByText('PersonalKnowledgeBase')).toBeInTheDocument()
    expect(await screen.findByText('WorkforceEmploymentKnowledgeBase')).toBeInTheDocument()
    // The 11-item source appears on the first screen instead of being stranded on page 5.
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
    // 953 items / 100 would be "Page 1 of 10" under a shared pager.
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
    // happy-dom's navigator.clipboard is getter-only; defineProperty replaces it.
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
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

/**
 * The three filter dropdowns were native `<select>`s until they moved to
 * `SimpleSelect` (Radix). Radix renders a `<button role="combobox">` and mounts
 * the options only while the popup is open, so a `change` event on the trigger
 * does nothing — open it, then click the option.
 */
describe('Knowledge List View — filter dropdowns', () => {
  const filterTrigger = (name: string) => screen.getByRole('combobox', { name })

  async function pick(filter: string, option: string | RegExp) {
    fireEvent.click(filterTrigger(filter))
    fireEvent.click(await screen.findByRole('option', { name: option }))
  }

  /** Newest request for a path prefix, so an assertion cannot read a stale call. */
  const lastCall = (prefix: string) =>
    String(mockKnowledgeApi.mock.calls.filter(c => String(c[0]).startsWith(prefix)).pop()?.[0] ?? '')

  it('mounts its options only once the popup is open', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')
    // A native select keeps every <option> in the DOM from first paint; the
    // theme-drawn popup has none until the trigger is pressed.
    expect(screen.queryByRole('option', { name: 'runbook' })).not.toBeInTheDocument()
    fireEvent.click(filterTrigger('Filter by type'))
    expect(await screen.findByRole('option', { name: 'runbook' })).toBeInTheDocument()
    // Underscored values stay humanised in the visible label.
    expect(screen.getByRole('option', { name: 'meeting notes' })).toBeInTheDocument()
  })

  it('shows each filter default in its trigger', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')
    // The empty-string "all" rows: reachable as real options, and rendered in
    // the trigger rather than falling through to a placeholder dash.
    expect(filterTrigger('Filter by type')).toHaveTextContent('All types')
    expect(filterTrigger('Filter by namespace')).toHaveTextContent('All namespaces')
    // statusFilter starts at DEFAULT_STATUS_FILTER, not at the "all" row. The
    // trigger shows the CATALOG label, not the raw `active` enum — the value is
    // visible copy now that the popup is theme-drawn rather than OS-drawn.
    expect(filterTrigger('Filter by status')).toHaveTextContent('Active')
  })

  it('narrows the query by type and rewinds to page 1', async () => {
    flatTotal = 60 // 60 hits at limit=20 in search mode -> a 3-page flat pager
    render(<KnowledgePage />, { wrapper: Wrapper })
    // The top-level pager exists only in flat search mode.
    await userEvent.type(await screen.findByPlaceholderText(/Search knowledge/i), 'hit{Enter}')
    fireEvent.click(await screen.findByRole('button', { name: /Next/ }))
    expect(await screen.findByText('Page 2 of 3')).toBeInTheDocument()

    await pick('Filter by type', 'runbook')

    // Both effects of the old onChange survive the bare-value signature: the
    // filter is applied AND the pager is rewound, so the user is not left on a
    // page that the narrowed result set may no longer have.
    expect(await screen.findByText('Page 1 of 3')).toBeInTheDocument()
    expect(filterTrigger('Filter by type')).toHaveTextContent('runbook')
    await waitFor(() => {
      const last = lastCall('/items')
      expect(last).toContain('type=runbook')
      expect(last).toContain('page=1')
    })
  })

  it('drops the status scope when the "All statuses" row is chosen', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')
    await waitFor(() => expect(lastCall('/source-counts')).toContain('status=active'))

    await pick('Filter by status', 'All statuses')

    // '' rides through SimpleSelect's internal sentinel and comes back out as
    // '', which the request builder reads as "no status param at all".
    expect(filterTrigger('Filter by status')).toHaveTextContent('All statuses')
    await waitFor(() => expect(lastCall('/source-counts')).not.toContain('status='))
  })

  it('offers every namespace with its item count and filters by the bare name', async () => {
    namespacesFixture = [{ name: 'default', count: 41 }, { name: 'work', count: 7 }]
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')

    fireEvent.click(filterTrigger('Filter by namespace'))
    expect(await screen.findByRole('option', { name: 'work (7)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'default (41)' })).toBeInTheDocument()
    // The count is label-only: the value sent upstream is the namespace name.
    fireEvent.click(screen.getByRole('option', { name: 'work (7)' }))
    await waitFor(() => expect(lastCall('/source-counts')).toContain('namespace=work'))
    expect(filterTrigger('Filter by namespace')).toHaveTextContent('work (7)')
  })
})
