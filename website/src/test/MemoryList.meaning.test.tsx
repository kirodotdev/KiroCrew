import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { memoryRecordParams } from '../api/client/memory'

/**
 * The memory list's search by meaning: the Words | Meaning switch changes the
 * request, weaker matches stay counted behind one row, and saved topics run,
 * save and remove from the Topics row.
 */
const { api } = vi.hoisted(() => ({ api: {
  memoryRecords: vi.fn(),
  memoryStores: vi.fn(),
  memoryRecordHistory: vi.fn(),
} }))
vi.mock('../api/client', () => ({ api }))
const { default: MemoryList } = await import('../pages/overview/memoryPage/MemoryList')

const STORE = 'member-reviewer-abc'
const FACT = { kind: 'fact', id: 'pref.review.language', key: 'pref.review.language', value_json: '"Review comments in English"', text: '', importance: 0.95, source: 'consolidation', updated_at: '2026-11-15T10:00:00Z', created_at: '2026-11-01T10:00:00Z', revision: 'a'.repeat(64), match: 0.71 }
const FACETS = { kind: { fact: 1 }, category: {}, applies: {}, scope: {}, source: { consolidation: 1 }, tag: {} }

function List() {
  return <MemoryList store={STORE} storeName="reviewer" privateMemory selecting={false} onSelectingChange={() => {}} recallOpen={false} onRecallOpenChange={() => {}} />
}
const lastView = () => api.memoryRecords.mock.calls.at(-1)!

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  localStorage.clear()
  api.memoryRecords.mockResolvedValue({ entries: [FACT], total: 1, has_more: false, facets: FACETS, hidden: 4 })
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false, current_revision: 1 })
})

async function searchByMeaning(text: string) {
  fireEvent.click(screen.getByRole('radio', { name: 'Meaning' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: text } })
  await waitFor(() => expect(lastView()[1]).toEqual({ q: text, kind: 'all' }))
}

describe('search by meaning', () => {
  it('sends mode=meaning only with words, and weak=1 only after Show them', () => {
    const view = { sort: 'match' as const, order: 'desc' as const, facets: true, meaning: true }
    expect(memoryRecordParams(STORE, { q: 'style', kind: 'all' }, 0, 50, view).get('mode')).toBe('meaning')
    expect(memoryRecordParams(STORE, { q: 'style', kind: 'all' }, 0, 50, view).has('weak')).toBe(false)
    expect(memoryRecordParams(STORE, { q: 'style', kind: 'all' }, 0, 50, { ...view, weak: true }).get('weak')).toBe('1')
    expect(memoryRecordParams(STORE, { q: 'style', kind: 'all' }, 0, 50, { sort: 'match' }).has('mode')).toBe(false)
  })

  it('ranks by meaning, labels the score, and lists the weaker matches on request', async () => {
    renderWithProviders(<List />)
    await screen.findByText('Review comments in English')
    // With no words, Meaning has nothing to embed: the plain list stays.
    fireEvent.click(screen.getByRole('radio', { name: 'Meaning' }))
    expect(lastView()[4]).not.toHaveProperty('meaning')
    fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'coding style' } })
    await waitFor(() => expect(lastView()[4]).toEqual(expect.objectContaining({ meaning: true, weak: false, sort: 'match' })))
    expect(await screen.findByText('Match 0.71')).toBeVisible()
    expect(screen.getByText('Weaker matches hidden: 4')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Show them' }))
    await waitFor(() => expect(lastView()[4]).toEqual(expect.objectContaining({ meaning: true, weak: true })))
    await waitFor(() => expect(screen.queryByText('Weaker matches hidden: 4')).toBeNull())
    fireEvent.click(screen.getByRole('radio', { name: 'Words' }))
    await waitFor(() => expect(lastView()[4]).not.toHaveProperty('meaning'))
  })

  it('saves a topic, runs it with one click, and removes it', async () => {
    renderWithProviders(<List />)
    await screen.findByText('Review comments in English')
    await searchByMeaning('coding style')
    fireEvent.click(await screen.findByRole('button', { name: 'Save “coding style”' }))
    const topics = screen.getByRole('group', { name: 'Topics' })
    expect(within(topics).getByRole('button', { name: 'coding style' })).toBeVisible()
    expect(JSON.parse(localStorage.getItem('kirocrew:memory-topics')!)).toEqual(['coding style'])
    expect(screen.queryByRole('button', { name: 'Save “coding style”' })).toBeNull()
    // Back in Words mode with other words, one click runs the saved topic by meaning.
    fireEvent.click(screen.getByRole('radio', { name: 'Words' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'tabs' } })
    await waitFor(() => expect(lastView()[1]).toEqual({ q: 'tabs', kind: 'all' }))
    fireEvent.click(within(topics).getByRole('button', { name: 'coding style' }))
    await waitFor(() => expect(lastView()[4]).toEqual(expect.objectContaining({ meaning: true })))
    expect(lastView()[1]).toEqual({ q: 'coding style', kind: 'all' })
    expect(screen.getByRole('textbox', { name: 'Search memory' })).toHaveValue('coding style')
    fireEvent.click(within(topics).getByRole('button', { name: 'Remove topic: coding style' }))
    expect(JSON.parse(localStorage.getItem('kirocrew:memory-topics')!)).toEqual([])
  })

  it('offers Search by words when the store has no embedding model', async () => {
    api.memoryRecords.mockImplementation(async (_store: string, _query: unknown, _offset: number, _limit: number, view: { meaning?: boolean }) => {
      if (view.meaning) throw Object.assign(new Error('Search by meaning needs the embedding model, which is not ready.'), { status: 409 })
      return { entries: [FACT], total: 1, has_more: false, facets: FACETS }
    })
    renderWithProviders(<List />)
    await screen.findByText('Review comments in English')
    await searchByMeaning('coding style')
    expect(await screen.findByText('Search by meaning is not available')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Search by words' }))
    await waitFor(() => expect(lastView()[4]).not.toHaveProperty('meaning'))
  })
})
