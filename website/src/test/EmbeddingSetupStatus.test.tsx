import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import EmbeddingSetupStatus from '../pages/overview/EmbeddingSetupStatus'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { i18nT } from '../i18n/t'

// The embedding setup's state inside the Embedding Model card. POST
// /api/memory/enable-embeddings answers some refusals with a machine `code`
// beside English `error` prose; each coded refusal reads from the catalog.

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(),
    vectorEmbeddingStatus: vi.fn(),
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
})

/** The rejection the api client produces for a coded refusal. */
const refusal = (status: number, code: string, error: string) =>
  new ApiError(status, error, JSON.stringify({ error, code }))

describe('embedding setup refusals', () => {
  it.each([
    [403, 'owner_only', 'owner only', 'pages.overview.vectorMemoryCard.enable_err_owner_only'],
    [500, 'config_unreadable', 'failed to read config file', 'pages.overview.vectorMemoryCard.enable_err_config_unreadable'],
    [500, 'embedding_dim_unreadable', 'could not read the active embedding width', 'pages.overview.vectorMemoryCard.enable_err_dim_unreadable'],
  ] as const)('shows %s/%s from the catalog, not the English prose', async (status, code, prose, key) => {
    vi.mocked(api.vectorEnableEmbeddings).mockRejectedValueOnce(refusal(status, code, prose))
    renderWithProviders(<EmbeddingSetupStatus />)
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
    renderWithProviders(<EmbeddingSetupStatus />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Setup already in progress: downloading'))
  })
})

describe('embedding setup state', () => {
  it('shows a failed download with Retry, and Retry starts the setup again', async () => {
    vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({ provider: 'none', setup_step: 'idle', model_available: false, setup_error: 'Download failed' } as never)
    vi.mocked(api.vectorEnableEmbeddings).mockResolvedValue({} as never)
    renderWithProviders(<EmbeddingSetupStatus />)
    await waitFor(() => expect(screen.getByText(/Download failed/i)).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: /Retry/i }))
    await waitFor(() => expect(api.vectorEnableEmbeddings).toHaveBeenCalledTimes(1))
  })

  it('shows the download with its megabytes and percent while it runs', async () => {
    vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({ provider: 'none', setup_step: 'downloading', download_step: 'downloading', bytes_downloaded: 305e6, bytes_total: 610e6 } as never)
    renderWithProviders(<EmbeddingSetupStatus />)
    const progress = await screen.findByTestId('embedding-setup-progress')
    expect(progress).toHaveTextContent(i18nT('pages.overview.vectorMemoryCard.downloading_embedding_model', { done: '305', total: '610', pct: 50 }))
  })

  it('shows nothing once vector memory runs', async () => {
    vi.mocked(api.vectorStats).mockResolvedValue({ ...IDLE_STATS, semantic_active: 3 } as never)
    vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({ provider: 'ollama', setup_step: 'done' } as never)
    renderWithProviders(<EmbeddingSetupStatus />)
    await waitFor(() => expect(api.vectorEmbeddingStatus).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByTestId('embedding-setup-status')).toBeEmptyDOMElement())
  })
})

it.each([[undefined, 1], ['legacy_embedding_vectors', 0]] as const)('a missing path with warning %s shows %s pointer(s)', async (code, pointers) => {
  vi.mocked(api.vectorStats).mockResolvedValue({ ...IDLE_STATS, semantic_active: 2 } as never)
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({ provider: 'local', setup_step: 'error', setup_error: 'x', setup_error_code: 'model_path_not_found', setup_warning_code: code } as never)
  renderWithProviders(<EmbeddingSetupStatus />)
  if (code) await screen.findByTestId('embedding-setup-warning')
  await waitFor(() => expect(screen.queryAllByTestId('embedding-setup-error-pointer')).toHaveLength(pointers))
})
