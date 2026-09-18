import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, within, act, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VectorMemoryCard, { parseTags, semanticValueText } from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { i18next, registerCatalogs } from '../i18n'
import zhCN from '../i18n/locales/zh-CN.json'

registerCatalogs({ 'zh-CN': { translation: zhCN } })

// Coverage-focused companion to the existing VectorMemoryCard specs. Those cover
// the pure helpers and the semantic render cap; this one drives the three tabs
// that never render until the user clicks them (Episodic / Audit / Inspector),
// the write / edit / delete round-trips, the embedding-setup progress block, and
// the status poll that terminates it.

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(),
    vectorEmbeddingStatus: vi.fn(),
    vectorSemantic: vi.fn(),
    vectorSemanticWrite: vi.fn(),
    vectorSemanticDelete: vi.fn(),
    vectorEpisodic: vi.fn(),
    vectorEpisodicSearch: vi.fn(),
    vectorEpisodicDelete: vi.fn(),
    vectorEvents: vi.fn(),
    vectorContextPreview: vi.fn(),
    vectorEnableEmbeddings: vi.fn(),
  },
}))

type Loose = Record<string, unknown>

const ACTIVE_STATS = { semantic_active: 2, episodic_active: 3, embedded_count: 1, migrated: true }
const ACTIVE_EMB = { provider: 'llama_cpp', setup_step: 'done', model_available: true }

/**
 * Set every api mock to a resolved default so a leftover implementation from a
 * previous test can never leak in (vi.clearAllMocks clears calls, not impls).
 */
function setupApi(over: Loose = {}) {
  vi.mocked(api.vectorStats).mockResolvedValue((over.stats ?? ACTIVE_STATS) as never)
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue((over.emb ?? ACTIVE_EMB) as never)
  vi.mocked(api.vectorSemantic).mockResolvedValue((over.semantic ?? { entries: [] }) as never)
  vi.mocked(api.vectorSemanticWrite).mockResolvedValue(undefined as never)
  vi.mocked(api.vectorSemanticDelete).mockResolvedValue(undefined as never)
  vi.mocked(api.vectorEpisodic).mockResolvedValue((over.episodic ?? { entries: [] }) as never)
  vi.mocked(api.vectorEpisodicSearch).mockResolvedValue((over.search ?? { results: [] }) as never)
  vi.mocked(api.vectorEpisodicDelete).mockResolvedValue(undefined as never)
  vi.mocked(api.vectorEvents).mockResolvedValue((over.events ?? { events: [] }) as never)
  vi.mocked(api.vectorContextPreview).mockResolvedValue((over.preview ?? null) as never)
  vi.mocked(api.vectorEnableEmbeddings).mockResolvedValue({ ok: true } as never)
}

/** Wait for the active card (its tab strip) to be on screen. */
async function waitForActive() {
  await waitFor(() => expect(screen.getByRole('button', { name: /Inspector/i })).toBeInTheDocument())
}

const tab = (name: RegExp) => screen.getByRole('button', { name })

/** Matches the `<p>` footer lines ("Showing N of M", "Showing N events"). */
const footer = (re: RegExp) => (_: string, el: Element | null) =>
  el?.tagName === 'P' && re.test(el.textContent || '')

const EPISODIC_ROWS = [
  { id: 'e1', text: '[2026-08-10 09:00] shipped the coverage wave', tags: ['ci', 'tests'], importance: 0.98 },
  { id: 'e2', text: 'plain fragment with no timestamp', tags: '["release"]', importance: 0.85, created_at: 'not-a-date' },
  { id: 'e3', text: 'third fragment', tags: 'legacy-default', importance: 0.5, created_at: '2026-08-01 12:00:00' },
  { id: 'e4', text: 'fourth fragment', tags: '"quoted"', importance: 0.99 },
  { id: 'e5', text: 'fifth fragment', tags: '42', importance: 0.99 },
  { id: 'e6', text: 'sixth fragment', tags: null, importance: 0.99 },
]

/** A rejection payload JSON.stringify cannot render, so the error extractor must fall back. */
const CIRCULAR_REJECTION: Loose = { note: 'cycle' }
CIRCULAR_REJECTION.self = CIRCULAR_REJECTION

const AUDIT_ROWS = [
  { event_type: 'memory_write', memory_key: 'pref.style.tone', new_value: 'terse', created_at: '2026-08-10 09:00:00' },
  { event_type: 'injection_block', memory_type: 'episodic', old_value: 'blocked text' },
  { event_type: 'consolidation_skip', memory_type: 'semantic' },
  { event_type: 'candidate_reject', memory_key: 'pref.bad', new_value: 'nope', created_at: '2026-08-10T10:00:00Z' },
]

describe('VectorMemoryCard — exported helpers', () => {
  it('parseTags normalises every shape the store can hand back', () => {
    expect(parseTags(['a', 'b'])).toEqual(['a', 'b'])
    expect(parseTags('["a"]')).toEqual(['a'])
    expect(parseTags('"solo"')).toEqual(['solo'])
    expect(parseTags('not json')).toEqual(['not json'])
    expect(parseTags('42')).toEqual(['42'])
    expect(parseTags(null)).toEqual([])
    expect(parseTags({ nope: 1 })).toEqual([])
  })

  it('semanticValueText pretty-prints objects and passes scalars through', () => {
    expect(semanticValueText({ value_json: '{"a":1}' })).toBe('{\n  "a": 1\n}')
    expect(semanticValueText({ value_json: 'raw string' })).toBe('raw string')
    expect(semanticValueText({ value_json: 7 })).toBe('7')
    expect(semanticValueText({})).toBe('')
  })
})

describe('VectorMemoryCard — load failures and parent callbacks', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })

  it.each(['vectorStats', 'vectorEmbeddingStatus', 'vectorSemantic'] as const)('reports a failed %s read and recovers on retry', async endpoint => {
    const user = userEvent.setup()
    vi.mocked(api[endpoint]).mockRejectedValue(new Error('memory read unavailable'))
    renderWithProviders(<VectorMemoryCard />)
    expect(await screen.findByRole('alert')).toHaveTextContent('memory read unavailable')
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
    if (endpoint === 'vectorSemantic') expect(screen.queryByText('No semantic entries')).not.toBeInTheDocument()
    expect(screen.getByText('Vector Memory')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    setupApi()
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    await waitForActive()
  })

  it('retains successful semantic rows and unsaved input when a refresh fails', async () => {
    const user = userEvent.setup()
    setupApi({ semantic: { entries: [{ key: 'user.name', value_json: '"Saved name"', confidence: 1 }] } })
    const { queryClient } = renderWithProviders(<VectorMemoryCard />)
    await screen.findByText('Saved name')
    await user.type(screen.getByPlaceholderText('Key (e.g. pref.backend.framework)'), 'project.pending')
    await user.type(screen.getByPlaceholderText('Value'), 'Keep this draft')
    vi.mocked(api.vectorSemantic).mockRejectedValue(new Error('refresh unavailable'))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['member-memory', 'default', 'semantic-browser'] }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('refresh unavailable')
    expect(screen.getByText('Saved name')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Value')).toHaveValue('Keep this draft')
    expect(screen.queryByText('No semantic entries')).not.toBeInTheDocument()
    vi.mocked(api.vectorSemantic).mockResolvedValue({ entries: [] } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await screen.findByText('No semantic entries')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByPlaceholderText('Value')).toHaveValue('Keep this draft')
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
  })

  it('reports migrated and active state to the parent', async () => {
    const onActiveChange = vi.fn()
    const onMigratedChange = vi.fn()
    renderWithProviders(<VectorMemoryCard onActiveChange={onActiveChange} onMigratedChange={onMigratedChange} />)

    await waitForActive()
    expect(onMigratedChange).toHaveBeenCalledWith(true)
    await waitFor(() => expect(onActiveChange).toHaveBeenLastCalledWith(true))
  })
})

describe('VectorMemoryCard labels in the active language', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('translates statistics and search columns without changing the submitted results', async () => {
    const user = userEvent.setup()
    setupApi({ search: { results: [{ id: 'translated-hit', text: 'retained search result', tags: [], importance: 0.9, score: 0.95 }] } })
    const { rerender } = renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    const statistics = screen.getByText('Embedded').parentElement!.parentElement!
    await user.click(tab(/^Episodic$/))
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'coverage{Enter}')
    await screen.findByText('retained search result')

    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(<VectorMemoryCard />)

    for (const label of ['语义记忆', '情景记忆', '已嵌入']) {
      expect(within(statistics).getByText(label)).toBeInTheDocument()
    }
    expect(screen.getAllByRole('columnheader').map(header => header.textContent)).toEqual([
      '内容', '标签', '重要性', '得分', '时间', '',
    ])
    expect(screen.getByText('retained search result')).toBeInTheDocument()
    expect(api.vectorEpisodicSearch).toHaveBeenCalledTimes(1)
    expect(api.vectorEpisodicSearch).toHaveBeenLastCalledWith('coverage', undefined)
  })
})

describe('VectorMemoryCard — Episodic tab', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })

  it('renders an empty episodic table on first open', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText('No episodic entries')).toBeInTheDocument())
    expect(api.vectorEpisodic).toHaveBeenCalledWith(50, 0, undefined)
    expect(api.vectorEpisodic).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Episodic Memory')).toBeInTheDocument()
    // No search query yet, so no Score column.
    expect(screen.queryByText('Score')).not.toBeInTheDocument()
  })

  it('renders rows, tag chips, importance badges and the When column', async () => {
    const user = userEvent.setup()
    setupApi({ episodic: { entries: EPISODIC_ROWS } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText(/shipped the coverage wave/)).toBeInTheDocument())

    // Tag chips come from every parseTags shape.
    for (const t of ['ci', 'tests', 'release', 'legacy-default', 'quoted', '42']) {
      expect(screen.getAllByText(t).length).toBeGreaterThan(0)
    }
    // Importance badge tiers: ok / warn / err.
    expect(screen.getByText(/●\s*0\.98/)).toBeInTheDocument()
    expect(screen.getByText(/●\s*0\.85/)).toBeInTheDocument()
    expect(screen.getByText(/●\s*0\.50/)).toBeInTheDocument()

    // A bracketed date prefix wins over created_at; an unparseable date is an em dash.
    expect(screen.getByText('2026-08-10')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
    expect(screen.getByText(footer(/Showing 6 entries/))).toBeInTheDocument()
  })

  it('searches, shows the Score column, then clears back to the browse list', async () => {
    const user = userEvent.setup()
    setupApi({
      episodic: { entries: EPISODIC_ROWS },
      search: { results: [
        { id: 's1', text: 'scored hit', tags: [], importance: 0.9, score: 0.91234 },
        { id: 's2', text: 'unscored hit', tags: [], importance: 0.9 },
      ] },
    })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText(/shipped the coverage wave/)).toBeInTheDocument())

    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'coverage')
    expect(screen.queryByText('Score')).not.toBeInTheDocument()
    expect(api.vectorEpisodicSearch).not.toHaveBeenCalled()
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), '{Enter}')
    await waitFor(() => expect(screen.getByText('scored hit')).toBeInTheDocument())
    expect(api.vectorEpisodicSearch).toHaveBeenCalledWith('coverage', undefined)
    expect(screen.getByText('Score')).toBeInTheDocument()
    expect(screen.getByText('0.912')).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), ' draft')
    expect(screen.getByText('Score')).toBeInTheDocument()
    expect(api.vectorEpisodicSearch).toHaveBeenLastCalledWith('coverage', undefined)

    // Clear resets the query and re-browses.
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.getByText(/shipped the coverage wave/)).toBeInTheDocument())
    expect(screen.queryByText('Score')).not.toBeInTheDocument()
  })

  it('runs a search from the Search button as well as the Enter key', async () => {
    const user = userEvent.setup()
    setupApi({ search: { results: [{ id: 's1', text: 'button hit', tags: [], importance: 0.9 }] } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText('No episodic entries')).toBeInTheDocument())

    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'button')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(screen.getByText('button hit')).toBeInTheDocument())
    // No score and no timestamp on the hit, so both cells render an em dash.
    expect(screen.getAllByText('—')).toHaveLength(2)
  })

  it('filters by tag, offers its own Clear, and toggles the same tag off', async () => {
    const user = userEvent.setup()
    setupApi({ episodic: { entries: EPISODIC_ROWS } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText('Filter by tag:')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'ci' }))
    await waitFor(() => expect(api.vectorEpisodic).toHaveBeenLastCalledWith(50, 0, 'ci'))
    // With a tag but no query, a dedicated Clear appears.
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument()

    // Clicking the same tag again toggles it back off.
    await user.click(screen.getByRole('button', { name: 'ci' }))
    await waitFor(() => expect(api.vectorEpisodic).toHaveBeenLastCalledWith(50, 0, undefined))

    await user.click(screen.getByRole('button', { name: 'ci' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument())
  })

  it('retains the first page after a failed next page and retries the same offset', async () => {
    const user = userEvent.setup()
    const page1 = Array.from({ length: 50 }, (_, i) => ({ id: `p1-${i}`, text: `first ${i}`, tags: [], importance: 0.99 }))
    setupApi({ episodic: { entries: page1 } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText('first 0')).toBeInTheDocument())

    vi.mocked(api.vectorEpisodic).mockRejectedValue(new Error('next page down'))
    await user.click(screen.getByRole('button', { name: 'Load more…' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('next page down')
    expect(screen.getByText('first 0')).toBeInTheDocument()
    expect(screen.queryByText('No episodic entries')).not.toBeInTheDocument()
    expect(api.vectorEpisodic).toHaveBeenLastCalledWith(50, 50, undefined)
    vi.mocked(api.vectorEpisodic).mockResolvedValue({ entries: [{ id: 'p2-0', text: 'second page row', tags: [], importance: 0.99 }] } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await waitFor(() => expect(screen.getByText('second page row')).toBeInTheDocument())
    expect(api.vectorEpisodic).toHaveBeenLastCalledWith(50, 50, undefined)
    // Still holds the first page, and the exhausted page hides Load more.
    expect(screen.getByText('first 0')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more…' })).not.toBeInTheDocument()
  })

  it('reports browse and search failures, preserves rows and retries the submitted search', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorEpisodic).mockRejectedValue(new Error('episodic down'))
    vi.mocked(api.vectorEpisodicSearch).mockRejectedValue(new Error('search down'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Episodic$/))
    expect(await screen.findByRole('alert')).toHaveTextContent('episodic down')
    expect(screen.queryByText('No episodic entries')).not.toBeInTheDocument()
    vi.mocked(api.vectorEpisodic).mockResolvedValue({ entries: EPISODIC_ROWS } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await screen.findByText('third fragment')
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'anything{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('search down')
    expect(screen.getByText('third fragment')).toBeInTheDocument()
    expect(screen.queryByText('No episodic entries')).not.toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), ' edited')
    vi.mocked(api.vectorEpisodicSearch).mockResolvedValue({ results: [] } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await screen.findByText('No episodic entries')
    expect(api.vectorEpisodicSearch).toHaveBeenLastCalledWith('anything', undefined)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('retains the row and search after a rejected deletion and retries that row', async () => {
    const user = userEvent.setup()
    setupApi({ episodic: { entries: EPISODIC_ROWS.slice(0, 2) }, search: { results: EPISODIC_ROWS.slice(0, 2) } })
    vi.mocked(api.vectorEpisodicDelete).mockRejectedValueOnce(new Error('delete refused'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.type(screen.getByPlaceholderText('Key (e.g. pref.backend.framework)'), 'pref.unsaved')
    await user.click(tab(/^Episodic$/))
    await screen.findByText(/shipped the coverage wave/)
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'coverage{Enter}')
    await screen.findByText('Score')
    const row = screen.getByText(/shipped the coverage wave/).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Delete' }))
    expect(await within(row).findByRole('alert')).toHaveTextContent('delete refused')
    expect(screen.getByPlaceholderText('Search episodic memories…')).toHaveValue('coverage')
    expect(screen.getByText('plain fragment with no timestamp')).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText(/shipped the coverage wave/)).not.toBeInTheDocument())
    expect(api.vectorEpisodicDelete).toHaveBeenNthCalledWith(2, 'e1')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await user.click(tab(/^Semantic$/))
    expect(screen.getByPlaceholderText('Key (e.g. pref.backend.framework)')).toHaveValue('pref.unsaved')
  })

  it('keeps a deleted row out of cached filters when a later refresh fails', async () => {
    const user = userEvent.setup()
    setupApi({ episodic: { entries: EPISODIC_ROWS.slice(0, 2) }, search: { results: EPISODIC_ROWS.slice(0, 2) } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText(/shipped the coverage wave/)).toBeInTheDocument())

    // Populate both browse and search caches before deleting from the search.
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'coverage{Enter}')
    await waitFor(() => expect(screen.getByText('Score')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())

    const row = screen.getByText(/shipped the coverage wave/).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByText(/shipped the coverage wave/)).not.toBeInTheDocument())
    expect(api.vectorEpisodicDelete).toHaveBeenCalledWith('e1')
    expect(screen.getByText('plain fragment with no timestamp')).toBeInTheDocument()

    vi.mocked(api.vectorEpisodic).mockRejectedValue(new Error('browse refresh unavailable'))
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('browse refresh unavailable')
    expect(screen.queryByText(/shipped the coverage wave/)).not.toBeInTheDocument()
    expect(screen.getByText('plain fragment with no timestamp')).toBeInTheDocument()

    vi.mocked(api.vectorEpisodicSearch).mockRejectedValue(new Error('search refresh unavailable'))
    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'coverage{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('search refresh unavailable')
    expect(screen.queryByText(/shipped the coverage wave/)).not.toBeInTheDocument()
    expect(screen.getByText('plain fragment with no timestamp')).toBeInTheDocument()
  })
})

describe('VectorMemoryCard — Audit tab', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })

  it('shows the empty audit state', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Audit$/))
    await waitFor(() => expect(screen.getByText('No events')).toBeInTheDocument())
    expect(api.vectorEvents).toHaveBeenCalledWith(50, 0)
    expect(screen.getByText('Audit Trail')).toBeInTheDocument()
  })

  it('renders every event severity, key column and timestamp fallback', async () => {
    const user = userEvent.setup()
    setupApi({ events: { events: AUDIT_ROWS } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Audit$/))
    await waitFor(() => expect(screen.getAllByText('memory_write').length).toBeGreaterThan(0))

    expect(screen.getAllByText('injection_block').length).toBeGreaterThan(0)
    expect(screen.getAllByText('consolidation_skip').length).toBeGreaterThan(0)
    expect(screen.getAllByText('candidate_reject').length).toBeGreaterThan(0)
    // memory_type === 'episodic' collapses to a literal 'episodic' key cell.
    expect(screen.getByText('episodic')).toBeInTheDocument()
    expect(screen.getByText('semantic')).toBeInTheDocument()
    expect(screen.getByText('terse')).toBeInTheDocument()
    expect(screen.getByText('blocked text')).toBeInTheDocument()
    // Rows without created_at render an em dash.
    expect(screen.getAllByText('—').length).toBe(2)
    expect(screen.getByText(footer(/Showing 4 events$/))).toBeInTheDocument()
  })

  it('filters by event type and restores the All view', async () => {
    const user = userEvent.setup()
    setupApi({ events: { events: AUDIT_ROWS } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Audit$/))
    await waitFor(() => expect(screen.getByText(footer(/Showing 4 events$/))).toBeInTheDocument())

    // One filter button per distinct event_type, plus All.
    await user.click(screen.getByRole('button', { name: 'injection_block' }))
    await waitFor(() => expect(screen.getByText(footer(/Showing 1 events \(4 total\)/))).toBeInTheDocument())
    expect(screen.queryByText('terse')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'All' }))
    await waitFor(() => expect(screen.getByText(footer(/Showing 4 events$/))).toBeInTheDocument())
  })

  it('shows a no-match row when the active filter matches nothing', async () => {
    const user = userEvent.setup()
    setupApi({ events: { events: AUDIT_ROWS } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Audit$/))
    await waitFor(() => expect(screen.getByText(footer(/Showing 4 events$/))).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'consolidation_skip' }))
    await waitFor(() => expect(screen.getByText(footer(/Showing 1 events/))).toBeInTheDocument())

    vi.mocked(api.vectorEvents).mockResolvedValue({ events: [AUDIT_ROWS[0]] } as never)
    await user.click(tab(/^Semantic$/))
    await user.click(tab(/^Audit$/))
    await waitFor(() => expect(screen.getByText('No events')).toBeInTheDocument())
  })

  it.each([false, true])('shows and recovers an audit failure with diagnosticsOnly=%s', async diagnosticsOnly => {
    const user = userEvent.setup()
    vi.mocked(api.vectorEvents).mockRejectedValue(new Error('events down'))
    renderWithProviders(<VectorMemoryCard diagnosticsOnly={diagnosticsOnly} />, {
      queryDefaults: { retryDelay: 0 },
    })
    await waitForActive()

    if (!diagnosticsOnly) await user.click(tab(/^Audit$/))
    expect(await screen.findByRole('alert')).toHaveTextContent('events down')
    expect(screen.queryByText('No events')).not.toBeInTheDocument()
    expect(api.vectorEvents).toHaveBeenCalledWith(50, 0)
    vi.mocked(api.vectorEvents).mockResolvedValue({ events: [] } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByText('No events')).toBeInTheDocument()
  })

  it('shows the audit failure when the stats request also fails', async () => {
    vi.mocked(api.vectorStats).mockRejectedValue(new Error('stats down'))
    vi.mocked(api.vectorEvents).mockRejectedValue(new Error('events down'))
    renderWithProviders(<VectorMemoryCard diagnosticsOnly />, {
      queryDefaults: { retryDelay: 0 },
    })

    await waitFor(() => {
      const messages = screen.getAllByRole('alert').map(notice => notice.textContent)
      expect(messages).toEqual(expect.arrayContaining([
        expect.stringContaining('stats down'), expect.stringContaining('events down'),
      ]))
    })
    expect(api.vectorStats).toHaveBeenCalled()
    expect(api.vectorEvents).toHaveBeenCalledWith(50, 0)
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument()
  })

  it('retains the loaded audit page and retries the failed next-page offset', async () => {
    const user = userEvent.setup()
    const page1 = Array.from({ length: 50 }, (_, i) => ({ event_type: 'memory_write', memory_key: `k${i}` }))
    setupApi({ events: { events: page1 } })
    renderWithProviders(<VectorMemoryCard diagnosticsOnly />, { queryDefaults: { retryDelay: 0 } })
    await screen.findByText('k49')
    vi.mocked(api.vectorEvents).mockRejectedValue(new Error('next audit page unavailable'))
    await user.click(screen.getByRole('button', { name: 'Load more…' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('next audit page unavailable')
    expect(screen.getByText('k0')).toBeInTheDocument()
    expect(screen.getByText('k49')).toBeInTheDocument()
    expect(screen.queryByText('No events')).not.toBeInTheDocument()
    vi.mocked(api.vectorEvents).mockResolvedValue({ events: [{ event_type: 'memory_write', memory_key: 'recovered-page' }] } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await screen.findByText('recovered-page')
    expect(api.vectorEvents).toHaveBeenLastCalledWith(50, 50)
    expect(screen.getByText('k0')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more…' })).not.toBeInTheDocument()
  })

  it('appends the next page of events', async () => {
    const user = userEvent.setup()
    const page1 = Array.from({ length: 50 }, (_, i) => ({ event_type: 'memory_write', memory_key: `k${i}`, new_value: `v${i}` }))
    setupApi({ events: { events: page1 } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Audit$/))
    await waitFor(() => expect(screen.getByText('k0')).toBeInTheDocument())

    vi.mocked(api.vectorEvents).mockResolvedValue({ events: [{ event_type: 'memory_write', memory_key: 'page2key', new_value: 'v' }] } as never)
    await user.click(screen.getByRole('button', { name: 'Load more…' }))
    await waitFor(() => expect(screen.getByText('page2key')).toBeInTheDocument())
    expect(api.vectorEvents).toHaveBeenLastCalledWith(50, 50)
    expect(screen.queryByRole('button', { name: 'Load more…' })).not.toBeInTheDocument()
  })
})

describe('VectorMemoryCard — Inspector tab', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })

  it('shows an initial preview failure without reporting an empty context', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorContextPreview).mockRejectedValue(new Error('preview unavailable'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Inspector$/))
    expect(await screen.findByRole('alert')).toHaveTextContent('preview unavailable')
    expect(screen.queryByText('Click Preview to see what gets injected into prompts.')).not.toBeInTheDocument()
    expect(screen.queryByText('No context to inject. Add some memories first.')).not.toBeInTheDocument()
    vi.mocked(api.vectorContextPreview).mockResolvedValue({ semantic_context: 'Recovered context' } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await screen.findByText('Recovered context')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('prompts for a preview when the first fetch returns nothing', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Inspector$/))
    await screen.findByText('Click Preview to see what gets injected into prompts.')
    expect(screen.getByText('Memory Inspector')).toBeInTheDocument()
    expect(api.vectorContextPreview).toHaveBeenCalledWith(undefined)
    expect(screen.getByText('Click Preview to see what gets injected into prompts.')).toBeInTheDocument()
  })

  it('renders both context blocks for a query typed and submitted with Enter', async () => {
    const user = userEvent.setup()
    setupApi({ preview: { semantic_context: 'SEMANTIC BLOCK', episodic_context: 'EPISODIC BLOCK' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Inspector$/))
    await waitFor(() => expect(screen.getByText('SEMANTIC BLOCK')).toBeInTheDocument())

    await user.type(screen.getByPlaceholderText(/Test query/), 'which database{Enter}')
    await waitFor(() => expect(api.vectorContextPreview).toHaveBeenLastCalledWith('which database'))
    expect(screen.getByText('Semantic Context (injected at session start)')).toBeInTheDocument()
    expect(screen.getByText('Episodic Context (injected per-message)')).toBeInTheDocument()
    expect(screen.getByText('EPISODIC BLOCK')).toBeInTheDocument()
  })

  it('reports an empty preview payload as nothing to inject', async () => {
    const user = userEvent.setup()
    setupApi({ preview: { semantic_context: '', episodic_context: '' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Inspector$/))

    await waitFor(() =>
      expect(screen.getByText('No context to inject. Add some memories first.')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Preview' }))
    await waitFor(() => expect(api.vectorContextPreview).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Semantic Context (injected at session start)')).not.toBeInTheDocument()
  })

  it('reports a failed preview and retries without losing the query or previous context', async () => {
    const user = userEvent.setup()
    setupApi({ preview: { semantic_context: 'Previous context' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Inspector$/))
    await screen.findByText('Previous context')
    vi.mocked(api.vectorContextPreview).mockRejectedValue(new Error('preview down'))
    await user.type(screen.getByPlaceholderText(/Test query/), 'database{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('preview down')
    expect(screen.getByText('Previous context')).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/Test query/)).toHaveValue('database')
    expect(screen.queryByText('No context to inject. Add some memories first.')).not.toBeInTheDocument()
    vi.mocked(api.vectorContextPreview).mockResolvedValue({ semantic_context: 'Retried context' } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await screen.findByText('Retried context')
    expect(api.vectorContextPreview).toHaveBeenLastCalledWith('database')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    vi.mocked(api.vectorContextPreview).mockRejectedValue(new Error('preview unavailable on return'))
    await user.click(tab(/^Semantic$/))
    await user.click(tab(/^Inspector$/))
    expect(await screen.findByRole('alert')).toHaveTextContent('preview unavailable on return')
    expect(api.vectorContextPreview).toHaveBeenLastCalledWith('database')
    expect(screen.getByPlaceholderText(/Test query/)).toHaveValue('database')
    expect(screen.getByText('Retried context')).toBeInTheDocument()

    vi.mocked(api.vectorContextPreview).mockResolvedValue({ semantic_context: 'Context after return' } as never)
    await user.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await screen.findByText('Context after return')
    expect(api.vectorContextPreview).toHaveBeenLastCalledWith('database')
    const reads = vi.mocked(api.vectorContextPreview).mock.calls.length
    await user.click(tab(/^Inspector$/))
    await waitFor(() => expect(api.vectorContextPreview).toHaveBeenCalledTimes(reads + 1))
    expect(api.vectorContextPreview).toHaveBeenLastCalledWith('database')
  })
})

describe('VectorMemoryCard — semantic write, edit and delete', () => {
  const ENTRY = { key: 'pref.style.tone', value_json: '"terse"', confidence: 1, source: 'user_explicit' }

  beforeEach(() => { vi.clearAllMocks(); setupApi({ semantic: { entries: [ENTRY] } }) })

  it('writes a new pair from the Set button and clears both inputs', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const keyInput = screen.getByPlaceholderText('Key (e.g. pref.backend.framework)')
    // Typing narrows the datalist suggestions.
    await user.type(keyInput, 'user.')
    await user.type(screen.getByPlaceholderText('Value'), 'Zezhen')
    await user.click(screen.getByRole('button', { name: 'Set' }))

    await waitFor(() => expect(api.vectorSemanticWrite).toHaveBeenCalledWith('user.', 'Zezhen'))
    await waitFor(() => expect(keyInput).toHaveValue(''))
    expect(api.vectorStats).toHaveBeenCalledTimes(2)
  })

  it('writes from the Enter key in the value field', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.type(screen.getByPlaceholderText('Key (e.g. pref.backend.framework)'), 'pref.os')
    await user.type(screen.getByPlaceholderText('Value'), 'linux{Enter}')
    await waitFor(() => expect(api.vectorSemanticWrite).toHaveBeenCalledWith('pref.os', 'linux'))
  })

  it('ignores the Set button until both fields are filled', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(screen.getByRole('button', { name: 'Set' }))
    await user.type(screen.getByPlaceholderText('Key (e.g. pref.backend.framework)'), 'pref.shell')
    await user.click(screen.getByRole('button', { name: 'Set' }))
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
  })

  it.each([
    ['an object carrying error', { error: 'object error field' }, 'object error field'],
    ['an object carrying detail', { detail: 'object detail field' }, 'object detail field'],
    ['an object carrying only message', { message: 'object message field' }, 'object message field'],
    ['an opaque object', { weird: 1 }, '{"weird":1}'],
    ['an empty error field', { error: '' }, 'Unknown error'],
    ['an Error wrapping JSON', new Error('{"detail":"json detail"}'), 'json detail'],
    ['a plain Error', new Error('plain failure'), 'plain failure'],
    ['an unserialisable object', CIRCULAR_REJECTION, 'Unknown error'],
  ])('surfaces a failed write from %s', async (_label, rejection, expected) => {
    const user = userEvent.setup()
    vi.mocked(api.vectorSemanticWrite).mockRejectedValue(rejection)
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.type(screen.getByPlaceholderText('Key (e.g. pref.backend.framework)'), 'pref.testing.runner')
    await user.type(screen.getByPlaceholderText('Value'), 'vitest')
    await user.click(screen.getByRole('button', { name: 'Set' }))

    await waitFor(() => expect(screen.getByText(expected)).toBeInTheDocument())
  })

  it('surfaces a failed write triggered from the Enter key', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorSemanticWrite).mockRejectedValue(new Error('enter write refused'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.type(screen.getByPlaceholderText('Key (e.g. pref.backend.framework)'), 'pref.editor.theme')
    await user.type(screen.getByPlaceholderText('Value'), 'dark{Enter}')
    await waitFor(() => expect(screen.getByText('enter write refused')).toBeInTheDocument())
  })

  it('opens inline edit, cancels with Escape, then saves with Enter', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const valueCell = screen.getByText('terse').closest('td')!
    await user.click(valueCell)
    const editInput = await waitFor(() => within(valueCell).getByRole('textbox'))
    expect(editInput).toHaveValue('terse')

    await user.keyboard('{Escape}')
    await waitFor(() => expect(within(valueCell).queryByRole('textbox')).not.toBeInTheDocument())

    await user.click(valueCell)
    const reopened = await waitFor(() => within(valueCell).getByRole('textbox'))
    await user.clear(reopened)
    await user.type(reopened, 'chatty{Enter}')
    await waitFor(() => expect(api.vectorSemanticWrite).toHaveBeenCalledWith('pref.style.tone', 'chatty'))
    await waitFor(() => expect(within(valueCell).queryByRole('textbox')).not.toBeInTheDocument())
  })

  it('saves an inline edit from the confirm button and dismisses with the cancel button', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const valueCell = screen.getByText('terse').closest('td')!
    await user.click(valueCell)
    await waitFor(() => expect(within(valueCell).getByRole('textbox')).toBeInTheDocument())

    // Icon-only confirm / cancel buttons, in DOM order.
    const [confirm] = within(valueCell).getAllByRole('button')
    await user.click(confirm)
    await waitFor(() => expect(api.vectorSemanticWrite).toHaveBeenCalledWith('pref.style.tone', 'terse'))

    await user.click(valueCell)
    await waitFor(() => expect(within(valueCell).getByRole('textbox')).toBeInTheDocument())
    const cancel = within(valueCell).getAllByRole('button')[1]
    await user.click(cancel)
    await waitFor(() => expect(within(valueCell).queryByRole('textbox')).not.toBeInTheDocument())
  })

  it('surfaces a failed inline edit instead of closing the editor', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorSemanticWrite).mockRejectedValue({ error: 'inline write refused' })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const valueCell = screen.getByText('terse').closest('td')!
    await user.click(valueCell)
    await waitFor(() => expect(within(valueCell).getByRole('textbox')).toBeInTheDocument())
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByText('inline write refused')).toBeInTheDocument())
  })

  it('surfaces a failed inline edit saved from the confirm button', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorSemanticWrite).mockRejectedValue({ detail: 'confirm write refused' })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const valueCell = screen.getByText('terse').closest('td')!
    await user.click(valueCell)
    await waitFor(() => expect(within(valueCell).getByRole('textbox')).toBeInTheDocument())
    const [confirm] = within(valueCell).getAllByRole('button')
    await user.click(confirm)
    await waitFor(() => expect(screen.getByText('confirm write refused')).toBeInTheDocument())
    // The editor stays open so the edit is not silently lost.
    expect(within(valueCell).getByRole('textbox')).toBeInTheDocument()
  })

  it('deletes a semantic row and reports a failing delete', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorSemanticDelete).mockRejectedValue(new Error('delete refused'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.getByText('delete refused')).toBeInTheDocument())

    vi.mocked(api.vectorSemanticDelete).mockResolvedValue(undefined as never)
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByText('delete refused')).not.toBeInTheDocument())
    expect(api.vectorSemanticDelete).toHaveBeenCalledWith('pref.style.tone')
  })

  it('clears the filter with the Clear button', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const filter = screen.getByPlaceholderText('Filter by key or value…')
    await user.type(filter, 'nothing-here')
    await waitFor(() => expect(screen.getByText('No matching entries')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.getByText('pref.style.tone')).toBeInTheDocument())
  })
})

describe('VectorMemoryCard — embedding setup progress', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })
  afterEach(() => { vi.useRealTimers() })

  const IDLE_STATS = { semantic_active: 0, episodic_active: 0, embedded_count: 0, migrated: true }

  it('shows the checking step while setup is starting', async () => {
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'checking' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByText('Checking system status…')).toBeInTheDocument())
  })

  it('shows a determinate download label and CDN hint when byte counts are known', async () => {
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'downloading', bytes_downloaded: 50_000_000, bytes_total: 610_000_000 } })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() =>
      expect(screen.getByText('Downloading embedding model (50/610 MB — 8%)…')).toBeInTheDocument())
    expect(screen.getByText('Downloading from CDN…')).toBeInTheDocument()
  })

  it('falls back to the fixed-size download label without byte counts', async () => {
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'downloading' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() =>
      expect(screen.getByText('Downloading embedding model (~610MB)…')).toBeInTheDocument())
  })

  it('labels the verifying sub-step', async () => {
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'downloading', download_step: 'verifying' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByText('Verifying model integrity…')).toBeInTheDocument())
  })

  it('labels a retrying download with its attempt number', async () => {
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'downloading', download_step: 'waiting_retry', download_attempt: 3 } })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByText('Retrying download (attempt 3)…')).toBeInTheDocument())
  })

  it('shows a rejected setup restart and keeps Retry available until it succeeds', async () => {
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'error', setup_error: 'Download failed' } })
    // A failing restart request must not escape as an unhandled rejection.
    vi.mocked(api.vectorEnableEmbeddings).mockRejectedValueOnce(new Error('restart refused'))
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.vectorEnableEmbeddings).toHaveBeenCalled())
    expect(await screen.findByRole('alert')).toHaveTextContent('restart refused')
    expect(screen.queryByText('Download failed. Check network connectivity and try again.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.vectorEnableEmbeddings).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('restart refused')).not.toBeInTheDocument()
  })

  it('polls until setup reports done, then stops polling and shows the active card', async () => {
    vi.useFakeTimers()
    setupApi({ stats: ACTIVE_STATS })
    vi.mocked(api.vectorEmbeddingStatus)
      .mockResolvedValueOnce({ provider: 'none', setup_step: 'downloading', bytes_downloaded: 5_000_000, bytes_total: 610_000_000 } as never)
      // A malformed poll reports an error without blanking the previous status.
      .mockResolvedValueOnce(null as never)
      .mockResolvedValue({ provider: 'llama_cpp', setup_step: 'done', model_available: true, model_id: 'qwen3-embedding:0.6b', model_dim: 1024 } as never)

    const { unmount } = renderWithProviders(<VectorMemoryCard />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText(/Downloading embedding model/)).toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(screen.getByText(/Downloading embedding model/)).toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByRole('button', { name: /Inspector/i })).toBeInTheDocument()

    const settled = vi.mocked(api.vectorEmbeddingStatus).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(vi.mocked(api.vectorEmbeddingStatus).mock.calls.length).toBe(settled)

    unmount()
  })

  it('reports a failing status poll while keeping the current step on screen', async () => {
    vi.useFakeTimers()
    setupApi({ stats: IDLE_STATS })
    vi.mocked(api.vectorEmbeddingStatus)
      .mockResolvedValueOnce({ provider: 'none', setup_step: 'checking' } as never)
      .mockRejectedValue(new Error('status endpoint down'))

    const { unmount } = renderWithProviders(<VectorMemoryCard />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('Checking system status…')).toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    // The last status remains visible alongside the read failure.
    expect(screen.getByText('Checking system status…')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('status endpoint down')
    unmount()
  })

  it('continues polling when the setup step advances mid-flight', async () => {
    vi.useFakeTimers()
    setupApi({ stats: IDLE_STATS })
    vi.mocked(api.vectorEmbeddingStatus)
      .mockResolvedValueOnce({ provider: 'none', setup_step: 'checking' } as never)
      .mockResolvedValueOnce({ provider: 'none', setup_step: 'downloading' } as never)
      .mockResolvedValue({ provider: 'llama_cpp', setup_step: 'done', model_available: true } as never)

    const { unmount } = renderWithProviders(<VectorMemoryCard />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('Checking system status…')).toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(screen.getByText('Downloading embedding model (~610MB)…')).toBeInTheDocument()

    const afterTransition = vi.mocked(api.vectorEmbeddingStatus).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(vi.mocked(api.vectorEmbeddingStatus).mock.calls.length).toBeGreaterThan(afterTransition)
    expect(screen.getByRole('button', { name: /Inspector/i })).toBeInTheDocument()
    unmount()
  })

  it('clears a live poll interval on unmount', async () => {
    vi.useFakeTimers()
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'checking' } })
    const { unmount } = renderWithProviders(<VectorMemoryCard />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('Checking system status…')).toBeInTheDocument()

    unmount()
    const settled = vi.mocked(api.vectorEmbeddingStatus).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(vi.mocked(api.vectorEmbeddingStatus).mock.calls.length).toBe(settled)
  })
})

describe('VectorMemoryCard — active header states', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })

  it.each([
    ['en', 'Embedding status', 'Embeddings'],
    ['zh-CN', '嵌入状态', '嵌入'],
  ])('names the status tile distinctly in %s', async (locale, label, oldLabel) => {
    await i18next.changeLanguage(locale)
    try {
      renderWithProviders(<VectorMemoryCard />)
      expect(await screen.findByText(label, { exact: true })).toBeInTheDocument()
      expect(screen.queryByText(oldLabel, { exact: true })).not.toBeInTheDocument()
      expect(screen.getByTestId('embeddings-stat-badge')).toHaveAttribute('data-state', 'active')
    } finally {
      await act(async () => { await i18next.changeLanguage('en') })
    }
  })

  it('falls back to faiss_index_size for the embedded stat and reads not active when nothing serves', async () => {
    setupApi({
      stats: { semantic_active: 0, episodic_active: 4, faiss_index_size: 77, migrated: false },
      emb: { provider: 'llama_cpp', setup_step: 'idle', model_available: false, server_healthy: false },
    })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    expect(screen.getByText('77')).toBeInTheDocument()
    // Nothing is progressing at setup_step idle: an older backend with no
    // model_active answers through model_available, and a false answer is a
    // neutral not-active, never a guessed "model loading".
    const badge = screen.getByTestId('embeddings-stat-badge')
    expect(badge).toHaveAttribute('data-state', 'inactive')
    expect(badge).toHaveTextContent('not active')
    expect(screen.queryByText('model loading')).not.toBeInTheDocument()
  })

  // Regression for the configured-inactive gateway: the Embedding Model header
  // on the same tab reads "Configured: … · not active", so the Embeddings tile
  // must not contradict it with a warning that claims the model is loading.
  it('reads a known configured model with model_active=false as not active, not loading', async () => {
    setupApi({
      emb: {
        provider: 'llama_cpp', setup_step: 'idle', model_id: 'qwen3-embedding:0.6b', model_dim: 1024,
        model_source: 'default', model_available: false, server_healthy: false, model_active: false,
      },
    })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const badge = screen.getByTestId('embeddings-stat-badge')
    expect(badge).toHaveAttribute('data-state', 'inactive')
    expect(badge).toHaveTextContent('not active')
    expect(badge).toHaveClass('text-[var(--muted)]')
    expect(badge).not.toHaveClass('text-warn')
    expect(screen.queryByText('model loading')).not.toBeInTheDocument()
    expect(screen.queryByText('active', { exact: true })).not.toBeInTheDocument()
  })

  it('model_active=false wins over a present file: the file is loaded on first use, not loading now', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, setup_step: 'done', model_available: true, server_healthy: true, model_active: false } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const badge = screen.getByTestId('embeddings-stat-badge')
    expect(badge).toHaveAttribute('data-state', 'inactive')
    expect(badge).toHaveTextContent('not active')
  })

  it('reads active only when model_active is true, or when an older backend reports the file present', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, model_available: false, server_healthy: false, model_active: true } })
    const first = renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    expect(screen.getByTestId('embeddings-stat-badge')).toHaveAttribute('data-state', 'active')
    expect(screen.getByTestId('embeddings-stat-badge')).toHaveTextContent('active')
    first.unmount()

    setupApi({ emb: { provider: 'llama_cpp', setup_step: 'done', model_available: true } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    expect(screen.getByTestId('embeddings-stat-badge')).toHaveAttribute('data-state', 'active')
    expect(screen.getByTestId('embeddings-stat-badge')).toHaveTextContent('active')
  })

  it('a genuinely progressing setup shows its progress, never a not-active tile', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, setup_step: 'downloading', model_available: false, model_active: false } })
    renderWithProviders(<VectorMemoryCard />)
    // A live download flips the card into its progress panel, so the stat
    // grid (and this tile) is not what the user sees for a progressing setup.
    await waitFor(() => expect(screen.getAllByText('Downloading embedding model (~610MB)…').length).toBeGreaterThanOrEqual(1))
    expect(screen.queryByText('not active')).not.toBeInTheDocument()
    expect(screen.queryByText('unknown', { exact: true })).not.toBeInTheDocument()
  })

  it('a failed setup reads setup failed, never not active or loading', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, setup_step: 'error', setup_error: 'boom', model_available: false, model_active: false } })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByTestId('embeddings-stat-badge')).toHaveAttribute('data-state', 'progress'))
    expect(screen.getByTestId('embeddings-stat-badge')).toHaveTextContent('Setup failed')
    expect(screen.queryByText('not active')).not.toBeInTheDocument()
    expect(screen.queryByText('model loading')).not.toBeInTheDocument()
  })

  it('reads unknown when no field answers whether the model serves', async () => {
    setupApi({ emb: { provider: 'llama_cpp', setup_step: 'idle' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const badge = screen.getByTestId('embeddings-stat-badge')
    expect(badge).toHaveAttribute('data-state', 'unknown')
    expect(badge).toHaveTextContent('unknown')
    expect(badge).toHaveClass('text-[var(--muted)]')
    expect(screen.queryByText('model loading')).not.toBeInTheDocument()
    expect(screen.queryByText('not active')).not.toBeInTheDocument()
  })

  it('translates the not-active tile in the active language', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, setup_step: 'idle', model_available: false, server_healthy: false, model_active: false } })
    const { rerender } = renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await act(async () => { await i18next.changeLanguage('zh-CN') })
    rerender(<VectorMemoryCard />)
    expect(screen.getByTestId('embeddings-stat-badge')).toHaveTextContent('未启用')
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('discloses the embedding model under the badge once one is known', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, model_id: 'qwen3-embedding:0.6b', model_dim: 1024 } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    expect(screen.getByText('Qwen3-Embedding-0.6B · 1024-dim')).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
  })

  it('shows the starting-up copy when the model is loaded but no memory exists yet', async () => {
    setupApi({
      stats: { semantic_active: 0, episodic_active: 0, migrated: true },
      emb: { provider: 'none', setup_step: 'idle', model_available: true },
    })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() =>
      expect(screen.getByText('Model loaded. Embedding engine is starting up.')).toBeInTheDocument())
  })
})

describe('VectorMemoryCard — embedding setup warning', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })

  const WARNING = 'These memory vectors were built before the model file that produced them was recorded.'

  it('renders the backend warning with a link to the embedding model settings', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, model_source: 'custom', setup_warning: WARNING } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    const notice = screen.getByTestId('embedding-setup-warning')
    expect(notice).toHaveAttribute('role', 'status')
    expect(notice).toHaveTextContent(WARNING)
    const link = within(notice).getByRole('link', { name: 'Open embedding model settings' })
    expect(link).toHaveAttribute('href', '#embed-model-path')
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders no warning when the field is empty or absent', async () => {
    setupApi({ emb: { ...ACTIVE_EMB, setup_warning: '' } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    expect(screen.queryByTestId('embedding-setup-warning')).not.toBeInTheDocument()
  })
})


describe('VectorMemoryCard localized backend status', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('renders coded warning and error in Chinese while keeping the settings action', async () => {
    await i18next.changeLanguage('zh-CN')
    setupApi({ emb: {
      ...ACTIVE_EMB,
      setup_warning: 'Backend warning fallback', setup_warning_code: 'legacy_embedding_vectors',
      setup_error: 'Backend error fallback', setup_error_code: 'model_path_not_found',
      setup_error_params: { path: '/models/missing.gguf' },
      repair: { generation: 'r', pending_invalidation: 1, pending_vectors: 3, deferred_stores: 2 },
    } })
    renderWithProviders(<VectorMemoryCard />)
    const notice = await screen.findByTestId('embedding-setup-warning')
    expect(notice).toHaveTextContent('这些向量')
    expect(within(notice).getByRole('link')).toHaveAttribute('href', '#embed-model-path')
    expect(screen.queryByText('Backend warning fallback')).not.toBeInTheDocument()
    expect(screen.queryByText('Backend error fallback')).not.toBeInTheDocument()
    expect(await screen.findByText(/路径：\/models\/missing.gguf/)).toBeInTheDocument()
    // With the file missing, the warning tells the user to fix the path first, not to reapply it.
    expect(notice).toHaveTextContent('先修正模型路径')
    // The standing-rebuild summary is rendered once, on the Embedding Model card, not here.
    expect(screen.queryByText(/待生成新向量/)).not.toBeInTheDocument()
    expect(screen.queryByText(/待重建向量/)).not.toBeInTheDocument()
  })

  it('folds the raw backend exception under a localized known-code notice', async () => {
    await i18next.changeLanguage('zh-CN')
    const raw = 'memory.embed_model_path could not be read: [Errno 5] Input/output error'
    setupApi({ emb: {
      ...ACTIVE_EMB,
      setup_error: raw, setup_error_code: 'model_verification_failed',
      setup_error_params: { path: '/models/model.gguf', error: raw },
    } })
    renderWithProviders(<VectorMemoryCard />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('/models/model.gguf')
    expect(alert).toHaveTextContent('重新应用')
    expect(alert).not.toHaveTextContent('Errno')
    const details = screen.getByTestId('embedding-setup-diagnostic')
    expect(details.tagName).toBe('DETAILS')
    expect(details).not.toHaveAttribute('open')
    expect(within(details).getByText(raw)).toHaveAttribute('translate', 'no')
  })

  it('offers no diagnostic fold for a path code, whose body already says it all', async () => {
    setupApi({ emb: {
      ...ACTIVE_EMB,
      setup_error: 'no file', setup_error_code: 'model_path_not_found',
      setup_error_params: { path: '/models/missing.gguf', error: 'no file' },
    } })
    renderWithProviders(<VectorMemoryCard />)
    await screen.findByRole('alert')
    expect(screen.queryByTestId('embedding-setup-diagnostic')).not.toBeInTheDocument()
  })
})


describe('EmbeddingModelCard path state gates Apply', () => {
  const MISSING = {
    ...ACTIVE_EMB, model_source: 'custom', model_path: '/models/missing.gguf', model_id: 'custom-model', model_dim: 2,
    setup_error: 'The model path points at a file that does not exist', setup_error_code: 'model_path_not_found',
    setup_error_params: { path: '/models/missing.gguf', error: 'The model path points at a file that does not exist' },
    setup_warning: 'legacy', setup_warning_code: 'legacy_embedding_vectors',
    reembed: { step: 'idle' },
  }

  it('shows the known path error under the field and disables Apply on first load', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: MISSING })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<EmbeddingModelCard />)
    const card = screen.getByTestId('embed-model-card')
    const error = await screen.findByTestId('embed-model-path-status-error')
    expect(error).toHaveTextContent('No file at that path.')
    expect(error).toHaveAttribute('role', 'alert')
    const input = screen.getByDisplayValue('/models/missing.gguf')
    expect(input).toHaveAttribute('aria-describedby', error.id)
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(within(error).queryByRole('button')).not.toBeInTheDocument()
    expect(error).not.toHaveTextContent('points at a file')
    expect(within(card).getByRole('button', { name: 'Rebuild memory vectors' })).toBeDisabled()
    expect(screen.getByDisplayValue('/models/missing.gguf')).toBeInTheDocument()
  })

  it('lets the live check override the stale status once the user edits or re-checks the path', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: MISSING })
    const validate = vi.fn(async () => ({ ok: true, size_bytes: 2 * 1024 * 1024 }))
    Object.assign(api, { vectorValidateEmbedModel: validate, vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByTestId('embed-model-path-status-error')
    const input = screen.getByDisplayValue('/models/missing.gguf')
    // The file was restored in place: one blur re-checks the SAME path and re-enables Apply.
    fireEvent.blur(input)
    await waitFor(() => expect(validate).toHaveBeenCalledWith('/models/missing.gguf'))
    await waitFor(() => expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument())
    await waitFor(() => expect(within(screen.getByTestId('embed-model-card')).getByRole('button', { name: 'Rebuild memory vectors' })).toBeEnabled())
  })

  it('still lets an emptied path revert to the bundled model', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: MISSING })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<EmbeddingModelCard />)
    await screen.findByTestId('embed-model-path-status-error')
    const input = screen.getByDisplayValue('/models/missing.gguf')
    fireEvent.change(input, { target: { value: '' } })
    // Clearing the field is an edit like any other: the last verdict and the
    // gate stay until the check on blur replaces them.
    expect(screen.getByTestId('embed-model-path-status-error')).toBeInTheDocument()
    expect(within(screen.getByTestId('embed-model-card')).getByRole('button', { name: /apply/i })).toBeDisabled()
    fireEvent.blur(input)
    await screen.findByText(/revert to the bundled model/)
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
    expect(within(screen.getByTestId('embed-model-card')).getByRole('button', { name: /apply/i })).toBeEnabled()
    expect(api.vectorValidateEmbedModel).not.toHaveBeenCalled()
  })

  it('renders the standing rebuild once, in user vocabulary, with the three counts kept apart', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: {
      ...ACTIVE_EMB, model_path: '', reembed: { step: 'deferred' },
      repair: { generation: 'r', pending_invalidation: 2, pending_vectors: 5, deferred_stores: 1 },
    } })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<><VectorMemoryCard diagnosticsOnly /><EmbeddingModelCard /></>)
    const line = await screen.findByTestId('embed-model-repair-status')
    expect(line).toHaveTextContent('5 memories still need a new vector')
    expect(line).toHaveTextContent('2 open stores still hold old vectors')
    expect(line).toHaveTextContent('1 closed or unavailable store will be rebuilt when it next opens')
    expect(line).toHaveAttribute('role', 'status')
    // The counts span every memory store (open ones counted, closed ones
    // deferred) while the tiles above count only the store shown, so the
    // sentence names its scope; the reader could not otherwise reconcile the two.
    expect(line).toHaveTextContent('across all memory stores:')
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(line).not.toHaveTextContent(/\b7\b/)
    expect(screen.getAllByText(/still need a new vector/)).toHaveLength(1)
  })

  it('does not hide an invalidation-only pending state', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: {
      ...ACTIVE_EMB, model_path: '', reembed: { step: 'deferred' },
      repair: { generation: 'r', pending_invalidation: 3, pending_vectors: 0, deferred_stores: 0 },
    } })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<EmbeddingModelCard />)
    expect(await screen.findByTestId('embed-model-repair-status')).toHaveTextContent('3 open stores still hold old vectors')
  })

  it('names the retry and the log for an unknown scope instead of promising a fix', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: { ...ACTIVE_EMB, model_path: '', reembed: { step: 'deferred' }, repair: { generation: 'r', unknown_scope: true } } })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<EmbeddingModelCard />)
    const line = await screen.findByTestId('embed-model-repair-status')
    expect(line).toHaveTextContent('retries automatically')
    expect(line).toHaveTextContent('gateway log')
  })
})


describe('EmbeddingModelCard header separates configured from active', () => {
  const KNOWN = { ...ACTIVE_EMB, model_id: 'qwen3-embedding:0.6b', model_dim: 1024, model_source: 'default', model_path: '', reembed: { step: 'idle' } }

  async function renderCard(emb: Loose) {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<EmbeddingModelCard />)
    return screen.findByTestId('embed-model-active-badge')
  }

  it('names the configured model as not active, with no success colour, when the backend says it is not serving', async () => {
    const badge = await renderCard({ ...KNOWN, model_active: false })
    const header = screen.getByTestId('embed-model-active')
    expect(header).toHaveTextContent('Configured: qwen3-embedding:0.6b · 1024d · not active')
    expect(header).not.toHaveTextContent(/^Active:/)
    expect(header).not.toHaveTextContent('Active model unknown')
    expect(badge).toHaveTextContent('bundled')
    expect(badge).toHaveAttribute('data-state', 'inactive')
    expect(badge.className).not.toMatch(/text-ok/)
  })

  it('keeps the active wording and the success colour once the model serves', async () => {
    const badge = await renderCard({ ...KNOWN, model_active: true })
    expect(screen.getByTestId('embed-model-active')).toHaveTextContent('Active: qwen3-embedding:0.6b · 1024d')
    expect(badge).toHaveAttribute('data-state', 'active')
    expect(badge.className).toMatch(/text-ok/)
  })

  it('treats a status without model_active as active, for an older backend', async () => {
    const badge = await renderCard({ ...KNOWN, model_active: undefined })
    expect(screen.getByTestId('embed-model-active')).toHaveTextContent('Active: qwen3-embedding:0.6b · 1024d')
    expect(badge).toHaveAttribute('data-state', 'active')
  })

  it.each(['default', 'custom'])('omits the %s provenance badge when the model identity is missing', async (model_source) => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: { ...KNOWN, model_source, model_id: '', model_dim: 0, model_active: false } })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<EmbeddingModelCard />)
    const header = await screen.findByTestId('embed-model-active')
    expect(header).toHaveTextContent('Active model unknown')
    expect(header).toHaveAttribute('data-state', 'unknown')
    expect(screen.queryByTestId('embed-model-active-badge')).not.toBeInTheDocument()
  })

  it('shows a custom model that is still loading as configured, not active', async () => {
    const badge = await renderCard({ ...KNOWN, model_source: 'custom', model_path: '/models/m.gguf', model_id: 'custom-model', model_dim: 2, model_active: false, reembed: { step: 'applying' } })
    expect(screen.getByTestId('embed-model-active')).toHaveTextContent('Configured: custom-model · 2d · not active')
    expect(badge).toHaveTextContent('custom')
    expect(badge).toHaveAttribute('data-state', 'inactive')
    expect(badge.className).not.toMatch(/text-aim/)
  })
})


describe('a model path error is reported once, under the path field', () => {
  const PATH_ERROR = {
    ...ACTIVE_EMB, model_source: 'custom', model_path: '/models/missing.gguf', model_id: 'custom-model', model_dim: 2,
    setup_error: 'The model path points at a file that does not exist', setup_error_code: 'model_path_not_found',
    setup_error_params: { path: '/models/missing.gguf', error: 'The model path points at a file that does not exist' },
    reembed: { step: 'idle' },
  }

  it('points from the Vector Memory card to the field instead of repeating the message and path', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: { ...PATH_ERROR, setup_step: 'error', model_active: false } })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<><VectorMemoryCard /><EmbeddingModelCard /></>)
    const field = await screen.findByTestId('embed-model-path-status-error')
    // A model identifier is not a repeated error: the existing disclosure
    // names the custom file even when the configured model is unavailable.
    const disclosure = screen.getByText('missing.gguf · 2-dim')
    expect(disclosure).toHaveAttribute('title', 'custom-model · 2-dim · runs locally in-process — /models/missing.gguf')
    expect(screen.getAllByText(/missing\.gguf/)).toEqual([disclosure])
    expect(field).toHaveTextContent('No file at that path.')
    const pointer = await screen.findByTestId('embedding-setup-error-pointer')
    expect(pointer).toHaveTextContent('keyword search')
    expect(within(pointer).getByRole('link', { name: 'Open embedding model settings' })).toHaveAttribute('href', '#embed-model-path')
    expect(pointer).not.toHaveTextContent('missing.gguf')
    expect(field).not.toHaveTextContent('missing.gguf')
    expect(pointer).not.toHaveTextContent('No file at that path.')
    // Where the fix lives is said ONCE, by the link: the pointer's prose ends
    // at the consequence (keyword search) and does not restate the destination.
    expect(within(pointer).getAllByText(/settings/i)).toHaveLength(1)
    expect(pointer).not.toHaveTextContent('Fix the path')
    // The localized message body renders exactly once on the page: under the field.
    expect(screen.getAllByText(/No file at that path\./)).toHaveLength(1)
    expect(screen.queryByText(/Path: \/models\/missing\.gguf/)).not.toBeInTheDocument()
  })

  it('keeps the full message and path on the Vector Memory card when no path field is on the page', async () => {
    setupApi({ emb: PATH_ERROR })
    renderWithProviders(<VectorMemoryCard />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('No file at that path.')
    expect(alert).toHaveTextContent('Path: /models/missing.gguf')
    expect(screen.queryByTestId('embedding-setup-error-pointer')).not.toBeInTheDocument()
  })

  it('does not shorten a non-path error even when the field is on the page', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    const raw = 'memory.embed_model_path could not be read: [Errno 5] Input/output error'
    setupApi({ emb: { ...PATH_ERROR, setup_error: raw, setup_error_code: 'model_verification_failed', setup_error_params: { path: '/models/missing.gguf', error: raw } } })
    Object.assign(api, { vectorValidateEmbedModel: vi.fn(), vectorApplyEmbedModel: vi.fn() })
    renderWithProviders(<><VectorMemoryCard /><EmbeddingModelCard /></>)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('/models/missing.gguf')
    expect(screen.queryByTestId('embedding-setup-error-pointer')).not.toBeInTheDocument()
    expect(screen.getByTestId('embedding-setup-diagnostic')).toBeInTheDocument()
    expect(screen.queryByTestId('embed-model-path-status-error')).not.toBeInTheDocument()
  })
})


describe('embedding model apply shares status with vector memory', () => {
  it('clears the sibling warning without remounting either card', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    let applied = false
    setupApi()
    Object.assign(api, { vectorApplyEmbedModel: vi.fn(async () => { applied = true; return { ok: true } }) })
    vi.mocked(api.vectorEmbeddingStatus).mockImplementation(async () => ({
      ...ACTIVE_EMB, model_path: '/models/model.gguf', model_id: 'custom-model', model_dim: 2,
      setup_warning_code: applied ? '' : 'legacy_embedding_vectors',
      setup_warning: applied ? '' : 'legacy warning',
      reembed: { step: applied ? 'done' : 'idle' },
    }))
    renderWithProviders(<><EmbeddingModelCard /><VectorMemoryCard diagnosticsOnly /></>)
    await screen.findByTestId('embedding-setup-warning')
    const card = screen.getByTestId('embed-model-card')
    // The field holds the configured path, so this is a reapply of that path.
    fireEvent.click(within(card).getByRole('button', { name: 'Rebuild memory vectors' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Rebuild memory vectors' }))
    await waitFor(() => expect(applied).toBe(true))
    await waitFor(() => expect(screen.queryByTestId('embedding-setup-warning')).not.toBeInTheDocument())
  })
})

describe('embedding setup UX regressions', () => {
  const missing = {
    ...ACTIVE_EMB, model_source: 'custom', model_path: '/models/missing.gguf',
    setup_warning_code: 'legacy_embedding_vectors', setup_error_code: 'model_path_not_found',
    setup_error_params: { path: '/models/missing.gguf' }, reembed: { step: 'idle' },
  }

  it('keeps one settings link in the legacy path warning and the full error under the field', async () => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: missing })
    renderWithProviders(<><VectorMemoryCard /><EmbeddingModelCard /></>)
    await screen.findByTestId('embed-model-path-status-error')
    await waitFor(() => expect(screen.queryByTestId('embedding-setup-error-pointer')).not.toBeInTheDocument())
    const links = screen.getAllByRole('link', { name: 'Open embedding model settings' })
    expect(links).toHaveLength(1)
    expect(screen.getByTestId('embedding-setup-warning')).toContainElement(links[0])
    expect(links[0]).toHaveAttribute('href', '#embed-model-path')
    expect(document.getElementById('embed-model-path')).toBeInTheDocument()
    expect(screen.getAllByText(/No file at that path\./)).toHaveLength(1)
  })

  it('retains the full legacy path error without the sibling field', async () => {
    setupApi({ emb: missing })
    renderWithProviders(<VectorMemoryCard />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Path: /models/missing.gguf')
    expect(screen.getByTestId('embedding-setup-warning')).toBeInTheDocument()
  })

  it.each(['checking', 'installing_faiss', 'future_step'])('does not expose raw %s in the pill or progress title', async step => {
    setupApi({ emb: { ...ACTIVE_EMB, setup_step: step } })
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalled())
    await screen.findByText('Embedding status')
    expect(screen.queryByText(step, { exact: true })).not.toBeInTheDocument()
    const label = step === 'checking' ? 'Checking system status…'
      : step === 'installing_faiss' ? 'Model loaded. Embedding engine is starting up.' : 'model loading'
    expect(screen.getAllByText(label).length).toBeGreaterThanOrEqual(1)
  })

  it.each([
    ['applying', 0, 0, undefined],
    ['running', 0, 0, undefined],
    ['running', 1, 4, '25'],
    ['failed', 1, 4, '25'],
    ['done', 4, 4, '100'],
  ])('preserves %s progress semantics (%s/%s)', async (step, done, total, value) => {
    const { default: EmbeddingModelCard } = await import('../pages/overview/EmbeddingModelCard')
    setupApi({ emb: { ...ACTIVE_EMB, reembed: { step, done, total } } })
    renderWithProviders(<EmbeddingModelCard />)
    const bar = await screen.findByRole('progressbar')
    if (value === undefined) expect(bar).not.toHaveAttribute('aria-valuenow')
    else expect(bar).toHaveAttribute('aria-valuenow', value)
  })
})
