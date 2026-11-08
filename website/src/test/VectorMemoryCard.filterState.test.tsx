import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

// The audit event filter is one-of-N, so it is a named radio group whose
// chosen type is checked. Each episodic tag chip toggles one tag on or off,
// so it says whether it is pressed. Before, both showed state by color only.

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

async function renderActiveCard() {
  vi.mocked(api.vectorStats).mockResolvedValue({
    semantic_active: 1, episodic_active: 1, embedded_count: 1,
    migrated: true, has_legacy_memory: false,
  })
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({
    provider: 'llama_cpp', setup_step: 'done', model_available: true,
  })
  renderWithProviders(<VectorMemoryCard />)
  await screen.findByRole('radiogroup', { name: 'Memory view' })
}

describe('VectorMemoryCard filter state', () => {
  beforeEach(() => vi.clearAllMocks())

  it('exposes the audit event filter as a radio group with the chosen type checked', async () => {
    vi.mocked(api.vectorEvents).mockResolvedValue({
      events: [{ event_type: 'create', memory_type: 'semantic', memory_key: 'k', new_value: 'v', created_at: '2026-01-01 00:00:00' }],
    } as never)
    await renderActiveCard()
    fireEvent.click(screen.getByRole('radio', { name: 'Audit' }))
    const group = await screen.findByRole('radiogroup', { name: 'Event type' })
    const create = await within(group).findByRole('radio', { name: 'create' })
    const all = within(group).getByRole('radio', { name: 'All' })
    expect(all).toHaveAttribute('aria-checked', 'true')
    expect(create).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(create)
    await waitFor(() => expect(create).toHaveAttribute('aria-checked', 'true'))
    expect(all).toHaveAttribute('aria-checked', 'false')
  })

  it('marks the episodic tag chip that is filtering as pressed', async () => {
    vi.mocked(api.vectorEpisodic).mockResolvedValue({
      entries: [{ id: 'e1', text: 'went hiking', tags: '["trip"]', importance: 0.5, created_at: '2026-01-01 00:00:00' }],
    } as never)
    await renderActiveCard()
    fireEvent.click(screen.getByRole('radio', { name: 'Episodic' }))
    const chip = await screen.findByRole('button', { name: 'trip' })
    expect(chip).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(chip)
    await waitFor(() => expect(screen.getByRole('button', { name: 'trip' })).toHaveAttribute('aria-pressed', 'true'))
  })
})
