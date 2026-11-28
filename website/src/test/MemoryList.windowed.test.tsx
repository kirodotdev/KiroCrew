import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { forwardRef, useImperativeHandle } from 'react'
import { renderWithProviders } from './helpers'

/**
 * With a scrolling container around it, the memory list windows its rows
 * (react-virtuoso): the list hands the rows, the divider and the footer to it as
 * one flat list, and each item stays the row's own <li> inside the page's <ul>.
 */
const { api, seen } = vi.hoisted(() => ({
  api: { memoryRecords: vi.fn(), memoryStores: vi.fn(), memoryRecordHistory: vi.fn() },
  seen: { scrollParent: null as HTMLElement | null, count: 0, scrolledTo: [] as number[], endReached: undefined as undefined | (() => void) },
}))
vi.mock('../api/client', () => ({ api }))
// The test DOM has no layout, so Virtuoso itself would render no rows: this
// stand-in renders every item through the list's own List and Item components.
vi.mock('react-virtuoso', () => ({
  Virtuoso: forwardRef(function Virtuoso({ data, itemContent, components, context, customScrollParent, endReached }: {
    endReached?: () => void
    data: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode; context: unknown; customScrollParent: HTMLElement
    components: { List: React.ComponentType<{ children: React.ReactNode; context: unknown }>; Item: React.ComponentType<Record<string, unknown>> }
  }, ref) {
    useImperativeHandle(ref, () => ({ scrollToIndex: ({ index }: { index: number }) => { seen.scrolledTo.push(index) } }))
    seen.scrollParent = customScrollParent
    seen.endReached = endReached
    seen.count = data.length
    const { List, Item } = components
    return <List context={context}>{data.map((item, i) => <Item key={i} data-index={i} data-item-index={i} data-known-size={0} item={item} context={context}>{itemContent(i, item)}</Item>)}</List>
  }),
}))
const { default: MemoryList } = await import('../pages/overview/memoryPage/MemoryList')

const row = (i: number, importance?: number) => ({ kind: 'fact', id: `fact.${i}`, key: `fact.${i}`, value_json: JSON.stringify(`Value ${i}`), text: '', importance, source: 'user_explicit', updated_at: '2026-11-20T10:00:00Z', created_at: '2026-11-01T10:00:00Z', revision: 'a'.repeat(64) })

const ancestorStyle = window.getComputedStyle
beforeEach(() => {
  vi.clearAllMocks()
  seen.scrolledTo = []
  sessionStorage.clear()
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false, current_revision: 1 })
  // The element marked as the page scroller scrolls and has a height.
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el: Element) => {
    const style = ancestorStyle(el)
    return (el as HTMLElement).dataset?.scroller ? { ...style, overflowY: 'auto' } as CSSStyleDeclaration : style
  })
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) { return this.dataset.scroller ? 800 : 0 })
})
afterEach(() => { vi.restoreAllMocks() })

describe('the windowed memory list', () => {
  it('windows the rows inside the page scroller and keeps the divider in its place', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [row(1, 0.9), row(2, 0.8), row(3)], total: 3, has_more: false, facets: {} })
    sessionStorage.setItem('kirocrew:memory-page-view:member-x', JSON.stringify({ q: '', kind: 'all', sort: 'confidence', order: 'desc', filters: {}, mode: 'words' }))
    renderWithProviders(<div data-scroller="1"><MemoryList store="member-x" storeName="x" privateMemory selecting={false} onSelectingChange={() => {}} /></div>)
    const list = await screen.findByRole('list', { name: 'Memories' })
    await within(list).findByText('Value 3')
    expect(seen.scrollParent).toBe(document.querySelector('[data-scroller]'))
    const items = list.querySelectorAll(':scope > li')
    // Three rows plus the divider before the first row without a score.
    expect(seen.count).toBe(4)
    expect(items).toHaveLength(4)
    expect(items[2]).toHaveAttribute('data-index', '2')
    expect(items[2].textContent).toMatch(/No confidence/i)
    expect(within(items[3] as HTMLElement).getByText('Value 3')).toBeInTheDocument()
  })

  it('puts the Strength divider before the first row that does not fade', async () => {
    const decay = { strength: 0.4, start: 0.85, rate: 0.03, rate_tag: null, days_old: 20, days_to_line: 10, state: 'fading' }
    const episode = { ...row(1), kind: 'episode', id: 'ep.1', text: 'Value 1', decay }
    api.memoryRecords.mockResolvedValue({ entries: [episode, row(2)], total: 2, has_more: false, facets: {}, decay: { fade_line: 0.26, importance: 0.5, start: 0.85, rates: [] } })
    sessionStorage.setItem('kirocrew:memory-page-view:member-x', JSON.stringify({ q: '', kind: 'all', sort: 'strength', order: 'desc', filters: {}, mode: 'words' }))
    renderWithProviders(<div data-scroller="1"><MemoryList store="member-x" storeName="x" privateMemory selecting={false} onSelectingChange={() => {}} /></div>)
    const list = await screen.findByRole('list', { name: 'Memories' })
    await within(list).findByText('Value 2')
    const items = list.querySelectorAll(':scope > li')
    expect(items).toHaveLength(3)
    expect(items[1].textContent).toBe("Facts and lessons don't fade · newest first")
    expect(within(items[2] as HTMLElement).getByText('Value 2')).toBeInTheDocument()
  })

  it('lands a graph pick on its row: the list narrows to it and scrolls the row into view', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [row(1), row(2), row(3)], total: 3, has_more: false, facets: {} })
    const jump = { refs: ['fact:fact.1', 'fact:fact.2', 'fact:fact.3'], open: { kind: 'fact' as const, id: 'fact.3' }, seq: 1 }
    renderWithProviders(<div data-scroller="1"><MemoryList jump={jump} store="member-x" storeName="x" privateMemory selecting={false} onSelectingChange={() => {}} /></div>)
    const list = await screen.findByRole('list', { name: 'Memories' })
    await within(list).findByText('Value 3')
    const asked = api.memoryRecords.mock.calls.at(-1)!
    expect(asked[1].filters).toEqual({ ref: jump.refs })
    expect(list.querySelector('[data-memory-focus]')?.textContent).toMatch(/Value 3/)
    expect(seen.scrolledTo).toEqual([2])
  })

  it('loads the next page when the windowed list reaches its end, not before', async () => {
    const page = (from: number, n: number, more: boolean) => ({ entries: Array.from({ length: n }, (_, i) => row(from + i)), total: 65, has_more: more, facets: {} })
    api.memoryRecords.mockImplementation((_store: string, _query: unknown, offset: number) => Promise.resolve(offset ? page(51, 15, false) : page(1, 50, true)))
    renderWithProviders(<div data-scroller="1"><MemoryList store="member-x" storeName="x" privateMemory selecting={false} onSelectingChange={() => {}} /></div>)
    const list = await screen.findByRole('list', { name: 'Memories' })
    await within(list).findByText('Value 50')
    expect(screen.getByText('1–50 of 65')).toBeInTheDocument()
    expect(api.memoryRecords.mock.calls.filter(call => call[2] === 50)).toHaveLength(0)
    seen.endReached!()
    expect(await screen.findByText('1–65 of 65')).toBeInTheDocument()
  })
})
