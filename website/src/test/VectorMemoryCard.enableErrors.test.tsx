import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { i18nT } from '../i18n/t'

// POST /api/memory/enable-embeddings answers some refusals with a machine
// `code` beside English `error` prose. The card turned the rejection into text
// with `extractError`, which returns the ApiError message -- that prose -- so
// every language saw English. Each coded refusal now reads from the catalog.

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(),
    vectorEmbeddingStatus: vi.fn(),
    vectorSemantic: vi.fn(),
    vectorEpisodic: vi.fn(),
    vectorEvents: vi.fn(),
    vectorContextPreview: vi.fn(),
    vectorEnableEmbeddings: vi.fn(),
  },
}))

const IDLE_STATS = { semantic_active: 0, episodic_active: 0, embedded_count: 0, migrated: false }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.vectorStats).mockResolvedValue(IDLE_STATS as never)
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue(
    { provider: 'none', setup_step: 'error', setup_error: 'Download failed' } as never,
  )
  vi.mocked(api.vectorSemantic).mockResolvedValue({ entries: [] } as never)
  vi.mocked(api.vectorEpisodic).mockResolvedValue({ entries: [] } as never)
  vi.mocked(api.vectorEvents).mockResolvedValue({ events: [] } as never)
  vi.mocked(api.vectorContextPreview).mockResolvedValue(null as never)
})

/** The rejection the api client produces for a coded refusal. */
const refusal = (status: number, code: string, error: string) =>
  new ApiError(status, error, JSON.stringify({ error, code }))

describe('VectorMemoryCard enable-embeddings refusals', () => {
  it.each([
    [403, 'owner_only', 'owner only', 'pages.overview.vectorMemoryCard.enable_err_owner_only'],
    [500, 'config_unreadable', 'failed to read config file', 'pages.overview.vectorMemoryCard.enable_err_config_unreadable'],
    [500, 'embedding_dim_unreadable', 'could not read the active embedding width', 'pages.overview.vectorMemoryCard.enable_err_dim_unreadable'],
  ] as const)('shows %s/%s from the catalog, not the English prose', async (status, code, prose, key) => {
    vi.mocked(api.vectorEnableEmbeddings).mockRejectedValueOnce(refusal(status, code, prose))
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    // The standing setup error is an alert too; wait for the one the click raises.
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(i18nT(key)))
    expect(screen.getByRole('alert')).not.toHaveTextContent(prose)
  })

  it('keeps the prose for a refusal with no code, so nothing is swallowed', async () => {
    vi.mocked(api.vectorEnableEmbeddings).mockRejectedValueOnce(
      new ApiError(409, 'Setup already in progress: downloading', JSON.stringify({ error: 'Setup already in progress: downloading' })),
    )
    renderWithProviders(<VectorMemoryCard />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Setup already in progress: downloading'))
  })
})
