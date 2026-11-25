import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * The memory list on a phone and in its filters: the search box gets its own
 * row, the filter sheet can be searched, a bot's channel and crew are filters,
 * and the saved-lesson focus ring follows the list's rounded corners.
 */
const { api } = vi.hoisted(() => ({ api: {
  memoryRecords: vi.fn(),
  memoryStores: vi.fn(),
  memoryRecall: vi.fn(),
  memoryRecordHistory: vi.fn(),
} }))
vi.mock('../api/client', () => ({ api }))
let phone = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => phone }))
const { default: MemoryList } = await import('../pages/overview/memoryPage/MemoryList')

const STORE = 'member-reviewer-abc'
const LESSON = { kind: 'directive', id: 'lesson.tool', key: 'lesson.tool', value_json: JSON.stringify({ rule: 'Work in a worktree', category: 'tool', applies: 'always' }), text: '', importance: 0.95, source: 'user_explicit', updated_at: '2026-11-20T10:00:00Z', created_at: '2026-11-01T10:00:00Z', revision: 'a'.repeat(64) }
const FACETS = {
  kind: { fact: 4, directive: 3, episode: 2 },
  category: { tool: 2, knowledge: 1 }, applies: { always: 3 },
  scope: { '': 1, 'src/kiro_crew': 1, 'website/src/pages': 1 },
  source: { user_explicit: 9 }, tag: {},
  surface: { slack: 6, '': 3 }, crew: { reviewer: 5 },
}

function List(props: Partial<Parameters<typeof MemoryList>[0]>) {
  return <MemoryList store={STORE} storeName="reviewer" privateMemory selecting={false} onSelectingChange={() => {}} recallOpen={false} onRecallOpenChange={() => {}} {...props} />
}
const lastView = () => api.memoryRecords.mock.calls.at(-1)!

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  phone = false
  api.memoryRecords.mockResolvedValue({ entries: [LESSON], total: 1, has_more: false, facets: FACETS })
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false, current_revision: 1 })
})

describe('the memory toolbar', () => {
  it('wraps on a phone, so the search box keeps a row of its own at any phone width', async () => {
    phone = true
    renderWithProviders(<List />)
    const toolbar = await screen.findByTestId('memory-toolbar')
    expect(toolbar).toHaveClass('flex-wrap')
    // A width class would stop wrapping from 640 px, inside the phone layout.
    expect(toolbar.className).not.toMatch(/sm:flex-nowrap/)
    expect(within(toolbar).getByRole('textbox', { name: 'Search memory' }).closest('label')).toHaveClass('basis-full')
  })

  it('stays on one row on a wide screen', async () => {
    renderWithProviders(<List />)
    const toolbar = await screen.findByTestId('memory-toolbar')
    expect(toolbar).toHaveClass('flex-nowrap')
    expect(within(toolbar).getByRole('textbox', { name: 'Search memory' }).closest('label')).not.toHaveClass('basis-full')
  })
})

describe('the saved-lesson focus ring', () => {
  it('rounds the first and last rows to the list corners, so the ring is not clipped', async () => {
    renderWithProviders(<List focusKey="lesson.tool" />)
    await screen.findByText('Work in a worktree')
    const row = document.querySelector('[data-memory-focus]')!
    expect(row).toHaveClass('ring-2', 'ring-inset', 'first:rounded-t-[15px]', 'last:rounded-b-[15px]')
  })
})

describe('the filter sheet', () => {
  it('searches the values of every group by label and keeps their counts', async () => {
    phone = true
    renderWithProviders(<List />)
    fireEvent.click(await screen.findByRole('button', { name: 'Filters' }))
    const sheet = await screen.findByRole('dialog', { name: 'Filters' })
    const find = within(sheet).getByRole('searchbox', { name: 'Search filters' })
    fireEvent.change(find, { target: { value: 'SRC' } })
    expect(within(sheet).getByRole('checkbox', { name: 'src/kiro_crew' })).toBeVisible()
    expect(within(sheet).getByRole('checkbox', { name: 'website/src/pages' })).toBeVisible()
    expect(within(sheet).queryByRole('checkbox', { name: 'Tool' })).toBeNull()
    expect(within(sheet).queryByRole('checkbox', { name: 'All repos' })).toBeNull()
    expect(within(within(sheet).getByRole('checkbox', { name: 'src/kiro_crew' }).closest('label')!).getByText('1')).toBeVisible()
    fireEvent.change(find, { target: { value: 'zzz' } })
    expect(within(sheet).getByRole('status')).toHaveTextContent('No memories match your search')
    fireEvent.change(find, { target: { value: '' } })
    expect(within(sheet).getByRole('checkbox', { name: 'Tool' })).toBeVisible()
  })

  it('leaves the wide filter rail without a search box', async () => {
    renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    await within(rail).findByRole('checkbox', { name: 'Tool' })
    expect(within(rail).queryByRole('searchbox')).toBeNull()
  })

  it('filters a bot\'s memories by the channel and the crew that wrote them', async () => {
    renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    const channel = await within(rail).findByRole('region', { name: 'Channel' })
    expect(within(channel).getByRole('checkbox', { name: 'slack' })).toBeVisible()
    expect(within(channel).getByRole('checkbox', { name: 'not recorded' })).toBeVisible()
    expect(within(rail).getByRole('region', { name: 'Member' })).toBeVisible()
    fireEvent.click(within(channel).getByRole('checkbox', { name: 'slack' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { surface: ['slack'] } }))
    fireEvent.click(within(rail).getByRole('checkbox', { name: 'reviewer' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { surface: ['slack'], crew: ['reviewer'] } }))
  })
})
