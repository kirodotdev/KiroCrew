import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { memoryRecordParams } from '../api/client/memory'

/**
 * The memory page's record list: it asks the records route for a sorted page
 * with facet counts, and every pill, filter, sort and Test recall control
 * changes that request or opens the matching surface.
 */
const { api } = vi.hoisted(() => ({ api: {
  memoryRecords: vi.fn(),
  memoryStores: vi.fn(),
  memoryRecall: vi.fn(),
  memoryRecordHistory: vi.fn(),
} }))
vi.mock('../api/client', () => ({ api }))
const { default: MemoryList } = await import('../pages/overview/memoryPage/MemoryList')

const STORE = 'member-reviewer-abc'
const LESSON = { kind: 'directive', id: 'lesson.tool', key: 'lesson.tool', value_json: JSON.stringify({ rule: 'Work in a worktree', category: 'tool', applies: 'always', negative: 'Editing the live checkout' }), text: '', importance: 0.95, source: 'user_explicit', updated_at: '2026-11-20T10:00:00Z', created_at: '2026-11-01T10:00:00Z', revision: 'a'.repeat(64) }
const EPISODE = { kind: 'episode', id: 'ep-1', text: 'Cleaned up stale worktrees', tags: '["release","worktree"]', importance: 0.6, source: 'consolidation', updated_at: '2026-11-13T10:00:00Z', created_at: '2026-11-13T10:00:00Z', revision: 'b'.repeat(64) }
const FACETS = {
  kind: { fact: 412, directive: 161, episode: 711 },
  category: { tool: 71, preference: 52, knowledge: 38 }, applies: { always: 97, on_topic: 64 }, scope: { '': 77 },
  source: { user_explicit: 300, consolidation: 900 }, tag: { release: 9, worktree: 6 },
}

function List(props: Partial<Parameters<typeof MemoryList>[0]>) {
  return <MemoryList store={STORE} storeName="reviewer" privateMemory selecting={false} onSelectingChange={() => {}} recallOpen={false} onRecallOpenChange={() => {}} {...props} />
}
const lastView = () => api.memoryRecords.mock.calls.at(-1)!

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  api.memoryRecords.mockResolvedValue({ entries: [LESSON, EPISODE], total: 2, has_more: false, facets: FACETS })
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.memoryRecall.mockResolvedValue({ semantic_context: '', episodic_context: '', lessons_context: '', retrieval: { facts: [], episodes: [] } })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false, current_revision: 1 })
})

describe('memoryRecordParams', () => {
  it('sends the sort, the facet request and each facet value as its own parameter', () => {
    const params = memoryRecordParams(STORE, { q: 'tree', kind: 'directive', filters: { category: ['tool', 'preference'], conf_min: 0.5 } }, 50, 50, { sort: 'confidence', order: 'asc', facets: true })
    expect(params.get('sort')).toBe('confidence')
    expect(params.get('order')).toBe('asc')
    expect(params.get('facets')).toBe('1')
    expect(params.getAll('category')).toEqual(['tool', 'preference'])
    expect(params.get('conf_min')).toBe('0.5')
    expect(params.has('conf_max')).toBe(false)
    expect(memoryRecordParams(STORE, { q: '', kind: 'all' }, 0, 50).has('sort')).toBe(false)
  })
})

describe('the memory list selection', () => {
  it('drops hand-picked rows when the kind pill changes the list', async () => {
    renderWithProviders(<List selecting />)
    fireEvent.click(await screen.findByRole('checkbox', { name: /^Select memory: Work in a worktree/ }))
    expect(screen.getByText('Selected: 1')).toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: /^Experiences/ }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'episode' }))
    await waitFor(() => expect(screen.getByText('Selected: 0')).toBeVisible())
    expect(screen.getByRole('button', { name: /^Forget selected memories/ })).toBeDisabled()
  })
})

describe('the memory list', () => {
  it('asks for the newest first with facet counts, and labels each pill and score', async () => {
    renderWithProviders(<List />)
    expect(await screen.findByText('Work in a worktree')).toBeVisible()
    expect(api.memoryRecords).toHaveBeenCalledWith(STORE, { q: '', kind: 'all' }, 0, 50, { sort: 'updated', order: 'desc', facets: true })
    expect(screen.getByRole('radio', { name: /^All\s*1,284$/ })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: /^Lessons\s*161$/ })).toBeVisible()
    const rows = screen.getByRole('list', { name: 'Memories' })
    expect(within(rows).getByText('Lesson · Tool')).toBeVisible()
    expect(within(rows).getByText('Every session')).toBeVisible()
    expect(within(rows).getByText('Confidence 0.95')).toBeVisible()
    expect(within(rows).getByText('Importance 0.60')).toBeVisible()
    expect(within(rows).getByText('Don’t: Editing the live checkout')).toBeVisible()
    expect(within(rows).getByText('#worktree')).toBeVisible()
  })

  it('filters by a ticked facet value, shows it as a chip, and clears it', async () => {
    renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    fireEvent.click(await within(rail).findByRole('checkbox', { name: 'Tool' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { category: ['tool'] } }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Tool' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all' }))
    // A lesson facet means nothing on the Experiences pill, so that pill does not send it.
    fireEvent.click(await within(rail).findByRole('checkbox', { name: 'Tool' }))
    fireEvent.click(screen.getByRole('radio', { name: /^Experiences/ }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'episode' }))
  })

  it('sorts by the picked field and reverses the order in one press', async () => {
    renderWithProviders(<List />)
    await screen.findByText('Work in a worktree')
    fireEvent.pointerDown(screen.getByRole('button', { name: /^Sort: Updated/ }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /^Confidence/ }))
    await waitFor(() => expect(lastView()[4]).toEqual({ sort: 'confidence', order: 'desc', facets: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Reverse order' }))
    await waitFor(() => expect(lastView()[4]).toEqual({ sort: 'confidence', order: 'asc', facets: true }))
  })

  it('ranks a search by Best match, and falls back to the picked sort when it is cleared', async () => {
    renderWithProviders(<List />)
    await screen.findByText('Work in a worktree')
    fireEvent.change(screen.getByRole('textbox', { name: 'Search memory' }), { target: { value: 'worktree' } })
    await waitFor(() => expect(lastView()[1]).toEqual({ q: 'worktree', kind: 'all' }))
    expect(lastView()[4]).toEqual({ sort: 'match', order: 'desc', facets: true })
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    await waitFor(() => expect(lastView()[4]).toEqual({ sort: 'updated', order: 'desc', facets: true }))
  })

  it('opens Test recall with an empty task when the search box is empty, and recalls only once one is typed', async () => {
    const onRecallOpenChange = vi.fn()
    renderWithProviders(<List recallOpen onRecallOpenChange={onRecallOpenChange} />)
    const dialog = await screen.findByRole('dialog', { name: 'Test recall' })
    const task = within(dialog).getByRole('textbox', { name: 'Task' })
    expect(task).toHaveValue('')
    const run = within(dialog).getByRole('button', { name: 'Test recall' })
    expect(run).toBeDisabled()
    expect(api.memoryRecall).not.toHaveBeenCalled()
    fireEvent.change(task, { target: { value: 'fix the flaky CI test' } })
    fireEvent.click(run)
    await waitFor(() => expect(api.memoryRecall).toHaveBeenCalledWith('fix the flaky CI test', STORE))
  })

  it('opens a record in the detail panel with every field the row summarizes', async () => {
    renderWithProviders(<List />)
    fireEvent.click((await screen.findAllByRole('button', { name: 'View details' }))[0])
    const detail = screen.getByTestId('memory-record-detail')
    expect(within(detail).getByRole('heading', { name: 'Lesson · Tool' })).toBeVisible()
    expect(within(detail).getByText('lesson.tool')).toBeVisible()
    expect(within(detail).getByText('Applies')).toBeVisible()
    expect(within(detail).getByText('All repos')).toBeVisible()
    expect(within(detail).getByText('Confidence 0.95')).toBeVisible()
    await waitFor(() => expect(api.memoryRecordHistory).toHaveBeenCalledWith(STORE, expect.objectContaining({ kind: 'directive', id: 'lesson.tool' }), 25, 0))
    fireEvent.click(within(detail).getByRole('button', { name: 'Close' }))
    expect(screen.queryByTestId('memory-record-detail')).toBeNull()
  })
})
