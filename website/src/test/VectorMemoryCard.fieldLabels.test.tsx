import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// Every text field on the Vector Memory card has an accessible name. A
// placeholder is not one: it vanishes once text is typed, and assistive tech
// may skip it.

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

const zhCard = zh.pages.overview.vectorMemoryCard

async function renderActiveCard() {
  vi.mocked(api.vectorStats).mockResolvedValue({
    semantic_active: 5, episodic_active: 3, embedded_count: 3,
    migrated: true, has_legacy_memory: false,
  })
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({
    provider: 'llama_cpp', setup_step: 'done', model_available: true,
  })
  renderWithProviders(<VectorMemoryCard />)
  await screen.findByRole('radiogroup', { name: i18next.t('pages.overview.vectorMemoryCard.view_switch_label') })
}

describe('VectorMemoryCard field names', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('names the semantic key, value and filter fields', async () => {
    await renderActiveCard()
    expect(await screen.findByRole('combobox', { name: 'Key' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Value' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Filter entries' })).toBeInTheDocument()
  })

  it('names the episodic search field', async () => {
    await renderActiveCard()
    fireEvent.click(screen.getByRole('radio', { name: 'Episodic' }))
    expect(await screen.findByRole('textbox', { name: 'Search episodic memories' })).toBeInTheDocument()
  })

  it('names the inspector query field', async () => {
    await renderActiveCard()
    fireEvent.click(screen.getByRole('radio', { name: 'Inspector' }))
    expect(await screen.findByRole('textbox', { name: 'Test query' })).toBeInTheDocument()
  })

  it('takes the names from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    await renderActiveCard()
    expect(await screen.findByRole('textbox', { name: zhCard.filter_entries_label })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: zhCard.column_value })).toBeInTheDocument()
  })
})
