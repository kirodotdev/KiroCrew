/**
 * Archive and Delete in the Knowledge detail view work while the list's
 * caches are loaded.
 *
 * Both actions first drop the item from every cached list under the shared
 * 'knowledge-items' prefix, so the list is already right when the reader goes
 * back. That prefix also holds the source-first view's counts, whose payload
 * has no `items` array. Rewriting that entry as a list threw before the
 * request was sent, so the action failed with a TypeError and changed nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DetailView from '../pages/knowledge/DetailView'
import * as api from '../pages/knowledge/api'
import type { KnowledgeItem } from '../pages/knowledge/types'
import { i18nT } from '../i18n/t'
import { namedCeiling } from './namedCeiling'

// Each wait spans the item read and then the action's request.
const DETAIL_ACTION_CHAIN = () => namedCeiling('DETAIL_ACTION_CHAIN', 5000)

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
  tags: '',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-02-03T00:00:00Z',
  entities: [],
}
const OTHER: KnowledgeItem = { ...ITEM, id: 'i2', title: 'Other notes' }

// The keys index.tsx and SourceGroup.tsx read the source-first list with.
const COUNTS_KEY = ['knowledge-items', 'source-counts', { typeFilter: '', statusFilter: 'active', namespaceFilter: '' }]
const SOURCE_ITEMS_KEY = ['knowledge-items', 'source-items', 's1', 1, {}]

function renderWithListCache() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  qc.setQueryData(COUNTS_KEY, { counts: { s1: 2 }, total: 2 })
  qc.setQueryData(SOURCE_ITEMS_KEY, { items: [ITEM, OTHER], total: 2 })
  const onBack = vi.fn()
  render(
    <QueryClientProvider client={qc}>
      <DetailView itemId="i1" onBack={onBack} />
    </QueryClientProvider>,
  )
  return { qc, onBack }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.knowledgeApi).mockImplementation(((path: string, opts?: RequestInit) => {
    if (opts?.method === 'DELETE' || opts?.method === 'PATCH') return Promise.resolve({ ok: true })
    if (path.endsWith('/related')) return Promise.resolve([])
    return Promise.resolve(ITEM)
  }) as typeof api.knowledgeApi)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

const sentWith = (method: string) =>
  vi.mocked(api.knowledgeApi).mock.calls.filter(([, opts]) => opts?.method === method)

describe('DetailView actions with the source-first list cached', () => {
  it('deletes the item and drops it from the cached source list', async () => {
    const { qc, onBack } = renderWithListCache()
    fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.knowledge.detailView.delete') }, DETAIL_ACTION_CHAIN()))

    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1), DETAIL_ACTION_CHAIN())
    expect(sentWith('DELETE').map(([path]) => path)).toEqual(['/items/i1'])
    expect(screen.queryByText(/Cannot read properties/)).toBeNull()
    expect(qc.getQueryData<{ items: KnowledgeItem[] }>(SOURCE_ITEMS_KEY)?.items.map(i => i.id)).toEqual(['i2'])
    // The counts entry is not a list, so it is left for the refetch to correct.
    expect(qc.getQueryData(COUNTS_KEY)).toEqual({ counts: { s1: 2 }, total: 2 })
  })

  it('archives the item and drops it from the cached source list', async () => {
    const { qc, onBack } = renderWithListCache()
    fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.knowledge.detailView.archive') }, DETAIL_ACTION_CHAIN()))

    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1), DETAIL_ACTION_CHAIN())
    expect(sentWith('PATCH').map(([path]) => path)).toEqual(['/items/i1'])
    expect(qc.getQueryData<{ items: KnowledgeItem[] }>(SOURCE_ITEMS_KEY)?.items.map(i => i.id)).toEqual(['i2'])
    expect(qc.getQueryData(COUNTS_KEY)).toEqual({ counts: { s1: 2 }, total: 2 })
  })
})
