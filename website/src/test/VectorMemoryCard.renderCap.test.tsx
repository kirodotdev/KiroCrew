import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VectorMemoryCard, { SEMANTIC_RENDER_CAP } from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'

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
