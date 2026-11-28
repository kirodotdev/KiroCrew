import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { forwardRef } from 'react'
import { renderWithProviders } from './helpers'

/**
 * With a scrolling container around it, the memory list windows its rows
 * (react-virtuoso): the list hands the rows, the divider and the footer to it as
 * one flat list, and each item stays the row's own <li> inside the page's <ul>.
 */
const { api, seen } = vi.hoisted(() => ({
  api: { memoryRecords: vi.fn(), memoryStores: vi.fn(), memoryRecordHistory: vi.fn() },
  seen: { scrollParent: null as HTMLElement | null, count: 0 },
}))
vi.mock('../api/client', () => ({ api }))
// The test DOM has no layout, so Virtuoso itself would render no rows: this
// stand-in renders every item through the list's own List and Item components.
vi.mock('react-virtuoso', () => ({
  Virtuoso: forwardRef(function Virtuoso({ data, itemContent, components, context, customScrollParent }: {
    data: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode; context: unknown; customScrollParent: HTMLElement
    components: { List: React.ComponentType<{ children: React.ReactNode; context: unknown }>; Item: React.ComponentType<Record<string, unknown>> }
  }, _ref) {
    seen.scrollParent = customScrollParent
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
})
