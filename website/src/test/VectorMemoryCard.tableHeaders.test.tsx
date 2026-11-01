import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, screen, fireEvent, within } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { i18next } from '../i18n/all'

// The Semantic Memory and Audit Trail tables name their columns from the catalog.
// Under the `en-XA` pseudolocale every catalog value is bracketed and accented, so
// a header read from the catalog renders as its `en-XA` value and an English
// literal renders as plain English. Each case asserts both halves.

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(async () => ({})),
    vectorEmbeddingStatus: vi.fn(async () => ({})),
    vectorSemantic: vi.fn(async () => ({ entries: [] })),
    vectorSemanticWrite: vi.fn(async () => undefined),
    vectorSemanticDelete: vi.fn(async () => undefined),
    vectorEpisodic: vi.fn(async () => ({ entries: [] })),
    vectorEvents: vi.fn(async () => ({ events: [] })),
    vectorContextPreview: vi.fn(async () => null),
    vectorEnableEmbeddings: vi.fn(async () => ({ ok: true })),
  },
}))

const XA = 'en-XA'
const xa = (key: string) => i18next.getFixedT(XA)(key)
const card = (key: string) => xa(`pages.overview.vectorMemoryCard.${key}`)

async function renderActiveCard() {
  vi.mocked(api.vectorStats).mockResolvedValue({
    semantic_active: 5, episodic_active: 3, embedded_count: 3,
    migrated: true, has_legacy_memory: false,
  })
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({
    provider: 'llama_cpp', setup_step: 'done', model_available: true,
  })
  renderWithProviders(<VectorMemoryCard />)
  return screen.findByRole('group', { name: card('view_switch_label') })
}

const headers = () => screen.getAllByRole('columnheader').map(h => h.textContent)

beforeEach(async () => {
  vi.clearAllMocks()
  await i18next.changeLanguage(XA)
})

afterEach(async () => {
  // Unmount first: a language switch re-renders every mounted consumer.
  cleanup()
  await i18next.changeLanguage('en')
})

describe('VectorMemoryCard table headers under the pseudolocale', () => {
  it('names the Semantic Memory columns from the catalog', async () => {
    await renderActiveCard()
    await screen.findByText(card('no_semantic_entries'))
    const expected = ['column_key', 'column_value', 'column_confidence', 'column_source'].map(card)
    for (const label of expected) expect(label).toMatch(/^\[/)
    expect(headers()).toEqual([...expected, ''])
    for (const english of ['Key', 'Value', 'Confidence', 'Source']) expect(headers()).not.toContain(english)
  })

  it('names the Audit Trail columns from the catalog', async () => {
    const group = await renderActiveCard()
    fireEvent.click(within(group).getByRole('button', { name: card('view_audit') }))
    await screen.findByText(card('no_events'))
    const expected = [
      card('column_event'),
      card('column_key_type'),
      card('column_details'),
      xa('pages.overview.memoryTab.when'),
    ]
    for (const label of expected) expect(label).toMatch(/^\[/)
    expect(headers()).toEqual(expected)
    for (const english of ['Event', 'Key/Type', 'Details', 'When']) expect(headers()).not.toContain(english)
  })
})
