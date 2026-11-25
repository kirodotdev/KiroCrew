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
    // A short placeholder, so it fits beside the mode switch, Select and Test recall.
    expect(within(toolbar).getByRole('textbox', { name: 'Search memory' })).toHaveAttribute('placeholder', 'Search memories…')
    expect(toolbar).toHaveClass('flex-nowrap')
    expect(within(toolbar).getByRole('textbox', { name: 'Search memory' }).closest('label')).not.toHaveClass('basis-full')
  })
})

describe('row colours (mock S2)', () => {
  it('marks a saved-lesson landing with the saved tint and bar, and an open row with the 8% accent tint', async () => {
    renderWithProviders(<List focusKey="lesson.tool" />)
    await screen.findByText('Work in a worktree')
    const row = document.querySelector('[data-memory-focus]')!
    expect(row).toHaveClass('first:rounded-t-[15px]', 'last:rounded-b-[15px]', 'shadow-[inset_3px_0_0_var(--ok)]')
    expect(row).toHaveTextContent('Just saved')
    expect(row.className).not.toMatch(/ring-accent|bg-accent-subtle/)
  })

  it('tints an open row with 8% of the accent', async () => {
    renderWithProviders(<List />)
    fireEvent.click(await screen.findByRole('button', { name: 'View details' }))
    expect(screen.getAllByText('Work in a worktree')[0].closest('li')).toHaveClass('bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]')
  })

  it('fills the picked kind pill with the text colour on dark', async () => {
    renderWithProviders(<List />)
    const pill = (await screen.findByRole('radio', { name: /^All/ })).firstElementChild!
    expect(pill).toHaveClass('[[data-mode=dark]_&]:bg-text', '[[data-mode=dark]_&]:text-bg', 'bg-bg-hover')
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

  it('searches the wide filter rail too', async () => {
    renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    await within(rail).findByRole('checkbox', { name: 'Tool' })
    fireEvent.change(within(rail).getByRole('searchbox', { name: 'Search filters' }), { target: { value: 'src' } })
    expect(within(rail).queryByRole('checkbox', { name: 'Tool' })).toBeNull()
    expect(within(rail).getByRole('checkbox', { name: 'src/kiro_crew' })).toBeVisible()
  })

  it('hides the wide rail, remembers it, and shows it again from the toolbar with the filter count', async () => {
    localStorage.removeItem('kirocrew:memory-filters-collapsed')
    const first = renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    fireEvent.click(await within(rail).findByRole('checkbox', { name: 'Tool' }))
    expect(screen.queryByRole('button', { name: 'Filters' })).toBeNull()
    fireEvent.click(within(rail).getByRole('button', { name: 'Hide filters' }))
    expect(screen.queryByRole('complementary', { name: 'Filters' })).toBeNull()
    const show = screen.getByRole('button', { name: 'Filters' })
    expect(show).toHaveTextContent('1')
    expect(localStorage.getItem('kirocrew:memory-filters-collapsed')).toBe('1')
    first.unmount()
    renderWithProviders(<List />)
    await screen.findByText('Work in a worktree')
    expect(screen.queryByRole('complementary', { name: 'Filters' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
    expect(await screen.findByRole('complementary', { name: 'Filters' })).toBeVisible()
    expect(localStorage.getItem('kirocrew:memory-filters-collapsed')).toBe('0')
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

describe('row chips as filters', () => {
  const EPISODE = { kind: 'episode', id: 'ep-1', text: 'Cleaned up stale worktrees', tags: '["release","worktree"]', importance: 0.6, source: 'consolidation', updated_at: '2026-11-13T10:00:00Z', created_at: '2026-11-13T10:00:00Z', revision: 'b'.repeat(64) }
  const SCOPED = { ...LESSON, id: 'lesson.web', key: 'lesson.web', value_json: JSON.stringify({ rule: 'Check the phone layout', category: 'tool', applies: 'on_topic', repo_scope: 'website' }) }
  beforeEach(() => { api.memoryRecords.mockResolvedValue({ entries: [SCOPED, EPISODE], total: 2, has_more: false, facets: FACETS }) })

  it('ticks a tag, scope, applies, category or source from its chip, and a second press clears it', async () => {
    renderWithProviders(<List />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    const tag = await within(rows).findByRole('button', { name: '#worktree' })
    expect(tag).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(tag)
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { tag: ['worktree'] } }))
    const list = () => screen.getByRole('list', { name: 'Memories' })
    await waitFor(() => expect(within(list()).getByRole('button', { name: '#worktree' })).toHaveAttribute('aria-pressed', 'true'))
    fireEvent.click(within(list()).getByRole('button', { name: '#worktree' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all' }))
    for (const name of ['website', 'On topic', 'Lesson · Tool', 'Saved by you or the agent']) {
      fireEvent.click((await within(await screen.findByRole('list', { name: 'Memories' })).findAllByRole('button', { name }))[0])
    }
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { scope: ['website'], applies: ['on_topic'], category: ['tool'], source: ['user_explicit'] } }))
    // The press filters; it does not open the row.
    expect(screen.queryByTestId('memory-record-detail')).toBeNull()
  })

  it('leaves chips as plain labels while selecting rows', async () => {
    renderWithProviders(<List selecting />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    await within(rows).findByText('#worktree')
    expect(within(rows).queryByRole('button', { name: '#worktree' })).toBeNull()
  })
})
