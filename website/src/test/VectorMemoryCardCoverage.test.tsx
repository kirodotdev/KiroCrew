import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, within, act, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VectorMemoryCard, { parseTags, semanticValueText } from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

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

  it('swallows every failing load call and stays on the loading card', async () => {
    vi.mocked(api.vectorStats).mockRejectedValue(new Error('stats down'))
    vi.mocked(api.vectorEmbeddingStatus).mockRejectedValue(new Error('emb down'))
    vi.mocked(api.vectorSemantic).mockRejectedValue(new Error('semantic down'))

    renderWithProviders(<VectorMemoryCard />)

    await waitFor(() => expect(api.vectorStats).toHaveBeenCalled())
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.getByText('Vector Memory')).toBeInTheDocument()
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

describe('VectorMemoryCard — Episodic tab', () => {
  beforeEach(() => { vi.clearAllMocks(); setupApi() })

  it('renders an empty episodic table on first open', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText('No episodic entries')).toBeInTheDocument())
    expect(api.vectorEpisodic).toHaveBeenCalledWith(50, 0, undefined)
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

    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'coverage{Enter}')
    await waitFor(() => expect(screen.getByText('scored hit')).toBeInTheDocument())
    expect(api.vectorEpisodicSearch).toHaveBeenCalledWith('coverage', undefined)
    expect(screen.getByText('Score')).toBeInTheDocument()
    expect(screen.getByText('0.912')).toBeInTheDocument()

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

  it('appends the next page when Load more is used', async () => {
    const user = userEvent.setup()
    const page1 = Array.from({ length: 50 }, (_, i) => ({ id: `p1-${i}`, text: `first ${i}`, tags: [], importance: 0.99 }))
    setupApi({ episodic: { entries: page1 } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText('first 0')).toBeInTheDocument())

    vi.mocked(api.vectorEpisodic).mockResolvedValue({ entries: [{ id: 'p2-0', text: 'second page row', tags: [], importance: 0.99 }] } as never)
    await user.click(screen.getByRole('button', { name: 'Load more…' }))
    await waitFor(() => expect(screen.getByText('second page row')).toBeInTheDocument())
    expect(api.vectorEpisodic).toHaveBeenLastCalledWith(50, 50, undefined)
    // Still holds the first page, and the exhausted page hides Load more.
    expect(screen.getByText('first 0')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more…' })).not.toBeInTheDocument()
  })

  it('degrades to an empty list when the browse and search calls fail', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorEpisodic).mockRejectedValue(new Error('episodic down'))
    vi.mocked(api.vectorEpisodicSearch).mockRejectedValue(new Error('search down'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText('No episodic entries')).toBeInTheDocument())

    await user.type(screen.getByPlaceholderText('Search episodic memories…'), 'anything{Enter}')
    await waitFor(() => expect(api.vectorEpisodicSearch).toHaveBeenCalled())
    expect(screen.getByText('No episodic entries')).toBeInTheDocument()
  })

  it('deletes an episodic row optimistically', async () => {
    const user = userEvent.setup()
    setupApi({ episodic: { entries: EPISODIC_ROWS.slice(0, 2) } })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()
    await user.click(tab(/^Episodic$/))
    await waitFor(() => expect(screen.getByText(/shipped the coverage wave/)).toBeInTheDocument())

    const row = screen.getByText(/shipped the coverage wave/).closest('tr')!
    await user.click(within(row).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByText(/shipped the coverage wave/)).not.toBeInTheDocument())
    expect(api.vectorEpisodicDelete).toHaveBeenCalledWith('e1')
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

  it('shows an empty audit list when the events call fails', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorEvents).mockRejectedValue(new Error('events down'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Audit$/))
    await waitFor(() => expect(screen.getByText('No events')).toBeInTheDocument())
    expect(api.vectorEvents).toHaveBeenCalledWith(50, 0)
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

  it('prompts for a preview when the first fetch returns nothing', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Inspector$/))
    await waitFor(() => expect(screen.getByText('Memory Inspector')).toBeInTheDocument())
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

  it('keeps the prompt when the preview call fails', async () => {
    const user = userEvent.setup()
    vi.mocked(api.vectorContextPreview).mockRejectedValue(new Error('preview down'))
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    await user.click(tab(/^Inspector$/))
    await waitFor(() => expect(api.vectorContextPreview).toHaveBeenCalled())
    expect(screen.getByText('Click Preview to see what gets injected into prompts.')).toBeInTheDocument()
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

  it('restarts setup from the Retry button and renders the error progress state', async () => {
    setupApi({ stats: IDLE_STATS, emb: { provider: 'none', setup_step: 'error', setup_error: 'Download failed' } })
    // A failing restart request must not escape as an unhandled rejection.
    vi.mocked(api.vectorEnableEmbeddings).mockRejectedValue(new Error('restart refused'))
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.vectorEnableEmbeddings).toHaveBeenCalled())
    // The progress block takes over and renders the error variant.
    expect(screen.getByText('Download failed')).toBeInTheDocument()
    expect(screen.getByText('Download failed. Check network connectivity and try again.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/i })).not.toBeInTheDocument()
  })

  it('polls until setup reports done, then stops polling and shows the active card', async () => {
    vi.useFakeTimers()
    setupApi({ stats: ACTIVE_STATS })
    vi.mocked(api.vectorEmbeddingStatus)
      .mockResolvedValueOnce({ provider: 'none', setup_step: 'downloading', bytes_downloaded: 5_000_000, bytes_total: 610_000_000 } as never)
      // A null poll response is ignored rather than blanking the status.
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

  it('ignores a failing status poll and keeps the current step on screen', async () => {
    vi.useFakeTimers()
    setupApi({ stats: IDLE_STATS })
    vi.mocked(api.vectorEmbeddingStatus)
      .mockResolvedValueOnce({ provider: 'none', setup_step: 'checking' } as never)
      .mockRejectedValue(new Error('status endpoint down'))

    const { unmount } = renderWithProviders(<VectorMemoryCard />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('Checking system status…')).toBeInTheDocument()

    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    // A rejected poll is swallowed; the step label is not blanked.
    expect(screen.getByText('Checking system status…')).toBeInTheDocument()
    unmount()
  })

  it('KNOWN DEFECT: polling dies when the setup step advances mid-flight', async () => {
    // The status-poll effect keys on `embStatus?.setup_step`, so the first step
    // change (checking -> downloading) runs its cleanup and clears the interval.
    // The effect body then cannot restart it because its guard requires
    // `!enabling`, which is already true. The card is left showing the
    // downloading label forever and never observes 'done'.
    //
    // This test pins the current behaviour. When the effect is fixed, flip the
    // final expectation to assert that polling CONTINUES past the transition.
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
    expect(vi.mocked(api.vectorEmbeddingStatus).mock.calls.length).toBe(afterTransition)
    expect(screen.getByText('Downloading embedding model (~610MB)…')).toBeInTheDocument()
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

  it('falls back to faiss_index_size for the embedded stat and warns while the model loads', async () => {
    setupApi({
      stats: { semantic_active: 0, episodic_active: 4, faiss_index_size: 77, migrated: false },
      emb: { provider: 'llama_cpp', setup_step: 'idle', model_available: false, server_healthy: false },
    })
    renderWithProviders(<VectorMemoryCard />)
    await waitForActive()

    expect(screen.getByText('77')).toBeInTheDocument()
    expect(screen.getByText('model loading')).toBeInTheDocument()
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
