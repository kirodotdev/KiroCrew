import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { crossingDate, strengthAt } from '../pages/overview/memoryPage/MemoryStrength'
import { memoryRecordParams } from '../api/client/memory'

/**
 * A V1 store's experiences carry their strength in recall: a chip on the row, a
 * curve and its rate in the detail panel, a Strength filter and sort, and the
 * store's fade rates in the rail. A store that does not fade shows none of it.
 */
const { api } = vi.hoisted(() => ({ api: {
  memoryRecords: vi.fn(),
  memoryStores: vi.fn(),
  memoryRecordHistory: vi.fn(),
} }))
vi.mock('../api/client', () => ({ api }))
const { default: MemoryList } = await import('../pages/overview/memoryPage/MemoryList')
const { NewMemoryDialog } = await import('../pages/overview/memoryPage/MemoryNewDialogs')

const FADE_LINE = 0.385
const STORE_DECAY = { fade_line: FADE_LINE, importance: 0.5, start: 0.85, rates: [{ tag: null, rate: 0.03, days_to_line: 27 }, { tag: 'keep', rate: 0, days_to_line: null }] }
const FADED = {
  kind: 'episode', id: 'ep-old', text: 'Reran shard 8 after a worktree timeout', tags: '[]', importance: 0.5, source: 'consolidation',
  updated_at: '2026-10-01T10:00:00Z', created_at: '2026-10-01T10:00:00Z', revision: 'c'.repeat(64),
  decay: { strength: 0.256, start: 0.85, rate: 0.03, rate_tag: null, days_old: 40, days_to_line: 0, state: 'faded' },
}
const KEPT = {
  ...FADED, id: 'ep-kept', text: 'The release train leaves on Thursdays', tags: '["keep"]', revision: 'd'.repeat(64),
  decay: { strength: 0.85, start: 0.85, rate: 0, rate_tag: 'keep', days_old: 400, days_to_line: null, state: 'strong' },
}
const FACETS = { kind: { episode: 2 }, tag: { keep: 1 }, source: { consolidation: 2 }, strength: { strong: 1, faded: 1 } }

function List(props: Partial<Parameters<typeof MemoryList>[0]>) {
  return <MemoryList store="default" storeName="Global memory" privateMemory={false} selecting={false} onSelectingChange={() => {}} recallOpen={false} onRecallOpenChange={() => {}} {...props} />
}
const lastView = () => api.memoryRecords.mock.calls.filter(call => call[3] === 50).at(-1)!

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  api.memoryRecords.mockResolvedValue({ entries: [FADED, KEPT], total: 2, has_more: false, facets: FACETS, decay: STORE_DECAY })
  api.memoryStores.mockResolvedValue({ stores: [], active: 'default' })
  api.memoryRecordHistory.mockResolvedValue({ entries: [], has_more: false, current_revision: 1 })
})

describe('the fade curve', () => {
  it('draws through the strength the server sends for today', () => {
    expect(strengthAt(0.85, 0.03, 40)).toBeCloseTo(FADED.decay.strength, 4)
  })
})

describe('Save as lesson', () => {
  it('opens on the copied rule as no draft, and a changed category makes one', async () => {
    const dirty = vi.fn()
    renderWithProviders(<NewMemoryDialog initialRule={FADED.text} onClose={() => {}} onSaved={() => {}} onLessonSaved={() => {}} onDirtyChange={dirty} />)
    expect(await screen.findByDisplayValue(FADED.text)).toBeInTheDocument()
    expect(dirty).toHaveBeenLastCalledWith(false)
    fireEvent.click(screen.getByRole('radio', { name: 'Tool' }))
    await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(true))
  })
})

describe('the crossing date', () => {
  it('counts whole days from when the row was written, not from now', () => {
    // Written Nov 1 23:30 UTC, first under the line at age 27: Nov 28 23:30 UTC.
    expect(crossingDate('2026-11-01T23:30:00+00:00', 27, 3)).toBe('2026-11-28T23:30:00.000Z')
    expect(crossingDate('2026-11-01T23:30:00', 27, 3)).toBe('2026-11-28T23:30:00.000Z')
  })
})

describe('the records request', () => {
  it('sends a ticked strength band to the server', () => {
    const params = memoryRecordParams('default', { q: '', kind: 'all', filters: { strength: ['faded', 'fading'] } }, 0, 50)
    expect(params.getAll('strength')).toEqual(['faded', 'fading'])
  })
})

describe('strength on the memory list', () => {
  it('shows each row’s strength, the rail’s bands and the store’s rates', async () => {
    renderWithProviders(<List />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    expect(within(rows).getByText('Strength 0.26')).toBeVisible()
    expect(within(rows).getByText('Strength 0.85')).toBeVisible()
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    const group = within(rail).getByRole('region', { name: 'Strength · experiences' })
    expect(within(group).getAllByRole('checkbox').map(box => box.getAttribute('aria-label'))).toEqual(['Strong', 'Faded'])
    const chart = within(group).getByTestId('store-fade-chart')
    // The day comes from the live rate: 0.03 a day puts importance 0.5 under the line on day 27.
    expect(within(chart).getByText('Default rate: new memories outrank one at importance 0.5 after 27 days')).toBeVisible()
    expect(within(chart).getByText("#keep: doesn't fade")).toBeVisible()
    fireEvent.click(within(group).getByRole('checkbox', { name: 'Faded' }))
    await waitFor(() => expect(lastView()[1]).toEqual({ q: '', kind: 'all', filters: { strength: ['faded'] } }))
  })

  it('opens a faded row on its curve and offers to keep it as a lesson', async () => {
    const save = vi.fn()
    renderWithProviders(<List onSaveAsLesson={save} />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    fireEvent.click(within(rows).getAllByRole('button', { name: 'View details' })[0])
    const detail = await screen.findByTestId('strength-detail')
    expect(within(detail).getByRole('img')).toHaveAccessibleName('Strength 0.26 today, 0.85 when saved.')
    // In the detail slide, which jsdom cannot lay out, so presence and not visibility.
    expect(within(detail).getByText('Fades 3% a day')).toBeInTheDocument()
    expect(within(detail).getByText('New memories rank above it, even on an exact match.')).toBeInTheDocument()
    fireEvent.click(within(detail).getByRole('button', { name: 'Save as lesson' }))
    expect(save).toHaveBeenCalledWith(FADED.text)
  })

  it('says a rate-0 tag keeps a row and offers no lesson for it', async () => {
    renderWithProviders(<List onSaveAsLesson={() => {}} />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    fireEvent.click(within(rows).getAllByRole('button', { name: 'View details' })[1])
    const detail = await screen.findByTestId('strength-detail')
    expect(within(detail).getByText('0.85')).toBeInTheDocument()
    expect(within(detail).getByText("Doesn't fade, set by #keep")).toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Save as lesson' })).toBeNull()
  })

  it('reads the rail sentence’s day from the server, never working it out itself', async () => {
    // The server's day for 0.06 a day is 14; a different number here proves the page does no math of its own.
    api.memoryRecords.mockResolvedValue({ entries: [FADED], total: 1, has_more: false, facets: FACETS, decay: { ...STORE_DECAY, rates: [{ tag: null, rate: 0.06, days_to_line: 15 }] } })
    renderWithProviders(<List />)
    expect(await screen.findByText('Default rate: new memories outrank one at importance 0.5 after 15 days')).toBeInTheDocument()
  })

  it('opens a row whose crossing is past any date JavaScript holds', async () => {
    const slow = { ...FADED, decay: { ...FADED.decay, strength: 0.85, rate: 1e-9, days_to_line: 791_993_016, state: 'strong' } }
    api.memoryRecords.mockResolvedValue({ entries: [slow], total: 1, has_more: false, facets: FACETS, decay: { ...STORE_DECAY, rates: [{ tag: null, rate: 1e-9, days_to_line: 791_993_016 }] } })
    renderWithProviders(<List />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    fireEvent.click(within(rows).getByRole('button', { name: 'View details' }))
    const detail = await screen.findByTestId('strength-detail')
    expect(within(detail).queryByText(/new memories rank above it/)).toBeNull()
    expect(within(screen.getByTestId('store-fade-chart')).getByText("Default rate: doesn't fade")).toBeInTheDocument()
  })

  it('offers the Strength sort only on a store that fades', async () => {
    renderWithProviders(<List />)
    await screen.findByText(FADED.text)
    // The sort list waits for the counts read, which says the store fades.
    await waitFor(() => expect(api.memoryRecords.mock.calls.some(call => call[4]?.facets)).toBe(true))
    fireEvent.pointerDown(screen.getByRole('button', { name: /^Sort: Updated/ }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /^Strength/ }))
    await waitFor(() => expect(lastView()[4]).toEqual({ sort: 'strength', order: 'desc' }))
  })

  it('shows no strength on a store that does not fade', async () => {
    api.memoryRecords.mockResolvedValue({ entries: [{ ...FADED, decay: undefined }], total: 1, has_more: false, facets: { kind: { episode: 1 }, strength: {} } })
    renderWithProviders(<List />)
    const rows = await screen.findByRole('list', { name: 'Memories' })
    expect(within(rows).queryByTestId('strength-chip')).toBeNull()
    expect(screen.queryByTestId('store-fade-chart')).toBeNull()
  })
})
