/**
 * `shortBody` cuts a long value to 39 UTF-16 units + '…' for a button label.
 * When an emoji straddles that cut, a plain slice(0, 39) ends on its lone high
 * surrogate and the label renders U+FFFD. These tests drive the restore-version
 * button that renders `shortBody` and pin that the cut lands on a whole code
 * point, and that a whole emoji ending exactly at the cut is kept.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import MemoryRecordsEditor from '../pages/overview/MemoryRecordsEditor'
import type { MemoryRecord } from '../types/memoryEditing'

const { api } = vi.hoisted(() => ({ api: { vectorSemanticWrite: vi.fn(), memoryRecordHistory: vi.fn(), memoryRecords: vi.fn(), memoryRecordsRefresh: vi.fn(), memoryQuerySelectionRefresh: vi.fn(), memoryEditPreview: vi.fn(), memoryEditPreviewPage: vi.fn(), memoryEditApply: vi.fn(), memoryRecall: vi.fn(), memoryStores: vi.fn(), themes: vi.fn(), themeDetail: vi.fn(), themeBoot: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const SHORT_BODY_CAP = 39 // the slice length shortBody applies past 40 chars
const EMOJI = '🎉' // U+1F389, the surrogate pair D83C DF89
const LONE_HIGH_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/

/** A value whose emoji straddles the 39-unit cut: its high surrogate sits at
 *  index 38 and its low surrogate at index 39, so slice(0, 39) keeps the lone
 *  high surrogate. A long tail keeps the whole over 40 units so it is cut. */
const straddling = 'a'.repeat(SHORT_BODY_CAP - 1) + EMOJI + 'z'.repeat(20)
/** A value whose emoji ends exactly at the cut: its low surrogate sits at index
 *  38, so slice(0, 39) keeps the whole emoji. */
const endsAtCap = 'a'.repeat(SHORT_BODY_CAP - 2) + EMOJI + 'z'.repeat(20)

const CURRENT = 'Seattle'
const current = (): MemoryRecord => ({ kind: 'fact', id: 'key:user.city', key: 'user.city', value_json: JSON.stringify(CURRENT), text: CURRENT, revision: '1'.padStart(64, '0'), source: 'user_explicit', metadata: { revision: 2 } })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  for (const mock of Object.values(api)) mock.mockReset()
  api.memoryRecords.mockResolvedValue({ entries: [current()], total: 1, has_more: false })
  api.memoryRecordsRefresh.mockResolvedValue({ entries: [current()], missing: [] })
  api.memoryQuerySelectionRefresh.mockResolvedValue({ matched_count: 1 })
  api.memoryEditPreview.mockResolvedValue({ preview_id: 'p', expires_at: '2026-12-31T12:00:00Z', matched_count: 1, changed_count: 1, unchanged_count: 0, entries: [], preview_offset: 0, preview_limit: 25, preview_has_more: false, warnings: [] })
  api.memoryEditApply.mockResolvedValue({ ok: true, changed_count: 1 })
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.themes.mockResolvedValue({ themes: [] }); api.themeDetail.mockResolvedValue({ slug: 'test-theme' }); api.themeBoot.mockResolvedValue({})
})

/** Open the detail dialog's history and return the restore button whose label
 *  carries `shortBody(restoreValue)`. `restoreValue` is the value of an older,
 *  restorable revision (distinct from the current value). */
async function restoreButton(restoreValue: string): Promise<HTMLElement> {
  api.memoryRecordHistory.mockResolvedValue({ current_revision: 2, has_more: false, entries: [
    { id: 2, revision: 2, base_revision: 1, status: 'accepted', operation: 'correct', source: 'user_explicit', before_json: JSON.stringify({ value_json: JSON.stringify('old') }), after_json: JSON.stringify({ value_json: JSON.stringify(CURRENT) }), metadata_json: '{}', created_at: '2026-09-07T12:00:00Z' },
    { id: 1, revision: 1, base_revision: 0, status: 'accepted', operation: 'create', source: 'user_explicit', before_json: null, after_json: JSON.stringify({ value_json: JSON.stringify(restoreValue) }), metadata_json: '{}', created_at: '2026-09-07T11:00:00Z' },
  ] })
  renderWithProviders(<MemoryRecordsEditor store="member-review" privateMemory />)
  fireEvent.click(await screen.findByRole('button', { name: 'View details' }))
  const detail = screen.getByRole('dialog', { name: 'Memory details' })
  const older = (await within(detail).findAllByText('Recorded change'))[1].closest('details')!
  fireEvent.click(within(older).getByText('Recorded change').closest('summary')!)
  return within(older).getByRole('button', { name: /^Restore / })
}

describe('MemoryRecordsEditor emoji-safe shortBody truncation', () => {
  it('cuts a straddling emoji off the restore button label without a lone surrogate', async () => {
    const button = await restoreButton(straddling)
    const expected = `Restore “${'a'.repeat(SHORT_BODY_CAP - 1)}…”`
    expect(button.textContent).toBe(expected)
    expect(button.textContent).not.toMatch(LONE_HIGH_SURROGATE)
  })

  it('keeps a whole emoji that ends exactly at the cut in the restore button label', async () => {
    const button = await restoreButton(endsAtCap)
    const expected = `Restore “${'a'.repeat(SHORT_BODY_CAP - 2) + EMOJI}…”`
    expect(button.textContent).toBe(expected)
    expect(button.textContent).not.toMatch(LONE_HIGH_SURROGATE)
  })
})
