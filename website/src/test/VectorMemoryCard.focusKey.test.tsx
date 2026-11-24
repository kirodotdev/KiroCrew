/**
 * `focusKey` (the chat's "Review lessons" link, `?lesson=`) opens one row: it is
 * read on its own, so a lesson past the render cap is still found, then pinned
 * first, highlighted and scrolled to. Changing the filter lets it go.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api/client'
import VectorMemoryCard, { SEMANTIC_RENDER_CAP } from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'

const FOCUS = 'lesson.ffffffffffff'
const lesson = (key: string, rule: string) => ({ key, value_json: JSON.stringify({ rule, category: 'tool' }), confidence: 0.9, source: 'agent' })
const many = Array.from({ length: SEMANTIC_RENDER_CAP + 20 }, (_, i) => lesson(`lesson.${String(i).padStart(12, '0')}`, `rule ${i}`))

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(async () => ({ semantic_active: 1, episodic_active: 0, embedded_count: 0, migrated: true })),
    vectorEmbeddingStatus: vi.fn(async () => ({ provider: 'none', setup_step: 'idle' })),
    vectorSemantic: vi.fn(),
    vectorSemanticSearch: vi.fn(),
    vectorSemanticWrite: vi.fn(async () => undefined),
    vectorSemanticDelete: vi.fn(async () => undefined),
    vectorEpisodic: vi.fn(async () => ({ entries: [] })),
    vectorEvents: vi.fn(async () => ({ events: [] })),
    vectorContextPreview: vi.fn(async () => null),
  },
}))

let scrolled: Element[]
const original = Element.prototype.scrollIntoView
beforeEach(() => {
  vi.clearAllMocks()
  scrolled = []
  Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
  vi.mocked(api.vectorSemantic).mockResolvedValue({ entries: [...many, lesson(FOCUS, 'the saved one')] })
  // The search answers by substring, so it can return a neighbour too.
  vi.mocked(api.vectorSemanticSearch).mockResolvedValue({ entries: [lesson(`${FOCUS}0`, 'a neighbour'), lesson(FOCUS, 'the saved one')] })
})
afterEach(() => { Element.prototype.scrollIntoView = original })

const rows = () => screen.getAllByRole('row').slice(1)

describe('focusKey', () => {
  it('pins the saved row first, past the render cap, highlighted and scrolled to', async () => {
    renderWithProviders(<VectorMemoryCard initialSemanticFilter="lesson." focusKey={FOCUS} />)
    await waitFor(() => expect(within(rows()[0]).getByText(FOCUS)).toBeInTheDocument())
    expect(api.vectorSemanticSearch).toHaveBeenCalledWith(FOCUS)
    expect(rows()[0]).toHaveAttribute('data-semantic-focus', 'true')
    expect(rows()[0].className).toContain('bg-accent-subtle')
    expect(screen.getAllByText(FOCUS)).toHaveLength(1)
    expect(screen.queryByText(`${FOCUS}0`)).not.toBeInTheDocument()
    await waitFor(() => expect(scrolled).toEqual([rows()[0]]))
    expect(rows().filter(r => r.hasAttribute('data-semantic-focus'))).toHaveLength(1)
  })

  it('lets the pin go once the filter changes', async () => {
    renderWithProviders(<VectorMemoryCard initialSemanticFilter="lesson." focusKey={FOCUS} />)
    await waitFor(() => expect(within(rows()[0]).getByText(FOCUS)).toBeInTheDocument())
    fireEvent.change(screen.getByPlaceholderText('Filter by key or value…'), { target: { value: 'rule 1' } })
    await waitFor(() => expect(screen.queryByText(FOCUS)).not.toBeInTheDocument())
  })

  it('shows a focused lookup failure and retries without losing semantic drafts', async () => {
    vi.mocked(api.vectorSemanticSearch).mockRejectedValueOnce(new Error('Focused lesson read failed'))
    renderWithProviders(<VectorMemoryCard initialSemanticFilter="lesson." focusKey={FOCUS} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Focused lesson read failed')
    const keyDraft = screen.getByRole('combobox', { name: 'Key', exact: true })
    fireEvent.change(keyDraft, { target: { value: 'pref.draft' } })
    expect(screen.queryByText(FOCUS)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await waitFor(() => expect(rows()[0]).toHaveAttribute('data-semantic-focus', 'true'))
    expect(within(rows()[0]).getByText(FOCUS)).toBeInTheDocument()
    expect(api.vectorSemanticSearch).toHaveBeenCalledTimes(2)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(keyDraft).toHaveValue('pref.draft')
    await waitFor(() => expect(scrolled).toEqual([rows()[0]]))
  })

  it('reads nothing extra without a focus', async () => {
    renderWithProviders(<VectorMemoryCard initialSemanticFilter="lesson." />)
    await waitFor(() => expect(screen.getByText('lesson.000000000000')).toBeInTheDocument())
    expect(api.vectorSemanticSearch).not.toHaveBeenCalled()
    expect(document.querySelector('[data-semantic-focus]')).toBeNull()
  })
})
