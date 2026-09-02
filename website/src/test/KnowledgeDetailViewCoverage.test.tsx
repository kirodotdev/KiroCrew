/**
 * Coverage for the Knowledge Library detail view.
 *
 * DetailView owns four things no other suite exercises: the entity-highlighting
 * pass over raw content, the inline tag editor, the related-items sidecar query,
 * and the archive/delete mutations with their optimistic list surgery and
 * rollback. Each is driven here through the real component with only the API
 * module and the markdown renderer stubbed — the API because every path under
 * test is a request shape, the renderer because its own suites cover it and its
 * plugin chain would dominate the runtime.
 */
import { describe, it, expect, vi, beforeEach, afterEach, beforeAll, afterAll } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComponentProps } from 'react'
import DetailView from '../pages/knowledge/DetailView'
import * as api from '../pages/knowledge/api'
import type { KnowledgeItem } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="markdown">{content}</div>,
}))

type DetailProps = ComponentProps<typeof DetailView>
type RelatedRow = KnowledgeItem & { shared_entities?: number }

// Fixture knobs, reset per test so each test states only the shape it needs.
let itemFixture: KnowledgeItem | null
let itemPending: boolean
let relatedFixture: RelatedRow[]
let mutationError: Error | null

const item = (over: Partial<KnowledgeItem> = {}): KnowledgeItem => ({
  id: 'i1',
  title: 'Ledger design notes',
  item_type: 'design_doc',
  status: 'active',
  content: 'The Ledger keeps every entry.',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-02-03T00:00:00Z',
  ...over,
})

/** Any request carrying a method is a mutation; reads answer from the fixtures. */
function route(path: string, opts?: RequestInit): unknown {
  if (opts?.method) return mutationError ? Promise.reject(mutationError) : { ok: true }
  if (path.endsWith('/related')) return relatedFixture
  if (itemPending) return new Promise(() => {})
  return itemFixture
}

const calls = () => vi.mocked(api.knowledgeApi).mock.calls as [string, RequestInit | undefined][]
const callsTo = (method: string) => calls().filter(([, o]) => o?.method === method)
const bodyOf = (opts: RequestInit | undefined) => JSON.parse(String(opts?.body)) as Record<string, unknown>

const makeClient = () => new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

function renderDetail(props: Partial<DetailProps> = {}, queryClient: QueryClient = makeClient()) {
  const onBack = vi.fn()
  const onEntityClick = vi.fn()
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <DetailView itemId="i1" onBack={onBack} onEntityClick={onEntityClick} {...props} />
    </QueryClientProvider>,
  )
  return { ...utils, queryClient, onBack, onEntityClick }
}

const writeText = vi.fn()
let originalClipboard: PropertyDescriptor | undefined

beforeAll(() => {
  originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
})

afterAll(() => {
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
  else delete (navigator as { clipboard?: unknown }).clipboard
})

beforeEach(() => {
  // useCopy() schedules a 1.5s reset; a real timer would fire after teardown.
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.clearAllMocks()
  itemFixture = item()
  itemPending = false
  relatedFixture = []
  mutationError = null
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  vi.mocked(api.knowledgeApi).mockImplementation(
    ((path: string, opts?: RequestInit) => Promise.resolve(route(path, opts))) as typeof api.knowledgeApi,
  )
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('DetailView — load states', () => {
  it('shows a skeleton while the item request is in flight', async () => {
    itemPending = true
    const { container } = renderDetail()
    await waitFor(() => expect(container.querySelector('[data-slot="skeleton"]')).toBeTruthy())
    expect(screen.queryByText('Ledger design notes')).not.toBeInTheDocument()
  })

  it('reports a missing item when the request answers with no record', async () => {
    itemFixture = null
    renderDetail()
    expect(await screen.findByText('Item not found')).toBeInTheDocument()
  })

  it('renders the header, type badge, timestamp, status and namespace chip', async () => {
    itemFixture = item({ namespace: 'work', updated_at: '2026-02-03T00:00:00Z' })
    renderDetail()
    expect(await screen.findByRole('heading', { name: 'Ledger design notes' })).toBeInTheDocument()
    expect(screen.getByText('design doc')).toBeInTheDocument()
    expect(screen.getByText(/2026/)).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('work')).toBeInTheDocument()
  })

  it('hides the namespace chip for the default namespace', async () => {
    itemFixture = item({ namespace: 'default' })
    renderDetail()
    await screen.findByRole('heading', { name: 'Ledger design notes' })
    expect(screen.queryByText('default')).not.toBeInTheDocument()
  })

  it('walks back to the list from the back button', async () => {
    const { onBack } = renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /Back to list/ }))
    expect(onBack).toHaveBeenCalledTimes(1)
  })
})

describe('DetailView — summary and source locations', () => {
  it('renders the summary card with each source location', async () => {
    itemFixture = item({
      summary: 'How the ledger records mandate changes.',
      source_locations: [
        { item_id: 'i1', source_id: 's1', section_title: 'Overview', chunk_range: '1-3' },
        { item_id: 'i1', source_id: 'raw-source-id' },
      ],
    })
    renderDetail()
    const card = (await screen.findByText('Summary')).parentElement as HTMLElement
    expect(card).toHaveTextContent('How the ledger records mandate changes.')
    expect(card).toHaveTextContent('Overview')
    expect(card).toHaveTextContent('(1-3)')
    // No section title: the raw source id stands in for it.
    expect(card).toHaveTextContent('raw-source-id')
  })

  it('omits the summary card when the item has no summary', async () => {
    renderDetail()
    await screen.findByRole('heading', { name: 'Ledger design notes' })
    expect(screen.queryByText('Summary')).not.toBeInTheDocument()
  })
})

describe('DetailView — entities and relations', () => {
  it('lists entities and reports a click on one', async () => {
    itemFixture = item({ entities: [{ id: 'e1', name: 'Ledger', entity_type: 'concept' }] })
    const { onEntityClick } = renderDetail()
    expect(await screen.findByText('Entities (1)')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'concept Ledger' }))
    expect(onEntityClick).toHaveBeenCalledWith('Ledger')
  })

  it('lists relations and reports clicks on either end, falling back to ids', async () => {
    itemFixture = item({
      content: 'no highlight here',
      relations: [
        { id: 'r1', source_id: 'e1', target_id: 'e2', relation_type: 'depends on', source_name: 'Ledger', target_name: 'Mandate' },
        { id: 'r2', source_id: 'raw-source', target_id: 'raw-target', relation_type: 'mentions' },
      ],
    })
    const { onEntityClick } = renderDetail()
    const card = (await screen.findByText('Relations (2)')).parentElement as HTMLElement
    expect(card).toHaveTextContent('depends on')
    fireEvent.click(within(card).getByRole('button', { name: 'Ledger' }))
    fireEvent.click(within(card).getByRole('button', { name: 'Mandate' }))
    // The second relation carries no display names, so the ids are the labels.
    fireEvent.click(within(card).getByRole('button', { name: 'raw-source' }))
    fireEvent.click(within(card).getByRole('button', { name: 'raw-target' }))
    expect(onEntityClick.mock.calls.map(c => c[0])).toEqual(['Ledger', 'Mandate', 'raw-source', 'raw-target'])
  })
})

describe('DetailView — related items', () => {
  it('fetches and lists related items, showing the shared-entity count when present', async () => {
    itemFixture = item({ entities: [{ id: 'e1', name: 'Ledger', entity_type: 'concept' }] })
    relatedFixture = [
      { ...item({ id: 'r1', title: 'Ledger rollout plan', item_type: 'runbook' }), shared_entities: 3 },
      item({ id: 'r2', title: 'Mandate policy', item_type: 'policy' }),
    ]
    renderDetail()
    const card = (await screen.findByText('Related Items (2)')).parentElement as HTMLElement
    expect(card).toHaveTextContent('Ledger rollout plan')
    expect(card).toHaveTextContent('3 shared')
    expect(card).toHaveTextContent('Mandate policy')
    expect(calls().some(([p]) => p === '/items/i1/related')).toBe(true)
  })

  it('renders nothing when there are no related items', async () => {
    itemFixture = item({ entities: [{ id: 'e1', name: 'Ledger', entity_type: 'concept' }] })
    renderDetail()
    await screen.findByText('Entities (1)')
    expect(screen.queryByText(/Related Items/)).not.toBeInTheDocument()
  })

  it('skips the related request entirely when the item has no entities', async () => {
    renderDetail()
    await screen.findByRole('heading', { name: 'Ledger design notes' })
    await waitFor(() => expect(calls().length).toBeGreaterThan(0))
    expect(calls().some(([p]) => p.endsWith('/related'))).toBe(false)
  })
})

describe('DetailView — content rendering', () => {
  it('highlights entity mentions in raw content and reports a click on one', async () => {
    itemFixture = item({
      content: 'The Ledger keeps every entry, and C++ (core) reads it.',
      entities: [
        { id: 'e1', name: 'Ledger', entity_type: 'concept' },
        { id: 'e2', name: 'C++ (core)', entity_type: 'component' },
      ],
    })
    const { container, onEntityClick } = renderDetail()
    await screen.findByText('Content')
    const pre = container.querySelector('pre') as HTMLElement
    // Regex-special characters in an entity name are escaped before matching,
    // so the mention still becomes its own clickable span.
    fireEvent.click(within(pre).getByRole('button', { name: 'C++ (core)' }))
    fireEvent.click(within(pre).getByRole('button', { name: 'Ledger' }))
    expect(onEntityClick.mock.calls.map(c => c[0])).toEqual(['C++ (core)', 'Ledger'])
  })

  it('leaves content untouched when the item has no entities', async () => {
    const { container } = renderDetail()
    await screen.findByText('Content')
    const pre = container.querySelector('pre') as HTMLElement
    expect(pre).toHaveTextContent('The Ledger keeps every entry.')
    expect(within(pre).queryAllByRole('button')).toHaveLength(0)
  })

  it('renders markdown content through the markdown renderer with a chunk entity strip', async () => {
    itemFixture = item({
      tags: 'ops,content_type:markdown',
      content: '# Ledger',
      entities: [{ id: 'e1', name: 'Ledger', entity_type: 'concept' }],
    })
    const { container, onEntityClick } = renderDetail()
    expect(await screen.findByTestId('markdown')).toHaveTextContent('# Ledger')
    expect(container.querySelector('pre')).toBeNull()
    const strip = (screen.getByText('Entities in this chunk:').parentElement) as HTMLElement
    fireEvent.click(within(strip).getByRole('button', { name: 'Ledger' }))
    expect(onEntityClick).toHaveBeenCalledWith('Ledger')
  })

  it('accepts an array of tags when deciding the content is markdown', async () => {
    itemFixture = item({ tags: ['content_type:markdown'] as unknown as string, content: 'array tagged' })
    renderDetail()
    expect(await screen.findByTestId('markdown')).toHaveTextContent('array tagged')
  })

  it('falls back to the raw view when no tag marks the content as markdown', async () => {
    itemFixture = item({ tags: 'ops,ledger' })
    const { container } = renderDetail()
    await screen.findByText('Content')
    expect(container.querySelector('pre')).toBeTruthy()
    expect(screen.queryByTestId('markdown')).not.toBeInTheDocument()
  })

  it('renders the match score for a search hit', async () => {
    itemFixture = item({ _score: 0.4231, _match_type: 'semantic' })
    renderDetail()
    expect(await screen.findByText(/Match: semantic/)).toHaveTextContent('0.423')
  })

  it('omits the match score for an item opened outside a search', async () => {
    renderDetail()
    await screen.findByText('Content')
    expect(screen.queryByText(/Match:/)).not.toBeInTheDocument()
  })
})

describe('DetailView — tag editor', () => {
  it('renders one chip per tag and no editor until asked', async () => {
    itemFixture = item({ tags: 'ops, ledger' })
    renderDetail()
    expect(await screen.findByText('ops')).toBeInTheDocument()
    expect(screen.getByText('ledger')).toBeInTheDocument()
    expect(screen.queryByLabelText('Comma-separated tags')).not.toBeInTheDocument()
  })

  it('shows a placeholder when the item carries no tags', async () => {
    renderDetail()
    expect(await screen.findByText('No tags')).toBeInTheDocument()
  })

  it('saves edited tags on Enter and closes the editor', async () => {
    itemFixture = item({ tags: 'ops' })
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'edit' }))
    const input = screen.getByLabelText('Comma-separated tags')
    fireEvent.change(input, { target: { value: 'ops, ledger' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(callsTo('PATCH')).toHaveLength(1))
    expect(bodyOf(callsTo('PATCH')[0][1])).toEqual({ tags: 'ops, ledger' })
    await waitFor(() => expect(screen.queryByLabelText('Comma-separated tags')).not.toBeInTheDocument())
  })

  it('saves edited tags from the save button', async () => {
    itemFixture = item({ tags: 'ops' })
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'edit' }))
    fireEvent.change(screen.getByLabelText('Comma-separated tags'), { target: { value: 'ledger' } })
    fireEvent.click(screen.getByRole('button', { name: 'save' }))
    await waitFor(() => expect(callsTo('PATCH')).toHaveLength(1))
    expect(bodyOf(callsTo('PATCH')[0][1])).toEqual({ tags: 'ledger' })
  })

  it('abandons the edit from the cancel button without a request', async () => {
    itemFixture = item({ tags: 'ops' })
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'edit' }))
    fireEvent.change(screen.getByLabelText('Comma-separated tags'), { target: { value: 'dropped' } })
    fireEvent.click(screen.getByRole('button', { name: 'cancel' }))
    expect(await screen.findByRole('button', { name: 'edit' })).toBeInTheDocument()
    expect(callsTo('PATCH')).toHaveLength(0)
  })

  it('abandons the edit on Escape without a request', async () => {
    itemFixture = item({ tags: 'ops' })
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'edit' }))
    fireEvent.keyDown(screen.getByLabelText('Comma-separated tags'), { key: 'Escape' })
    expect(await screen.findByRole('button', { name: 'edit' })).toBeInTheDocument()
    expect(callsTo('PATCH')).toHaveLength(0)
  })
})

describe('DetailView — copy and export', () => {
  it('copies the content and reverts the button label once the flash expires', async () => {
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /Copy Content/ }))
    expect(writeText).toHaveBeenCalledWith('The Ledger keeps every entry.')
    expect(await screen.findByRole('button', { name: /Copied!/ })).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(1600) })
    expect(await screen.findByRole('button', { name: /Copy Content/ })).toBeInTheDocument()
  })

  it('copies the summary when the item has no content', async () => {
    itemFixture = item({ content: '', summary: 'Ledger summary only' })
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /Copy Content/ }))
    expect(writeText).toHaveBeenCalledWith('Ledger summary only')
  })

  it('copies the title when the item has neither content nor summary', async () => {
    itemFixture = item({ content: '' })
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /Copy Content/ }))
    expect(writeText).toHaveBeenCalledWith('Ledger design notes')
  })

  it('exports through a download link named after the item', async () => {
    renderDetail()
    const btn = await screen.findByRole('button', { name: /Export/ })
    const anchorClick = vi.fn()
    const anchors: HTMLAnchorElement[] = []
    const create = document.createElement.bind(document)
    const spy = vi.spyOn(document, 'createElement').mockImplementation(((tag: string, opts?: ElementCreationOptions) => {
      const el = create(tag, opts)
      if (tag === 'a') {
        const a = el as HTMLAnchorElement
        a.click = anchorClick
        anchors.push(a)
      }
      return el
    }) as typeof document.createElement)
    try {
      fireEvent.click(btn)
    } finally {
      spy.mockRestore()
    }
    expect(anchorClick).toHaveBeenCalledTimes(1)
    expect(anchors[0].getAttribute('href')).toBe('/api/knowledge/items/i1/export')
    expect(anchors[0].download).toBe('Ledger design notes.knowledge')
  })
})

describe('DetailView — archive and delete', () => {
  const seededList = (qc: QueryClient) => {
    qc.setQueryData(['knowledge-items'], { items: [item(), item({ id: 'i2', title: 'Other' })], total: 2 })
    return qc
  }
  type ListCache = { items: KnowledgeItem[]; total: number } | undefined

  it('archives an active item, prunes it from the cached list and returns to the list', async () => {
    const qc = seededList(makeClient())
    const { onBack } = renderDetail({}, qc)
    fireEvent.click(await screen.findByRole('button', { name: /Archive/ }))
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    expect(bodyOf(callsTo('PATCH')[0][1])).toEqual({ status: 'archived' })
    const cached = qc.getQueryData(['knowledge-items']) as ListCache
    expect(cached?.items.map(i => i.id)).toEqual(['i2'])
    expect(cached?.total).toBe(1)
  })

  it('unarchives an archived item', async () => {
    itemFixture = item({ status: 'archived' })
    const { onBack } = renderDetail()
    expect(screen.queryByRole('button', { name: /^Archive$/ })).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: /Unarchive/ }))
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    expect(bodyOf(callsTo('PATCH')[0][1])).toEqual({ status: 'active' })
  })

  it('restores the cached list and surfaces the error when archiving fails', async () => {
    mutationError = new Error('archive refused')
    const qc = seededList(makeClient())
    const { onBack } = renderDetail({}, qc)
    fireEvent.click(await screen.findByRole('button', { name: /Archive/ }))
    expect(await screen.findByText('archive refused', {}, { timeout: 5_000 })).toBeInTheDocument()
    expect(onBack).not.toHaveBeenCalled()
    const cached = qc.getQueryData(['knowledge-items']) as ListCache
    expect(cached?.items.map(i => i.id)).toEqual(['i1', 'i2'])
    expect(cached?.total).toBe(2)
  })

  it('leaves an empty cached list entry alone while archiving', async () => {
    const qc = makeClient()
    qc.setQueryData(['knowledge-items', 'page-2'], null)
    const { onBack } = renderDetail({}, qc)
    fireEvent.click(await screen.findByRole('button', { name: /Archive/ }))
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    expect(qc.getQueryData(['knowledge-items', 'page-2'])).toBeNull()
  })

  it('deletes the item once the confirmation is accepted', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const qc = seededList(makeClient())
    const { onBack } = renderDetail({}, qc)
    fireEvent.click(await screen.findByRole('button', { name: /Delete/ }))
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    expect(callsTo('DELETE')).toHaveLength(1)
    expect(callsTo('DELETE')[0][0]).toBe('/items/i1')
    expect((qc.getQueryData(['knowledge-items']) as ListCache)?.items.map(i => i.id)).toEqual(['i2'])
  })

  it('leaves an empty cached list entry alone while deleting', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const qc = makeClient()
    qc.setQueryData(['knowledge-items', 'page-2'], null)
    const { onBack } = renderDetail({}, qc)
    fireEvent.click(await screen.findByRole('button', { name: /Delete/ }))
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1))
    expect(qc.getQueryData(['knowledge-items', 'page-2'])).toBeNull()
  })

  it('does nothing when the delete confirmation is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { onBack } = renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /Delete/ }))
    expect(callsTo('DELETE')).toHaveLength(0)
    expect(onBack).not.toHaveBeenCalled()
  })

  it('surfaces a failed delete and rolls the cached list back', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mutationError = new Error('delete refused')
    const qc = seededList(makeClient())
    renderDetail({}, qc)
    fireEvent.click(await screen.findByRole('button', { name: /Delete/ }))
    expect(await screen.findByText('delete refused', {}, { timeout: 5_000 })).toBeInTheDocument()
    expect((qc.getQueryData(['knowledge-items']) as ListCache)?.items).toHaveLength(2)
  })

  it('falls back to a generic message when the failure carries no text', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mutationError = new Error('')
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: /Delete/ }))
    expect(await screen.findByText('Action failed', {}, { timeout: 5_000 })).toBeInTheDocument()
  })
})
