import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api } from '../api/client'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VectorMemoryCard, { SEMANTIC_RENDER_CAP } from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { i18next } from '../i18n/all'
import { i18nT } from '../i18n/t'

// The Semantic Memory table caps the rendered rows at SEMANTIC_RENDER_CAP and
// relies on the filter (key OR value) to reach entries past the cap. Rendering
// every entry synchronously would freeze the Settings page for 10-20s in
// vector-only mode.

const N = SEMANTIC_RENDER_CAP + 150 // comfortably past the cap
const VALUE_MARKER = 'zeta-region-marker' // lives only in one entry's (object) value, never in a key
const OBJECT_IDX = SEMANTIC_RENDER_CAP + 100 // past the cap, so only reachable via filter

const makeEntries = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    key: `alpha.${i}`,
    // one entry carries an OBJECT value whose content holds VALUE_MARKER; the
    // rest are plain string values. value_json mirrors the DB (JSON text).
    value_json: i === OBJECT_IDX ? JSON.stringify({ region: VALUE_MARKER, n: i }) : `"val-${i}"`,
    confidence: 1.0,
    source: 'user_explicit',
  }))

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(async () => ({ semantic_active: N, episodic_active: 0, embedded_count: 0, migrated: true })),
    vectorEmbeddingStatus: vi.fn(async () => ({ provider: 'none', setup_step: 'idle' })),
    vectorSemantic: vi.fn(async () => ({ entries: makeEntries(N) })),
    vectorSemanticWrite: vi.fn(async () => undefined),
    vectorSemanticDelete: vi.fn(async () => undefined),
    vectorEpisodic: vi.fn(async () => ({ entries: [] })),
    vectorEvents: vi.fn(async () => ({ events: [] })),
    vectorContextPreview: vi.fn(async () => null),
  },
}))

const footerMatcher = (re: RegExp) => (_: string, el: Element | null) =>
  el?.tagName === 'P' && re.test(el.textContent || '')

describe('VectorMemoryCard — Semantic Memory render cap', () => {
  beforeEach(() => vi.clearAllMocks())

  it('caps rendered rows and filters by key to reach entries past the cap', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)

    await waitFor(() => expect(screen.getByText('alpha.0')).toBeInTheDocument())

    // Exactly the first SEMANTIC_RENDER_CAP rows render; the next entry does not.
    expect(screen.getByText(`alpha.${SEMANTIC_RENDER_CAP - 1}`)).toBeInTheDocument()
    expect(screen.queryByText(`alpha.${SEMANTIC_RENDER_CAP}`)).not.toBeInTheDocument()
    expect(screen.getByText(footerMatcher(new RegExp(`Showing ${SEMANTIC_RENDER_CAP} of ${N}`)))).toBeInTheDocument()

    // A key past the cap is reachable via the filter, and others drop away.
    const input = screen.getByPlaceholderText('Filter by key or value…')
    await user.type(input, `alpha.${OBJECT_IDX}`)
    await waitFor(() => expect(screen.getByText(`alpha.${OBJECT_IDX}`)).toBeInTheDocument())
    expect(screen.queryByText('alpha.0')).not.toBeInTheDocument()
    expect(screen.getByText(footerMatcher(/Showing 1 of 1/))).toBeInTheDocument()

    // Clearing restores the capped view.
    await user.clear(input)
    await waitFor(() => expect(screen.getByText('alpha.0')).toBeInTheDocument())
    expect(screen.getByText(footerMatcher(new RegExp(`Showing ${SEMANTIC_RENDER_CAP} of ${N}`)))).toBeInTheDocument()
  })

  it('renders the semantic footer, filtered count included, as one catalog sentence', async () => {
    // en-XA brackets every catalog value, so a footer glued from fragments reads
    // `[Showing] 100 [of] 111 (filtered from 250)` with the last part in plain English.
    // The refine hint after it is its own sentence, so the match is anchored at the start only.
    await i18next.changeLanguage('en-XA')
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByText('alpha.0')).toBeInTheDocument())
    expect(screen.getByText(footerMatcher(new RegExp(`^\\[[^[\\]]*${SEMANTIC_RENDER_CAP}[^[\\]]*${N}[^[\\]]*\\]`)))).toBeInTheDocument()

    // alpha.1, alpha.10-19 and alpha.100-199: 111 of the N keys.
    await user.type(screen.getByPlaceholderText(i18nT('pages.overview.vectorMemoryCard.filter_by_key_or_value')), 'alpha.1')
    await waitFor(() => expect(screen.queryByText('alpha.0')).not.toBeInTheDocument())
    expect(screen.getByText(footerMatcher(new RegExp(`^\\[[^[\\]]*${SEMANTIC_RENDER_CAP}[^[\\]]*111[^[\\]]*${N}[^[\\]]*\\]`)))).toBeInTheDocument()
  })

  it('picks the footer plural form from the shown count', async () => {
    // Italian agrees the participle with the count: one shown row is "Visualizzato", not "Visualizzati".
    await i18next.changeLanguage('it')
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByText('alpha.0')).toBeInTheDocument())
    await user.type(screen.getByPlaceholderText(i18nT('pages.overview.vectorMemoryCard.filter_by_key_or_value')), `alpha.${N - 1}`)
    await waitFor(() => expect(screen.queryByText('alpha.0')).not.toBeInTheDocument())
    expect(screen.getByText(footerMatcher(new RegExp(`^Visualizzato 1 di 1 \\(filtrato da ${N}\\)$`)))).toBeInTheDocument()
  })

  it('filters by value content, including object values past the cap', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByText('alpha.0')).toBeInTheDocument())

    // VALUE_MARKER exists only inside one entry's object value (no key contains it),
    // and that entry is past the cap — so a value-substring match is the only way in.
    const input = screen.getByPlaceholderText('Filter by key or value…')
    await user.type(input, VALUE_MARKER)
    await waitFor(() => expect(screen.getByText(`alpha.${OBJECT_IDX}`)).toBeInTheDocument())
    expect(screen.getByText(footerMatcher(/Showing 1 of 1/))).toBeInTheDocument()
  })

  it('shows a no-match state and hides the footer when nothing matches', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByText('alpha.0')).toBeInTheDocument())

    const input = screen.getByPlaceholderText('Filter by key or value…')
    await user.type(input, 'no-such-entry-xyzzy')
    await waitFor(() => expect(screen.getByText('No matching entries')).toBeInTheDocument())
    expect(screen.queryByText(footerMatcher(/Showing/))).not.toBeInTheDocument()
  })
})


it('opens diagnostics with a bounded audit page without downloading the semantic database', async () => {
  vi.clearAllMocks()
  renderWithProviders(<VectorMemoryCard diagnosticsOnly />)
  await waitFor(() => expect(api.vectorEvents).toHaveBeenCalledWith(50, 0))
  expect(api.vectorSemantic).not.toHaveBeenCalled()
  expect(screen.queryByPlaceholderText('Filter by key or value…')).not.toBeInTheDocument()
})
