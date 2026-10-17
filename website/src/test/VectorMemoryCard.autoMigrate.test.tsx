import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

// Migration is automatic + background at gateway boot, so the Vector Memory
// card exposes no manual "Migrate" / "Migrate from Markdown" button and no
// proactive "Start Embedding Engine" button. Only a Retry affordance remains,
// and only in the genuine download-error state.

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

describe('VectorMemoryCard — automatic migration (no manual buttons)', () => {
  beforeEach(() => vi.clearAllMocks())

  it('has legacy memory but is NOT migrated → shows no Migrate button', async () => {
    // The exact state a manual "Migrate from Markdown" button keyed off.
    vi.mocked(api.vectorStats).mockResolvedValue({
      semantic_active: 0, episodic_active: 0, embedded_count: 0,
      migrated: false, has_legacy_memory: true,
    })
    vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({
      provider: 'none', setup_step: 'idle', model_available: false,
    })
    renderWithProviders(<VectorMemoryCard />)

    await waitFor(() =>
      expect(screen.getByText(/downloads automatically in the background/i)).toBeInTheDocument()
    )
    expect(screen.queryByText(/Migrate from Markdown/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Migrate$/i })).not.toBeInTheDocument()
    // No proactive start button either.
    expect(screen.queryByText(/Start Embedding Engine/i)).not.toBeInTheDocument()
  })

  it('active + not-migrated with legacy → no Migrate button in the active view', async () => {
    vi.mocked(api.vectorStats).mockResolvedValue({
      semantic_active: 5, episodic_active: 3, embedded_count: 3,
      migrated: false, has_legacy_memory: true,
    })
    vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({
      provider: 'llama_cpp', setup_step: 'done', model_available: true,
    })
    renderWithProviders(<VectorMemoryCard />)

    // Active view renders (the Inspector tab button is unique to it).
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Inspector/i })).toBeInTheDocument()
    )
    expect(screen.queryByText(/Migrate/i)).not.toBeInTheDocument()
  })

  it('download error state → shows a Retry button', async () => {
    vi.mocked(api.vectorStats).mockResolvedValue({
      semantic_active: 0, episodic_active: 0, embedded_count: 0,
      migrated: true, has_legacy_memory: false,
    })
    vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({
      provider: 'none', setup_step: 'idle', model_available: false,
      setup_error: 'Download failed',
    })
    renderWithProviders(<VectorMemoryCard />)

    await waitFor(() => expect(screen.getByText(/Download failed/i)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
  })
})
