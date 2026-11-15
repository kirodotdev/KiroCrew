/**
 * The Semantic, Episodic and Audit tables show stored text exactly as stored.
 * React escapes text children itself, so a pre-escaped string would show its
 * entities (`&gt;&gt;`, `&amp;`) literally.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

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

const SEM_KEY = 'pref.a&b>c'
const SEM_VALUE = 'echo x >> log & wait'
const EPI_TEXT = 'ran tail -f a.log & grep x >> b.log'
const AUDIT_KEY = 'pref.<audit>&key'
const AUDIT_VALUE = 'old >> new & more'

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.vectorStats).mockResolvedValue({ semantic_active: 1, episodic_active: 1, embedded_count: 1, migrated: true } as never)
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({ provider: 'llama_cpp', setup_step: 'done', model_available: true } as never)
  vi.mocked(api.vectorSemantic).mockResolvedValue({ entries: [{ key: SEM_KEY, value_json: JSON.stringify(SEM_VALUE), confidence: 1 }] } as never)
  vi.mocked(api.vectorEpisodic).mockResolvedValue({ entries: [{ id: 'e1', text: EPI_TEXT, tags: [], importance: 0.9 }] } as never)
  vi.mocked(api.vectorEpisodicSearch).mockResolvedValue({ results: [] } as never)
  vi.mocked(api.vectorEvents).mockResolvedValue({ events: [{ event_type: 'memory_write', memory_key: AUDIT_KEY, new_value: AUDIT_VALUE, created_at: '2026-08-10 09:00:00' }] } as never)
  vi.mocked(api.vectorContextPreview).mockResolvedValue(null as never)
})

const exact = (text: string) => {
  const el = screen.getByText(text)
  expect(el.textContent).toBe(text)
}

describe('VectorMemoryCard — raw stored text', () => {
  it('shows semantic, episodic and audit text with >> and & exactly as stored', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    await screen.findByText(SEM_VALUE)
    exact(SEM_KEY)
    exact(SEM_VALUE)

    await user.click(screen.getByRole('radio', { name: /^Episodic$/ }))
    await waitFor(() => expect(screen.getByText(EPI_TEXT)).toBeInTheDocument())
    exact(EPI_TEXT)

    await user.click(screen.getByRole('radio', { name: /^Audit$/ }))
    await waitFor(() => expect(screen.getByText(AUDIT_KEY)).toBeInTheDocument())
    exact(AUDIT_KEY)
    exact(AUDIT_VALUE)
    expect(document.body.textContent).not.toMatch(/&(gt|lt|amp);/)
  })
})
