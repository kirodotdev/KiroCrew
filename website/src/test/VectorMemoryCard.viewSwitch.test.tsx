import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// The card's view switch (Semantic / Episodic / Audit / Inspector) names each
// button from the catalog, and marks the shown view with aria-pressed so a
// screen reader can tell which one is on.

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
  const group = await screen.findByRole('group', { name: i18next.t('pages.overview.vectorMemoryCard.view_switch_label') })
  return group
}

describe('VectorMemoryCard view switch', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('marks only the shown view as pressed and follows a click', async () => {
    const group = await renderActiveCard()
    const semantic = within(group).getByRole('button', { name: 'Semantic' })
    const audit = within(group).getByRole('button', { name: 'Audit' })
    expect(semantic).toHaveAttribute('aria-pressed', 'true')
    expect(audit).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(audit)
    await waitFor(() => expect(audit).toHaveAttribute('aria-pressed', 'true'))
    expect(semantic).toHaveAttribute('aria-pressed', 'false')
    expect(within(group).getAllByRole('button').filter(b => b.getAttribute('aria-pressed') === 'true')).toHaveLength(1)
  })

  it('names every view button from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    const group = await renderActiveCard()
    const names = within(group).getAllByRole('button').map(b => b.textContent?.trim())
    expect(names).toEqual([zhCard.view_semantic, zhCard.view_episodic, zhCard.view_audit, zhCard.inspector])
    expect(names).not.toContain('Semantic')
    expect(names).not.toContain('Episodic')
    expect(names).not.toContain('Audit')
  })
})
