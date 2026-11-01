import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { namedCeiling } from './namedCeiling'
// Every wait here spans a request chain or the lazy Graph tab, so each carries
// a named lost-run ceiling (website/docs/testing.md).
const PAGE_REQUEST_CHAIN = () => namedCeiling('PAGE_REQUEST_CHAIN', 5000)

/**
 * A failed request on the Knowledge page shell says so.
 *
 * Each case here used to fail with no message: a refused bulk archive or delete
 * put the rows back with nothing said, a failed list read rendered onboarding or
 * "no matches", and a failed entity read rendered "No items found". Each now
 * renders an ErrorNotice carrying the server's reason.
 *
 * The heavy children are stubbed, as in KnowledgeIndexCoverage.test.tsx: the
 * contract under test is the page's own.
 */
const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))
vi.mock('../pages/knowledge/DetailView', () => ({ default: () => <div>detail</div> }))
vi.mock('../pages/knowledge/SourcesList', () => ({ default: () => <div>sources</div> }))
vi.mock('../pages/knowledge/KnowledgeGraph', () => ({
  default: ({ onSelectEntity }: { onSelectEntity: (name: string) => void }) => (
    <button onClick={() => onSelectEntity('Ledger')}>stub-graph-pick</button>
  ),
}))

const { default: KnowledgePage } = await import('../pages/knowledge/index')

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

// Per-test knobs: which request fails, and with what.
let mutationError: Error | null
let countsError: Error | null
let searchError: Error | null
let entityItemsError: Error | null
let countsEmpty: boolean

function api(path: string): Promise<unknown> {
  const p = String(path)
  if (p.startsWith('/items/')) return mutationError ? Promise.reject(mutationError) : Promise.resolve({ ok: true })
  if (p.startsWith('/items')) return searchError ? Promise.reject(searchError) : Promise.resolve({ items: [FLAT_ITEM], total: 1 })
  if (p.startsWith('/source-counts')) return countsError ? Promise.reject(countsError) : Promise.resolve(countsEmpty ? { counts: {}, total: 0 } : { counts: { s1: 1 }, total: 1 })
  if (p.startsWith('/entities/by-name/')) return entityItemsError ? Promise.reject(entityItemsError) : Promise.resolve([])
  if (p === '/sources') return Promise.resolve([{ id: 's1', name: 'Notes', source_type: 'local_folder', uri: '/n', sync_status: 'synced', item_count: 1 }])
  if (p === '/stats') return Promise.resolve({ items: 1, entities: 0, relations: 0, sources: 1 })
  if (p === '/config') return Promise.resolve({ enabled: true, supported_formats: ['.md'] })
  return Promise.resolve([])
}

let qc: QueryClient
function renderPage() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}><KnowledgePage /></QueryClientProvider>
    </MemoryRouter>,
  )
}

async function searchAndSelect() {
  await userEvent.type(await screen.findByPlaceholderText(/Search knowledge/i, {}, PAGE_REQUEST_CHAIN()), 'ledger{Enter}')
  await userEvent.click(await screen.findByLabelText('Select Quarterly ledger notes', {}, PAGE_REQUEST_CHAIN()))
  expect(await screen.findByText('1 selected', {}, PAGE_REQUEST_CHAIN())).toBeInTheDocument()
}

beforeEach(() => {
  vi.clearAllMocks()
  mutationError = null
  countsError = null
  searchError = null
  entityItemsError = null
  countsEmpty = false
  mockKnowledgeApi.mockImplementation((path: string) => api(path))
})

afterEach(() => {
  qc?.clear()
  vi.unstubAllGlobals()
})

describe('KnowledgePage — failed requests are shown', () => {
  it('a refused bulk archive says why', async () => {
    renderPage()
    await searchAndSelect()
    mutationError = new Error('patch refused')
    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))
    const notice = await screen.findByTestId('knowledge-bulk-error', {}, PAGE_REQUEST_CHAIN())
    expect(notice).toHaveTextContent("Couldn't archive the selected items")
    expect(notice).toHaveTextContent('patch refused')
  })

  it('a refused bulk delete says why', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    renderPage()
    await searchAndSelect()
    mutationError = new Error('delete refused')
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    const notice = await screen.findByTestId('knowledge-bulk-error', {}, PAGE_REQUEST_CHAIN())
    expect(notice).toHaveTextContent("Couldn't delete the selected items")
    expect(notice).toHaveTextContent('delete refused')
  })

  it('a bulk action clears the previous action\'s failure', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    renderPage()
    await searchAndSelect()
    mutationError = new Error('patch refused')
    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await screen.findByTestId('knowledge-bulk-error', {}, PAGE_REQUEST_CHAIN())
    mutationError = new Error('delete refused')
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.getByTestId('knowledge-bulk-error')).toHaveTextContent('delete refused'), PAGE_REQUEST_CHAIN())
    expect(screen.getByTestId('knowledge-bulk-error')).toHaveTextContent("Couldn't delete the selected items")
  })

  it('a failed source list read is not shown as an empty library', async () => {
    countsError = new Error('503 Service Unavailable')
    renderPage()
    const notice = await screen.findByTestId('knowledge-list-load-error', {}, PAGE_REQUEST_CHAIN())
    expect(notice).toHaveTextContent("Couldn't load the knowledge items")
    expect(notice).toHaveTextContent('503 Service Unavailable')
    expect(screen.queryByTestId('knowledge-onboarding')).not.toBeInTheDocument()
    expect(screen.queryByText('No items match your filters')).not.toBeInTheDocument()
  })

  it('a failed search is not shown as "no matches"', async () => {
    searchError = new Error('search backend down')
    renderPage()
    await userEvent.type(await screen.findByPlaceholderText(/Search knowledge/i, {}, PAGE_REQUEST_CHAIN()), 'ledger{Enter}')
    const notice = await screen.findByTestId('knowledge-list-load-error', {}, PAGE_REQUEST_CHAIN())
    expect(notice).toHaveTextContent('search backend down')
    expect(screen.queryByText('No items match your search')).not.toBeInTheDocument()
  })

  it('a failed entity read is not shown as "No items found"', async () => {
    entityItemsError = new Error('entity lookup failed')
    renderPage()
    await userEvent.click(await screen.findByRole('button', { name: /Graph/ }, PAGE_REQUEST_CHAIN()))
    await userEvent.click(await screen.findByRole('button', { name: 'stub-graph-pick' }, PAGE_REQUEST_CHAIN()))
    const notice = await screen.findByTestId('knowledge-entity-items-error', {}, PAGE_REQUEST_CHAIN())
    expect(notice).toHaveTextContent("Couldn't load the items for this entity")
    expect(notice).toHaveTextContent('entity lookup failed')
    expect(screen.queryByText('No items found')).not.toBeInTheDocument()
  })

  it('a bulk failure that lands after the selection empties is still shown', async () => {
    renderPage()
    await searchAndSelect()
    // Hold the PATCH open so the optimistic update, which hides the row and so
    // empties the selection, renders before the failure arrives.
    let fail!: (e: Error) => void
    mockKnowledgeApi.mockImplementation((path: string, opts?: RequestInit) =>
      String(path).startsWith('/items/') && opts?.method === 'PATCH'
        ? new Promise((_, reject) => { fail = reject })
        : api(path))
    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(screen.queryByText('1 selected')).not.toBeInTheDocument(), PAGE_REQUEST_CHAIN())
    await act(async () => { fail(new Error('patch refused late')) })
    const notice = await screen.findByTestId('knowledge-bulk-error', {}, PAGE_REQUEST_CHAIN())
    expect(notice).toHaveTextContent('patch refused late')
  })

  it('a failed refresh over a cached empty library is not shown as onboarding', async () => {
    countsEmpty = true
    renderPage()
    expect(await screen.findByTestId('knowledge-onboarding', {}, PAGE_REQUEST_CHAIN())).toBeInTheDocument()
    countsError = new Error('refresh failed')
    await act(async () => { await qc.invalidateQueries({ queryKey: ['knowledge-items'] }) })
    expect(await screen.findByTestId('knowledge-list-load-error', {}, PAGE_REQUEST_CHAIN())).toHaveTextContent('refresh failed')
    expect(screen.queryByTestId('knowledge-onboarding')).not.toBeInTheDocument()
    expect(screen.queryByText('No items match your filters')).not.toBeInTheDocument()
  })

  it('a successful read shows no notice', async () => {
    renderPage()
    expect(await screen.findByText('Notes', {}, PAGE_REQUEST_CHAIN())).toBeInTheDocument()
    expect(screen.queryByTestId('knowledge-list-load-error')).not.toBeInTheDocument()
  })
})
