/**
 * The grouped-memory preview cuts a long row to TEXT_PREVIEW_CHARS (160) with a
 * plain UTF-16 slice. When an emoji straddles that cap, a plain slice ends on its
 * lone high surrogate and the text renders U+FFFD (the replacement glyph). These
 * tests pin that the cut lands on a whole code point in both the mobile card and
 * the table cell, and keeps a whole emoji that ends exactly at the cap.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, within } from '@testing-library/react'
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
const TEXT_PREVIEW_CHARS = 160
const EMOJI = '🎉' // U+1F389, the surrogate pair D83C DF89
const LONE_HIGH_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

/** A string whose emoji straddles `cap`: its high surrogate sits at index cap-1
 *  and its low surrogate at index cap, so a plain slice(0, cap) keeps the lone
 *  high surrogate. `cap - 1` filler units, then the emoji, then a long tail so
 *  the cut is real. */
const straddling = (cap: number) => 'a'.repeat(cap - 1) + EMOJI + 'z'.repeat(50)
/** A string whose emoji ends exactly at `cap`: its low surrogate sits at index
 *  cap-1, so slice(0, cap) keeps the whole emoji. */
const endsAtCap = (cap: number) => 'a'.repeat(cap - 2) + EMOJI + 'z'.repeat(50)

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
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({ slug: 'zzq-theme' })
  api.themeBoot.mockResolvedValue({})
})

/** Seed one count, click it, and return once the entry rows have rendered. */
async function openGroupWith(text: string) {
  api.memoryCarve.mockImplementation((request: { countBy?: string }) => request.countBy
    ? Promise.resolve({ counts: { slack: 1 } })
    : Promise.resolve({ entries: [{ id: 'episode:preview', kind: 'episode', text, crew: 'a-crew', surface: 'a-surface' }] }))
  renderWithProviders(<MemoryCarveCard store={CREW_STORE} />)
  // Explicit ceiling: the count button appears only after the store-list query
  // (useMemoryStores) settles and the counts query it gates then resolves, so
  // this spans the store-list → counts-query chain. Testing Library's 1000ms
  // default can be exceeded under loaded CI workers, flaking a correct render;
  // 5s matches the repo's convention for chained waits.
  fireEvent.click(await screen.findByRole('button', { name: /slack.*1/i }, { timeout: 5_000 }))
  // Same chain, next link: the entry rows render only after the entries query
  // (the non-countBy memoryCarve call) resolves.
  await screen.findByTestId('memory-carve-mobile-results', undefined, { timeout: 5_000 })
}

describe('MemoryCarveCard emoji-safe preview truncation', () => {
  it('cuts a straddling emoji off the mobile card preview without a lone surrogate', async () => {
    await openGroupWith(straddling(TEXT_PREVIEW_CHARS))
    const expected = 'a'.repeat(TEXT_PREVIEW_CHARS - 1)
    const narrow = await screen.findByTestId('memory-carve-mobile-results')
    const preview = within(narrow).getByText(expected)
    expect(preview.textContent).toBe(expected)
    expect(preview.textContent).not.toMatch(LONE_HIGH_SURROGATE)
  })

  it('cuts a straddling emoji off the table cell preview without a lone surrogate', async () => {
    await openGroupWith(straddling(TEXT_PREVIEW_CHARS))
    const expected = 'a'.repeat(TEXT_PREVIEW_CHARS - 1)
    const table = screen.getByRole('table')
    const preview = within(table).getByText(expected)
    expect(preview.textContent).toBe(expected)
    expect(preview.textContent).not.toMatch(LONE_HIGH_SURROGATE)
  })

  it('keeps a whole emoji that ends exactly at the cap in both renderings', async () => {
    await openGroupWith(endsAtCap(TEXT_PREVIEW_CHARS))
    const expected = 'a'.repeat(TEXT_PREVIEW_CHARS - 2) + EMOJI
    const narrow = await screen.findByTestId('memory-carve-mobile-results')
    expect(within(narrow).getByText(expected).textContent).toBe(expected)
    expect(within(screen.getByRole('table')).getByText(expected).textContent).toBe(expected)
  })
})
