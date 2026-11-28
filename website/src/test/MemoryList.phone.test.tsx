import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
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
    expect(within(toolbar).getByRole('textbox', { name: 'Search memory' }).parentElement).toHaveClass('basis-full')
  })

  it('keeps Test recall out of the desktop toolbar when filters are hidden', async () => {
    renderWithProviders(<List />)
    fireEvent.click(await screen.findByRole('button', { name: 'Hide filters' }))
    const toolbar = screen.getByTestId('memory-toolbar')
    expect(within(toolbar).getByRole('button', { name: 'Filters' })).toBeVisible()
    expect(within(toolbar).getByRole('button', { name: 'Select' })).toBeVisible()
    expect(within(toolbar).queryByRole('button', { name: 'Test recall' })).toBeNull()
  })

  it('keeps the search box at least 22rem wide, and on its own row when the content area is narrow', async () => {
    renderWithProviders(<List />)
    const toolbar = await screen.findByTestId('memory-toolbar')
    // Keyed on the toolbar's own width (a container query), not the window's.
    expect(toolbar.parentElement).toHaveClass('@container')
    expect(within(toolbar).getByRole('textbox', { name: 'Search memory' }).parentElement).toHaveClass('basis-full', '@[40rem]:min-w-[22rem]', '@[40rem]:basis-0')
    // Button text never wraps onto a second line, the mode switch's included.
    for (const button of within(toolbar).getAllByRole('button')) if (button.textContent?.trim()) expect(button.className).toMatch(/whitespace-nowrap/)
    for (const radio of within(toolbar).getAllByRole('radio')) expect(radio.className).toMatch(/whitespace-nowrap/)
    // A short placeholder, so it fits beside the mode switch, Select and Test recall.
    expect(within(toolbar).getByRole('textbox', { name: 'Search memory' })).toHaveAttribute('placeholder', 'Search memories…')
  })
})

describe('row colours (mock S2)', () => {
  it('marks a saved-lesson landing with the saved tint and bar, and an open row with the 8% accent tint', async () => {
    renderWithProviders(<List focusKey="lesson.tool" />)
    await screen.findByText('Work in a worktree')
    const row = document.querySelector('[data-memory-focus]')!
    expect(row).toHaveClass('first:rounded-t-[15px]', 'last:rounded-b-[15px]', 'shadow-[inset_3px_0_0_var(--ok)]')
    expect(row).toHaveTextContent('Just saved')
    expect(within(row as HTMLElement).getByTestId('memory-just-saved')).toBeInTheDocument()
    expect(row.className).not.toMatch(/ring-accent|bg-accent-subtle/)
  })

  it('tints an open row with 8% of the accent', async () => {
    renderWithProviders(<List />)
    fireEvent.click(await screen.findByRole('button', { name: 'View details' }))
    expect(screen.getAllByText('Work in a worktree')[0].closest('li')).toHaveClass('bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]')
  })

  it('fills the picked kind pill with the text colour on both themes', async () => {
    renderWithProviders(<List />)
    const pill = (await screen.findByRole('radio', { name: /^All/ })).firstElementChild!
    expect(pill).toHaveClass('bg-text', 'text-bg')
    expect(pill.className).not.toContain('data-mode')
  })

  it('colours the kind icon by kind', async () => {
    renderWithProviders(<List />)
    await screen.findByText('Work in a worktree')
    const icons = [...document.querySelectorAll('[data-testid="memory-list"] svg.lucide-inline')].map(icon => icon.getAttribute('class') || '')
    expect(icons.some(c => c.includes('text-accent'))).toBe(true)
  })
})

describe('the filter sheet', () => {
  it('searches the values of every group by label and keeps their counts', async () => {
    phone = true
    renderWithProviders(<List />)
    fireEvent.click(await screen.findByRole('button', { name: 'Filters' }))
    const sheet = await screen.findByRole('dialog', { name: 'Filters' })
    // The sheet's body renders a frame after its shell.
    const find = await within(sheet).findByRole('searchbox', { name: 'Search filters' })
    fireEvent.change(find, { target: { value: 'SRC' } })
    await waitFor(() => expect(within(sheet).getByRole('checkbox', { name: 'src/kiro_crew' })).toBeVisible())
    expect(within(sheet).getByRole('checkbox', { name: 'website/src/pages' })).toBeVisible()
    expect(within(sheet).queryByRole('checkbox', { name: 'Tool' })).toBeNull()
    expect(within(sheet).queryByRole('checkbox', { name: 'All repos' })).toBeNull()
    expect(within(within(sheet).getByRole('checkbox', { name: 'src/kiro_crew' }).closest('[data-facet-row]') as HTMLElement).getByText('1')).toBeVisible()
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
    // The hide button shares the search row, not a row of its own.
    expect(within(rail).getByRole('button', { name: 'Hide filters' }).parentElement).toBe(within(rail).getByRole('searchbox', { name: 'Search filters' }).parentElement!.parentElement)
    // Its icon sits on the rail's right edge, and the click area stays 36px.
    expect(within(rail).getByRole('button', { name: 'Hide filters' })).toHaveClass('-mr-2.5', 'h-9', 'w-9')
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
    // It animates open: visible once its first frame widens it.
    await waitFor(() => expect(screen.getByRole('complementary', { name: 'Filters' })).toBeVisible())
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
    for (const name of ['website', 'On topic', 'Lesson · Tool', 'Saved on purpose']) {
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

describe('the wide filter rail', () => {
  it('stays in view, as tall as the window, with confidence at the top', async () => {
    renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    expect(screen.getByTestId('filter-rail')).toHaveClass('sticky', 'self-start')
    // As tall as the part of its scroll area in view, and scrolling on its own.
    expect(rail).toHaveClass('overflow-y-auto', 'overscroll-contain')
    expect(rail.style.maxHeight).not.toBe('')
    const sections = within(rail).getAllByRole('region')
    expect(sections[0]).toHaveAccessibleName(/Confidence/)
  })

  it('scrolls each group inside 16 rows and lists ticked values first', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [LESSON], total: 1, has_more: false, facets: { ...FACETS, scope: { '': 1, website: 9, docs: 4 } } })
    renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    const scope = await within(rail).findByRole('region', { name: /Scope/ })
    expect(within(scope).getByTestId('facet-group-scroll')).toHaveClass('max-h-[32rem]', 'overflow-y-auto')
    expect(within(scope).getAllByRole('checkbox').map(box => box.getAttribute('aria-label'))).toEqual(['website', 'docs', 'All repos'])
    fireEvent.click(within(scope).getByRole('checkbox', { name: 'docs' }))
    await waitFor(() => expect(within(screen.getByRole('region', { name: /Scope/ })).getAllByRole('checkbox')[0]).toHaveAccessibleName('docs'))
  })

  it('is dragged or arrowed wider within its bounds, and remembers its width', async () => {
    renderWithProviders(<List />)
    const handle = await screen.findByRole('separator', { name: 'Filter width' })
    expect(handle).toHaveAttribute('aria-valuenow', '240')
    fireEvent.keyDown(handle, { key: 'ArrowRight' })
    expect(handle).toHaveAttribute('aria-valuenow', '256')
    expect(handle.parentElement).toHaveStyle({ width: '256px' })
    expect(localStorage.getItem('kirocrew:memory-filters-width')).toBe('256')
    for (let i = 0; i < 20; i++) fireEvent.keyDown(handle, { key: 'ArrowRight' })
    expect(handle).toHaveAttribute('aria-valuenow', '420')
    localStorage.removeItem('kirocrew:memory-filters-width')
  })

  it('gives way to the filter sheet when the content area is too narrow for it', async () => {
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(private callback: ResizeObserverCallback) {}
      observe() { this.callback([{ contentRect: { width: 640 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    try {
      renderWithProviders(<List />)
      await screen.findByText('Work in a worktree')
      expect(screen.queryByRole('complementary', { name: 'Filters' })).toBeNull()
      fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
      expect(await screen.findByRole('dialog', { name: 'Filters' })).toBeVisible()
    } finally {
      globalThis.ResizeObserver = original
    }
  })
})

describe('a chat session source', () => {
  it('reads and filters as Learned from chat, with the full source on hover', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [{ ...LESSON, source: 'consolidation:dashboard:chat-12-1790000012' }], total: 1, has_more: false, facets: { ...FACETS, source: { consolidation: 1 } } })
    renderWithProviders(<List />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    const chip = await within(rows).findByRole('button', { name: 'Learned from chat' })
    expect(within(chip).getByText('Learned from chat')).toHaveAttribute('title', 'consolidation:dashboard:chat-12-1790000012')
    fireEvent.click(chip)
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { source: ['consolidation'] } }))
  })
})

describe('scrolling and animating the wide rail', () => {
  /** Give `el` a scroll height, a box height and a position. */
  const scrollBox = (el: HTMLElement, scrollHeight: number, clientHeight: number, scrollTop = 0) => {
    Object.defineProperty(el, 'scrollHeight', { configurable: true, value: scrollHeight })
    Object.defineProperty(el, 'clientHeight', { configurable: true, value: clientHeight })
    Object.defineProperty(el, 'scrollTop', { configurable: true, writable: true, value: scrollTop })
  }
  const wheel = (target: Element, deltaY: number) => {
    const event = new WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true })
    target.dispatchEvent(event)
    return event
  }

  it('moves the group under the pointer first, then the rail, and never the page', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [LESSON], total: 1, has_more: false, facets: { ...FACETS, scope: Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`pkg/${i}`, 30 - i])) } })
    renderWithProviders(<List />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    const group = within(await within(rail).findByRole('region', { name: /Scope/ })).getByTestId('facet-group-scroll')
    expect(group).toHaveClass('overscroll-contain')
    scrollBox(rail, 2000, 600)
    scrollBox(group, 960, 512)
    const event = wheel(within(group).getAllByRole('checkbox')[0], 100)
    expect(event.defaultPrevented).toBe(true)
    expect(group.scrollTop).toBe(100)
    expect(rail.scrollTop).toBe(0)
    group.scrollTop = 448
    wheel(within(group).getAllByRole('checkbox')[0], 100)
    expect(group.scrollTop).toBe(448)
    expect(rail.scrollTop).toBe(100)
    // Over a heading between groups, the rail moves.
    wheel(within(rail).getAllByRole('region')[0], 50)
    expect(rail.scrollTop).toBe(150)
  })

  it('gives the scroll hint a row of its own under the fade', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [LESSON], total: 1, has_more: false, facets: { ...FACETS, scope: Object.fromEntries(Array.from({ length: 30 }, (_, i) => [`pkg/${i}`, 30 - i])) } })
    const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get() { return this.dataset.testid === 'facet-group-scroll' ? 960 : 0 } })
    try {
      renderWithProviders(<List />)
      const rail = await screen.findByRole('complementary', { name: 'Filters' })
      const scope = await within(rail).findByRole('region', { name: /Scope/ })
      const hint = await within(scope).findByTestId('facet-group-more')
      expect(hint.tagName).toBe('P')
      expect(hint.parentElement!.contains(within(scope).getByTestId('facet-group-scroll').parentElement!)).toBe(true)
      expect(hint.closest('[data-testid="facet-group-scroll"]')).toBeNull()
    } finally {
      if (sh) Object.defineProperty(HTMLElement.prototype, 'scrollHeight', sh)
    }
  })

  it('animates open and shut, and snaps under reduced motion', async () => {
    renderWithProviders(<List />)
    await screen.findByRole('complementary', { name: 'Filters' })
    const shell = screen.getByTestId('filter-rail')
    expect(shell.className).toMatch(/transition-\[width,margin,opacity\]/)
    expect(shell.className).toContain('motion-reduce:transition-none')
    expect(shell.style.transitionDuration).toBe('225ms')
    fireEvent.click(screen.getByRole('button', { name: 'Hide filters' }))
    // Shutting: still drawn while it narrows, but already out of reach.
    expect(screen.getByTestId('filter-rail')).toHaveAttribute('aria-hidden', 'true')
    await waitFor(() => expect(screen.queryByTestId('filter-rail')).toBeNull())
  })

  it('removes the rail at once when the user asks for less motion', async () => {
    const original = window.matchMedia
    window.matchMedia = ((query: string) => ({ matches: query.includes('reduce'), media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, onchange: null, dispatchEvent: () => false })) as typeof window.matchMedia
    try {
      renderWithProviders(<List />)
      await screen.findByRole('complementary', { name: 'Filters' })
      fireEvent.click(screen.getByRole('button', { name: 'Hide filters' }))
      expect(screen.queryByTestId('filter-rail')).toBeNull()
    } finally {
      window.matchMedia = original
    }
  })
})

describe('a counts read that fails', () => {
  it('says so beside the list and reads the counts again on Retry', async () => {
    let fail = true
    api.memoryRecords.mockImplementation(async (_s: string, _q: unknown, _o: number, limit: number) => {
      if (limit === 1 && fail) throw new Error('counts down')
      return limit === 1 ? { entries: [], total: 1, has_more: false, facets: FACETS } : { entries: [LESSON], total: 1, has_more: false }
    })
    renderWithProviders(<List />)
    await screen.findByText('Work in a worktree')
    const retry = await screen.findByRole('button', { name: 'Retry memory access' }, { timeout: 15000 })
    const before = api.memoryRecords.mock.calls.filter(call => call[3] === 1).length
    fail = false
    fireEvent.click(retry)
    await waitFor(() => expect(api.memoryRecords.mock.calls.filter(call => call[3] === 1).length).toBeGreaterThan(before))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry memory access' })).toBeNull())
  }, 30000)
})

describe('a hand-off from the graph', () => {
  it('lists exactly the picked records with one chip, opens a single one, and the chip brings the list back', async () => {
    // A date range picked earlier would hide the picked records, so the hand-off drops it.
    sessionStorage.setItem(`kirocrew:memory-page-view:${STORE}`, JSON.stringify({ q: '', kind: 'all', sort: 'updated', order: 'desc', filters: {}, range: { preset: 'today' } }))
    const view = renderWithProviders(<List jump={{ refs: ['aaaaaaaaaa'], open: { kind: 'directive', id: 'lesson.tool' }, seq: 1 }} />)
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { ref: ['aaaaaaaaaa'] } }))
    expect(await screen.findByTestId('memory-record-detail')).toBeInTheDocument()
    // The opened memory is highlighted, but nothing was saved, so it is not marked as just saved.
    await waitFor(() => expect(document.querySelector('[data-memory-focus]')).not.toBeNull())
    expect(screen.queryByTestId('memory-just-saved')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: 1 selected' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all' }))
    view.unmount()
  })
})

describe('a saved-lesson landing', () => {
  it('searches the key by words over every date, and keeps the saved meaning search and range', async () => {
    const saved = JSON.stringify({ q: 'worktrees', kind: 'all', sort: 'updated', order: 'desc', filters: {}, mode: 'meaning', range: { preset: 'today' } })
    sessionStorage.setItem(`kirocrew:memory-page-view:${STORE}`, saved)
    renderWithProviders(<List focusKey="lesson.tool" />)
    await screen.findByText('Work in a worktree')
    expect(lastView()[1]).toEqual({ q: 'lesson.tool', kind: 'directive' })
    expect(lastView()[4]).not.toHaveProperty('meaning')
    expect(JSON.parse(sessionStorage.getItem(`kirocrew:memory-page-view:${STORE}`)!)).toEqual(JSON.parse(saved))
  })
})

describe('server pages as the list scrolls', () => {
  it('asks for the next page only when the end comes near, and a filter starts again from the first', async () => {
    const page = (offset: number) => Array.from({ length: offset ? 20 : 50 }, (_, i) => ({ ...LESSON, id: `lesson.n${offset + i}`, key: `lesson.n${offset + i}`, text: `Rule ${offset + i}`, value_json: JSON.stringify({ rule: `Rule ${offset + i}`, category: 'tool', applies: 'always' }) }))
    api.memoryRecords.mockImplementation(async (_s: string, _q: unknown, offset: number, limit: number) => limit === 1
      ? { entries: [], total: 70, has_more: true, facets: FACETS }
      : { entries: page(offset), total: 70, has_more: offset === 0 })
    const watched: IntersectionObserverCallback[] = []
    const original = globalThis.IntersectionObserver
    globalThis.IntersectionObserver = class { constructor(callback: IntersectionObserverCallback) { watched.push(callback) } observe() {} disconnect() {} unobserve() {} takeRecords() { return [] } } as unknown as typeof IntersectionObserver
    try {
      renderWithProviders(<List />)
      await screen.findByText('Rule 49', {}, { timeout: 5000 })
      const pageCalls = () => api.memoryRecords.mock.calls.filter(call => call[3] === 50)
      expect(pageCalls().map(call => call[2])).toEqual([0])
      // The end of the list scrolls near: the next page loads under the first.
      act(() => watched.at(-1)!([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
      // The next page's rows mount a few per frame.
      await screen.findByText('Rule 69', {}, { timeout: 5000 })
      expect(screen.getByText('Rule 0')).toBeInTheDocument()
      expect(pageCalls().map(call => call[2])).toEqual([0, 50])
      expect(screen.getByText('1–70 of 70')).toBeInTheDocument()
      // A new filter reads from the start again.
      const rail = await screen.findByRole('complementary', { name: 'Filters' })
      fireEvent.click(await within(rail).findByRole('checkbox', { name: 'Tool' }))
      await waitFor(() => expect(pageCalls().at(-1)![2]).toBe(0))
      expect(pageCalls().at(-1)![1]).toEqual({ q: '', kind: 'all', filters: { category: ['tool'] } })
    } finally { globalThis.IntersectionObserver = original }
  })
})

describe('tags as filters', () => {
  const EPISODE = { kind: 'episode', id: 'ep-9', text: 'Shipped the insider build', tags: '["release"]', importance: 0.6, source: 'consolidation:dashboard:chat-1-1', updated_at: '2026-11-13T10:00:00Z', created_at: '2026-11-13T10:00:00Z', revision: 'c'.repeat(64) }

  it('ticks the rail from a row tag without opening the row, and from the detail panel too', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [EPISODE], total: 1, has_more: false, facets: { ...FACETS, tag: { release: 1 } } })
    renderWithProviders(<List />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    fireEvent.click(await within(rows).findByRole('button', { name: '#release' }))
    expect(screen.queryByTestId('memory-record-detail')).toBeNull()
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { tag: ['release'] } }))
    const rail = screen.getByRole('complementary', { name: 'Filters' })
    await waitFor(() => expect(within(rail).getByRole('checkbox', { name: '#release' })).toBeChecked())
    // Ticking it off in the rail clears the row tag's pressed state.
    fireEvent.click(within(rail).getByRole('checkbox', { name: '#release' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all' }))
    fireEvent.click(within(screen.getByRole('list', { name: 'Memories' })).getByRole('button', { name: 'View details' }))
    const detail = await screen.findByTestId('memory-record-detail')
    expect(within(detail).getByText('Learned from chat')).toHaveAttribute('title', 'consolidation:dashboard:chat-1-1')
    fireEvent.click(within(detail).getByRole('button', { name: '#release' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { tag: ['release'] } }))
  })
})

describe('the search mode switch', () => {
  it('leaves the search box for a row of its own when the toolbar is too narrow for it', async () => {
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(private callback: ResizeObserverCallback) {}
      observe() { this.callback([{ contentRect: { width: 420 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    try {
      renderWithProviders(<List />)
      const toolbar = await screen.findByTestId('memory-toolbar')
      const box = within(toolbar).getByRole('textbox', { name: 'Search memory' })
      const modes = within(toolbar).getByRole('radiogroup', { name: 'Search by' })
      expect(box.parentElement!.contains(modes)).toBe(false)
      expect(box).toHaveClass('pr-10')
    } finally {
      globalThis.ResizeObserver = original
    }
  })

  it('sits inside the search box on a wide toolbar', async () => {
    renderWithProviders(<List />)
    const toolbar = await screen.findByTestId('memory-toolbar')
    const box = within(toolbar).getByRole('textbox', { name: 'Search memory' })
    expect(box.parentElement!.contains(within(toolbar).getByRole('radiogroup', { name: 'Search by' }))).toBe(true)
  })
})

describe('detail layout in a narrow desktop pane', () => {
  it.each([[703, false, true], [704, false, false], [800, true, true], [968, true, false]])('at %ipx with rail open=%s, stacks=%s', async (width, railOpen, stacked) => {
    const original = globalThis.ResizeObserver
    localStorage.setItem('kirocrew:memory-filters-collapsed', railOpen ? '0' : '1')
    localStorage.removeItem('kirocrew:memory-filters-width')
    globalThis.ResizeObserver = class {
      constructor(private callback: ResizeObserverCallback) {}
      observe() { this.callback([{ contentRect: { width } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    try {
      renderWithProviders(<List />)
      fireEvent.click(await screen.findByRole('button', { name: 'View details' }))
      const detail = await screen.findByTestId('memory-record-detail')
      if (stacked) {
        expect(screen.queryByTestId('memory-detail-slide')).toBeNull()
        expect(detail.parentElement).toContainElement(screen.getByRole('list', { name: 'Memories' }))
        fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
        // The sheet fades in: wait for the end of its entrance, not its first frame.
        const sheet = await screen.findByRole('dialog', { name: 'Filters' })
        await waitFor(() => expect(sheet).toBeVisible())
      } else {
        expect(screen.getByTestId('memory-detail-slide')).toContainElement(detail)
      }
    } finally {
      globalThis.ResizeObserver = original
      localStorage.removeItem('kirocrew:memory-filters-collapsed')
    }
  })
})
