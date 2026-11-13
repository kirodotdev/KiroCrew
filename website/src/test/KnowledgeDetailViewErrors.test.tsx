/**
 * A failed request in the Knowledge detail view says so.
 *
 * Each case here used to fail with no message: a failed item read rendered
 * "Item not found", a refused tag save left the editor open with nothing said,
 * and a failed related-items read hid the card as if there were none. Each now
 * renders an ErrorNotice carrying the server's reason.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DetailView from '../pages/knowledge/DetailView'
import * as api from '../pages/knowledge/api'
import type { KnowledgeItem } from '../pages/knowledge/types'
import { i18nT } from '../i18n/t'
import { namedCeiling } from './namedCeiling'
// Every wait here spans a request chain (item read, then related read; save,
// then re-read), so each carries a named lost-run ceiling (website/docs/testing.md).
const DETAIL_REQUEST_CHAIN = () => namedCeiling('DETAIL_REQUEST_CHAIN', 5000)

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))

const ITEM: KnowledgeItem = {
  id: 'i1',
  title: 'Ledger design notes',
  item_type: 'design_doc',
  status: 'active',
  content: 'The Ledger keeps every entry.',
  tags: 'alpha',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-02-03T00:00:00Z',
  // One entity, so the related-items read is enabled.
  entities: [{ id: 'e1', name: 'Ledger', entity_type: 'concept' }],
}

const EDIT = i18nT('pages.knowledge.detailView.edit')
const SAVE = i18nT('pages.knowledge.detailView.save')
const CANCEL = i18nT('pages.knowledge.detailView.cancel')
const TAGS = i18nT('pages.knowledge.detailView.comma_separated_tags')

// Per-test knobs: which request fails, and with what.
let itemError: Error | null
let relatedError: Error | null
let saveError: Error | null

function route(path: string, opts?: RequestInit): Promise<unknown> {
  if (opts?.method === 'PATCH') return saveError ? Promise.reject(saveError) : Promise.resolve({ ok: true })
  if (path.endsWith('/related')) return relatedError ? Promise.reject(relatedError) : Promise.resolve([])
  return itemError ? Promise.reject(itemError) : Promise.resolve(ITEM)
}

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <DetailView itemId="i1" onBack={vi.fn()} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  itemError = null
  relatedError = null
  saveError = null
  vi.mocked(api.knowledgeApi).mockImplementation(route as typeof api.knowledgeApi)
})

describe('DetailView — failed requests are shown', () => {
  it('a failed item read shows its reason, not "Item not found"', async () => {
    itemError = new Error('500 Internal Server Error')
    renderDetail()
    const notice = await screen.findByTestId('knowledge-item-load-error', {}, DETAIL_REQUEST_CHAIN())
    expect(notice).toHaveTextContent("Couldn't load this item")
    expect(notice).toHaveTextContent('500 Internal Server Error')
    expect(screen.queryByText('Item not found')).not.toBeInTheDocument()
    // The way back stays on screen.
    expect(screen.getByRole('button', { name: /Back to list/ })).toBeInTheDocument()
  })

  it('a refused tag save says why and keeps the draft open', async () => {
    saveError = new Error('tags too long')
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: EDIT }, DETAIL_REQUEST_CHAIN()))
    const input = screen.getByLabelText(TAGS)
    fireEvent.change(input, { target: { value: 'alpha, beta' } })
    fireEvent.click(screen.getByRole('button', { name: SAVE }))
    const notice = await screen.findByTestId('knowledge-tags-save-error', {}, DETAIL_REQUEST_CHAIN())
    expect(notice).toHaveTextContent("Couldn't save the tags")
    expect(notice).toHaveTextContent('tags too long')
    // The draft is not thrown away, and nothing offers to navigate off it.
    expect(screen.getByLabelText(TAGS)).toHaveValue('alpha, beta')
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
  })

  it('closing the tag editor clears its save failure', async () => {
    saveError = new Error('tags too long')
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: EDIT }, DETAIL_REQUEST_CHAIN()))
    fireEvent.click(screen.getByRole('button', { name: SAVE }))
    await screen.findByTestId('knowledge-tags-save-error', {}, DETAIL_REQUEST_CHAIN())
    fireEvent.click(screen.getByRole('button', { name: CANCEL }))
    fireEvent.click(await screen.findByRole('button', { name: EDIT }, DETAIL_REQUEST_CHAIN()))
    expect(screen.queryByTestId('knowledge-tags-save-error')).not.toBeInTheDocument()
  })

  it('a failed related-items read is shown instead of hidden', async () => {
    relatedError = new Error('related index offline')
    renderDetail()
    const notice = await screen.findByTestId('knowledge-related-error', {}, DETAIL_REQUEST_CHAIN())
    expect(notice).toHaveTextContent("Couldn't load related items")
    expect(notice).toHaveTextContent('related index offline')
  })

  it('a failed refresh after a tag save is shown over the cached item', async () => {
    renderDetail()
    fireEvent.click(await screen.findByRole('button', { name: EDIT }, DETAIL_REQUEST_CHAIN()))
    // The save succeeds; the re-read it triggers fails.
    itemError = new Error('database is locked')
    fireEvent.click(screen.getByRole('button', { name: SAVE }))
    const notice = await screen.findByTestId('knowledge-item-load-error', {}, DETAIL_REQUEST_CHAIN())
    expect(notice).toHaveTextContent('database is locked')
    // The cached item stays on screen under the notice.
    expect(screen.getByText('Ledger design notes')).toBeInTheDocument()
  })

  it('a failure with no text falls back to a plain reason', async () => {
    itemError = new Error('')
    renderDetail()
    const notice = await screen.findByTestId('knowledge-item-load-error', {}, DETAIL_REQUEST_CHAIN())
    expect(notice).toHaveTextContent('The request failed without a reason.')
  })
})
