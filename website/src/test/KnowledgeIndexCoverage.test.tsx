import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { IngestionJob, NamespaceInfo } from '../pages/knowledge/types'

/**
 * Coverage for the KnowledgePage shell itself — the paths the source-first
 * tests in KnowledgeListView.test.tsx leave cold: the help dialog, the keyboard
 * handler, the bulk archive/delete mutations, the ingest mutation, the graph
 * tab and its entity section, and the stats bar's embedding states.
 *
 * The three heavy children are stubbed. Each owns its own test file, and the
 * behaviour under test here is the PAGE's contract with them: which props it
 * hands down and what it does with the callbacks they fire. Stubbing keeps a
 * child's internal fetches out of the assertions.
 */
const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

vi.mock('../pages/knowledge/DetailView', () => ({
  default: ({ itemId, onBack }: { itemId: string; onBack: () => void }) => (
    <div>
      <span>detail:{itemId}</span>
      <button onClick={onBack}>stub-back</button>
    </div>
  ),
}))

vi.mock('../pages/knowledge/KnowledgeGraph', () => ({
  default: ({ highlightEntity, onSelectEntity }: {
    highlightEntity: string | null
    onSelectEntity: (name: string) => void
  }) => (
    <div>
      <span>highlight:{highlightEntity ?? 'none'}</span>
      <button onClick={() => onSelectEntity('Ledger')}>stub-graph-pick</button>
    </div>
  ),
}))

vi.mock('../pages/knowledge/SourcesList', () => ({
  default: ({ onIngest, uploadNamespace, setUploadNamespace, namespaces, ingestionJobs, uploadAccept, acceptsNoExtension }: {
    onIngest: (files: File[]) => void
    uploadNamespace: string
    setUploadNamespace: (v: string) => void
    namespaces: NamespaceInfo[]
    ingestionJobs: IngestionJob[]
    uploadAccept?: string
    acceptsNoExtension?: boolean
  }) => (
    <div>
      <span>accept:{uploadAccept}</span>
      <span>ns:{uploadNamespace}</span>
      <span>nsCount:{namespaces.length}</span>
      <span>noExt:{String(acceptsNoExtension)}</span>
      <button onClick={() => onIngest([new File(['body'], 'notes.md', { type: 'text/markdown' })])}>stub-upload</button>
      <button onClick={() => setUploadNamespace('work')}>stub-set-ns</button>
      <ul>{ingestionJobs.map(j => <li key={j.name}>job:{j.name}:{j.status}</li>)}</ul>
    </div>
  ),
}))

const { default: KnowledgePage } = await import('../pages/knowledge/index')

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

let qc = makeClient()
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  </MemoryRouter>
)

type Stats = {
  items: number
  entities: number
  relations: number
  sources: number
  embeddings?: { enabled: boolean; model?: string; available?: boolean; embedded_items?: number }
}
type SourceFixture = { id: string; name: string; source_type: string; uri: string; sync_status: string; item_count: number }

const FLAT_ITEM = {
  id: 'f1',
  title: 'Quarterly ledger notes',
  item_type: 'document',
  status: 'active',
  source_id: 's1',
  content: 'flat search body',
  created_at: '2026-01-01',
  updated_at: '2026-01-01',
}

// Fixture knobs. Every test resets them in beforeEach, so a test only states
// the shape it needs.
let sourcesFixture: SourceFixture[]
let countsFixture: Record<string, number>
let countsTotal: number
let filteredCountsEmpty: boolean
let namespacesFixture: NamespaceInfo[]
let statsFixture: Stats | undefined
let statsPending: boolean
let entitiesFixture: { id: string; name: string; entity_type: string; mention_count?: number }[]
let entityItemsFixture: { id: string; title: string; item_type: string; status: string; updated_at: string }[]
let flatTotal: number
let itemMutationError: Error | null
let ingestError: Error | null
let countsCalls: string[]

function defaultApi(path: string): Promise<unknown> {
  const p = String(path)
  if (p.startsWith('/items/')) {
    return itemMutationError ? Promise.reject(itemMutationError) : Promise.resolve({ ok: true })
  }
  if (p.startsWith('/items')) {
    return Promise.resolve({ items: [FLAT_ITEM], total: flatTotal })
  }
  if (p.startsWith('/source-counts')) {
    countsCalls.push(p)
    if (filteredCountsEmpty && p.includes('type=')) return Promise.resolve({ counts: {}, total: 0 })
    return Promise.resolve({ counts: countsFixture, total: countsTotal })
  }
  if (p.startsWith('/ingest')) {
    return ingestError ? Promise.reject(ingestError) : Promise.resolve({ job_id: 'j1' })
  }
  if (p.startsWith('/entities/by-name/')) return Promise.resolve(entityItemsFixture)
  if (p.startsWith('/entities?q=')) return Promise.resolve(entitiesFixture)
  if (p === '/sources') return Promise.resolve(sourcesFixture)
  if (p === '/stats') {
    // A pending promise, not `undefined`: react-query rejects an undefined
    // payload, and "the page before /stats answers" is a loading state anyway.
    return statsPending ? new Promise<never>(() => {}) : Promise.resolve(statsFixture)
  }
  if (p === '/namespaces') return Promise.resolve(namespacesFixture)
  if (p === '/config') return Promise.resolve({ enabled: true, supported_formats: ['.md', '.txt'], accepts_no_extension: false })
  return Promise.resolve([])
}

beforeEach(() => {
  vi.clearAllMocks()
  qc = makeClient()
  sourcesFixture = [{ id: 's1', name: 'PersonalKnowledgeBase', source_type: 'local_folder', uri: '/pkb', sync_status: 'synced', item_count: 4 }]
  countsFixture = { s1: 4 }
  countsTotal = 4
  filteredCountsEmpty = false
  namespacesFixture = []
  statsFixture = { items: 4, entities: 2, relations: 1, sources: 1, embeddings: { enabled: true, available: true, model: 'gte-small', embedded_items: 4 } }
  entitiesFixture = [{ id: 'e1', name: 'Ledger', entity_type: 'concept', mention_count: 7 }]
  entityItemsFixture = [{ id: 'i9', title: 'Ledger design', item_type: 'design_doc', status: 'active', updated_at: '2026-01-02' }]
  flatTotal = 1
  itemMutationError = null
  ingestError = null
  countsCalls = []
  mockKnowledgeApi.mockImplementation((path: string) => defaultApi(path))
})

afterEach(() => {
  qc.clear()
})

/** Flat search mode: the page owns the item list and the top-level pager. */
async function searchFor(term: string) {
  const input = await screen.findByPlaceholderText(/Search knowledge/i)
  await userEvent.type(input, `${term}{Enter}`)
  return input
}

describe('KnowledgePage — help dialog', () => {
  it('opens the help dialog with the onboarding steps and shortcut legend', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Help/ }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Welcome to the Knowledge Library')
    expect(dialog).toHaveTextContent('Keyboard Shortcuts')
    // The numbered onboarding steps come from ONBOARDING.steps, one <li> each.
    expect(dialog.querySelectorAll('li').length).toBeGreaterThanOrEqual(3)
  })

  it('closes the help dialog from its Close button', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Help/ }))
    await screen.findByRole('dialog')
    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('closes the help dialog when the backdrop itself is clicked', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Help/ }))
    const backdrop = (await screen.findByRole('dialog')).parentElement as HTMLElement
    // Only a click on the backdrop element closes it — a click that bubbles up
    // from the panel must not, or every interaction inside would dismiss it.
    fireEvent.click(screen.getByRole('dialog'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.click(backdrop)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('Escape closes the help dialog before it clears anything else', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Help/ }))
    await screen.findByRole('dialog')
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

describe('KnowledgePage — keyboard shortcuts', () => {
  it('"/" focuses the search box', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    const input = await screen.findByPlaceholderText(/Search knowledge/i)
    expect(document.activeElement).not.toBe(input)
    fireEvent.keyDown(document.body, { key: '/' })
    expect(document.activeElement).toBe(input)
  })

  it('Escape inside the search box blurs it instead of reaching the page handler', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    const input = await screen.findByPlaceholderText(/Search knowledge/i) as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(document.activeElement).not.toBe(input)
  })

  it('arrow keys page the flat search results forwards and back', async () => {
    flatTotal = 60 // 60 hits at limit=20 in search mode -> 3 pages
    render(<KnowledgePage />, { wrapper: Wrapper })
    await searchFor('ledger')
    expect(await screen.findByText('Page 1 of 3')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    expect(await screen.findByText('Page 2 of 3')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' })
    expect(await screen.findByText('Page 1 of 3')).toBeInTheDocument()
    // Past the last page there is nothing to advance to.
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' })
    expect(await screen.findByText('Page 1 of 3')).toBeInTheDocument()
  })

  it('Escape closes an open item before it clears the selection', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await searchFor('ledger')
    await userEvent.click(await screen.findByText('Quarterly ledger notes'))
    expect(await screen.findByText('detail:f1')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('detail:f1')).not.toBeInTheDocument())
  })

  it('Escape clears a selection once nothing is open', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await searchFor('ledger')
    await userEvent.click(await screen.findByLabelText('Select Quarterly ledger notes'))
    expect(await screen.findByText('1 selected')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument())
  })
})

describe('KnowledgePage — bulk actions', () => {
  async function selectFlatItem() {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await searchFor('ledger')
    await userEvent.click(await screen.findByLabelText('Select Quarterly ledger notes'))
    expect(await screen.findByText('1 selected')).toBeInTheDocument()
  }

  const mutationCalls = (method: string) =>
    mockKnowledgeApi.mock.calls.filter(c =>
      String(c[0]).startsWith('/items/') && (c[1] as { method?: string } | undefined)?.method === method)

  it('archives the selection and drops it when the write succeeds', async () => {
    await selectFlatItem()
    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(mutationCalls('PATCH')).toHaveLength(1))
    const [path, opts] = mutationCalls('PATCH')[0] as [string, { body?: string }]
    expect(path).toBe('/items/f1')
    expect(String(opts.body)).toContain('archived')
    // onSuccess calls onDone, so the bar goes away.
    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument())
  })

  it('restores the list when the archive write fails', async () => {
    await selectFlatItem()
    itemMutationError = new Error('patch refused')
    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(mutationCalls('PATCH')).toHaveLength(1))
    // onError replays the snapshot taken in onMutate, so the optimistically
    // removed row comes back and the selection is NOT cleared.
    expect(await screen.findByText('Quarterly ledger notes')).toBeInTheDocument()
    expect(screen.getByText('1 selected')).toBeInTheDocument()
  })

  it('deletes the selection only after the confirm prompt is accepted', async () => {
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmSpy)
    await selectFlatItem()
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(mutationCalls('DELETE')).toHaveLength(1))
    expect(mutationCalls('DELETE')[0][0]).toBe('/items/f1')
    expect(confirmSpy).toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('writes nothing when the delete prompt is dismissed', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    await selectFlatItem()
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    expect(mutationCalls('DELETE')).toHaveLength(0)
    expect(screen.getByText('1 selected')).toBeInTheDocument()
    vi.unstubAllGlobals()
  })

  it('copies the content of the selected items', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await selectFlatItem()
    await userEvent.click(screen.getByRole('button', { name: /Copy Content/ }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('flat search body'))
    expect(await screen.findByText('Copied!')).toBeInTheDocument()
  })

  it('clears the selection from the bar', async () => {
    await selectFlatItem()
    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument())
  })
})

describe('KnowledgePage — sources tab and ingest', () => {
  it('hands the backend-advertised formats to the sources panel', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Sources/ }))
    // Built from /config.supported_formats, not from the hardcoded fallback.
    expect(await screen.findByText('accept:.md,.txt')).toBeInTheDocument()
    expect(screen.getByText('noExt:false')).toBeInTheDocument()
    expect(screen.getByText('ns:default')).toBeInTheDocument()
  })

  it('lets the panel change the upload namespace', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Sources/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-set-ns' }))
    expect(await screen.findByText('ns:work')).toBeInTheDocument()
  })

  it('ingests dropped files under the active namespace and reports each job', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Sources/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-upload' }))
    expect(await screen.findByText('job:notes.md:done')).toBeInTheDocument()
    const ingest = mockKnowledgeApi.mock.calls.find(c => String(c[0]).startsWith('/ingest'))
    expect(String(ingest?.[0])).toContain('namespace=default')
    expect((ingest?.[1] as { method?: string })?.method).toBe('POST')
  })

  it('reports a per-file error instead of failing the whole upload', async () => {
    ingestError = new Error('unsupported format')
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Sources/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-upload' }))
    expect(await screen.findByText('job:notes.md:error: unsupported format')).toBeInTheDocument()
  })
})

describe('KnowledgePage — graph tab', () => {
  it('mounts the graph lazily and shows no entity section until one is picked', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Graph View/ }))
    expect(await screen.findByText('highlight:none')).toBeInTheDocument()
    expect(screen.queryByText('Items mentioning:')).not.toBeInTheDocument()
  })

  it('lists the items that mention the entity selected in the graph', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Graph View/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-graph-pick' }))
    expect(await screen.findByText('Items mentioning:')).toBeInTheDocument()
    expect(await screen.findByText('Ledger design')).toBeInTheDocument()
    expect(mockKnowledgeApi.mock.calls.some(c => String(c[0]) === '/entities/by-name/Ledger/items')).toBe(true)
  })

  it('says so when the selected entity has no items', async () => {
    entityItemsFixture = []
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Graph View/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-graph-pick' }))
    expect(await screen.findByText('No items found')).toBeInTheDocument()
  })

  it('clearing the entity selection removes the section', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Graph View/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-graph-pick' }))
    await screen.findByText('Items mentioning:')
    await userEvent.click(screen.getByRole('button', { name: 'Clear entity selection' }))
    await waitFor(() => expect(screen.queryByText('Items mentioning:')).not.toBeInTheDocument())
  })

  it('opening an entity item returns to the list tab with that item open', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    await userEvent.click(await screen.findByRole('button', { name: /Graph View/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-graph-pick' }))
    await userEvent.click(await screen.findByText('Ledger design'))
    expect(await screen.findByText('detail:i9')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'stub-back' }))
    await waitFor(() => expect(screen.queryByText('detail:i9')).not.toBeInTheDocument())
  })

  it('an autocomplete hit jumps to the graph tab focused on that entity', async () => {
    render(<KnowledgePage />, { wrapper: Wrapper })
    const input = await screen.findByPlaceholderText(/Search knowledge/i)
    await userEvent.type(input, 'led')
    // The suggestion row carries the entity type and its mention count.
    const hit = await screen.findByRole('button', { name: /concept Ledger 7 mentions/ })
    await userEvent.click(hit)
    expect(await screen.findByText('highlight:Ledger')).toBeInTheDocument()
    expect(await screen.findByText('Items mentioning:')).toBeInTheDocument()
  })
})

describe('KnowledgePage — empty states and stats', () => {
  it('sends a brand new library to the Sources tab', async () => {
    countsFixture = {}
    countsTotal = 0
    render(<KnowledgePage />, { wrapper: Wrapper })
    const onboarding = await screen.findByTestId('knowledge-onboarding')
    expect(onboarding).toHaveTextContent('Welcome to the Knowledge Library')
    await userEvent.click(screen.getByRole('button', { name: 'Go to Sources to upload files' }))
    expect(await screen.findByText('accept:.md,.txt')).toBeInTheDocument()
  })

  it('a filter that matches nothing reports empty filters, not an empty library', async () => {
    filteredCountsEmpty = true
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')
    fireEvent.click(screen.getByRole('combobox', { name: 'Filter by type' }))
    fireEvent.click(await screen.findByRole('option', { name: 'runbook' }))
    // Onboarding would strand the user here: the filter bar must stay so the
    // filter can be undone.
    expect(await screen.findByText('No items match your filters')).toBeInTheDocument()
    expect(screen.queryByTestId('knowledge-onboarding')).not.toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Filter by type' })).toBeInTheDocument()
  })

  it('flags an embedding model that is enabled but not yet available', async () => {
    statsFixture = { items: 4, entities: 2, relations: 1, sources: 1, embeddings: { enabled: true, available: false, model: 'gte-small' } }
    render(<KnowledgePage />, { wrapper: Wrapper })
    expect(await screen.findByText(/embeddings loading/)).toBeInTheDocument()
  })

  it('shows the initializing state when embeddings are switched off', async () => {
    statsFixture = { items: 4, entities: 2, relations: 1, sources: 1 }
    render(<KnowledgePage />, { wrapper: Wrapper })
    expect(await screen.findByText(/embeddings initializing/)).toBeInTheDocument()
  })

  it('renders no stats bar until /stats answers', async () => {
    statsFixture = undefined
    render(<KnowledgePage />, { wrapper: Wrapper })
    await screen.findByText('PersonalKnowledgeBase')
    expect(screen.queryByText(/embeddings/)).not.toBeInTheDocument()
  })
})

describe('KnowledgePage — source sync polling', () => {
  afterEach(() => { vi.useRealTimers() })

  it('refreshes items once a scanning source settles', async () => {
    vi.useFakeTimers()
    sourcesFixture = [{ id: 's1', name: 'PersonalKnowledgeBase', source_type: 'local_folder', uri: '/pkb', sync_status: 'syncing', item_count: 4 }]
    render(<KnowledgePage />, { wrapper: Wrapper })
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    const before = countsCalls.length
    // The scan finishes: the 5s poll picks up the new status and the effect
    // invalidates the item caches so the newly ingested rows appear.
    sourcesFixture = [{ id: 's1', name: 'PersonalKnowledgeBase', source_type: 'local_folder', uri: '/pkb', sync_status: 'synced', item_count: 9 }]
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(countsCalls.length).toBeGreaterThan(before)
  })
})
