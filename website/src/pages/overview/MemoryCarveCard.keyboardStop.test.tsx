/**
 * The entry table of the "Explore memory" card can be wider than the card, and
 * its cells are plain text, so nothing inside it takes focus. While it hides
 * columns its scroller is the named Tab stop the arrow keys scroll; a table
 * that fits stays out of the Tab order, since a stop there would cost a Tab
 * press for nothing.
 *
 * jsdom does no layout, so scroll geometry is stubbed the way
 * `MarkdownRenderer.tableKeyboardStop.test.tsx` stubs it, and the API is mocked
 * as `MemoryCarveCard.truncate.test.tsx` mocks it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import MemoryCarveCard from './MemoryCarveCard'

const { api } = vi.hoisted(() => ({
  api: {
    memoryStores: vi.fn(),
    memoryCarve: vi.fn(),
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
  },
}))
vi.mock('../../api/client', () => ({ api }))

const CREW_STORE = 'finance'

/** `hidden` px of content beyond the right edge of a 320px scroller. */
function stubGeometry(hidden: number) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(0)
}

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/')
  localStorage.clear()
  api.memoryStores.mockResolvedValue({
    stores: [{
      name: CREW_STORE, is_default: false, lineage: 'crew', exists: true,
      semantic_count: 5, episodic_count: 1, lessons_count: 0,
      facets_supported: true, backup_count: 0, newest_backup: null,
    }],
    active: 'default',
  })
  api.memoryCarve.mockImplementation((request: { countBy?: string }) => request.countBy
    ? Promise.resolve({ counts: { slack: 1 } })
    : Promise.resolve({ entries: [{ id: 'episode:wide', kind: 'episode', text: 'remembered text', crew: 'a-crew', surface: 'a-surface' }] }))
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({ slug: 'zzq-theme' })
  api.themeBoot.mockResolvedValue({})
})

afterEach(() => { vi.restoreAllMocks() })

/** Click the one count and return the entry table once its rows render. */
async function openEntryTable() {
  renderWithProviders(<MemoryCarveCard store={CREW_STORE} />)
  // Explicit ceiling: the count button waits on the store-list query and then
  // the counts query it gates, and the rows on the entries query after it.
  fireEvent.click(await screen.findByRole('button', { name: /slack.*1/i }, { timeout: 5_000 }))
  return screen.findByRole('table', undefined, { timeout: 5_000 })
}

describe('MemoryCarveCard entry table keyboard stop', () => {
  it('makes a clipped entry table a Tab stop named after the card', async () => {
    stubGeometry(240)
    const table = await openEntryTable()
    const region = screen.getByRole('region', { name: 'Explore memory' })
    expect(region).toBe(table.parentElement)
    expect(region).toHaveAttribute('tabindex', '0')
  })

  it('leaves an entry table that fits out of the Tab order', async () => {
    stubGeometry(0)
    const scroller = (await openEntryTable()).parentElement!
    expect(scroller).not.toHaveAttribute('tabindex')
    expect(scroller).not.toHaveAttribute('role')
    expect(scroller).not.toHaveAttribute('aria-label')
    expect(screen.queryByRole('region')).toBeNull()
  })
})
