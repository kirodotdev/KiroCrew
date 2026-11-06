import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { i18nT } from '../i18n/t'

// PUT /api/memory/semantic answers its write-gate refusals with a machine
// `code` beside English `error` prose. The card showed that prose through
// `extractError`, so every language saw English. The uncoded fallback is pinned
// by VectorMemoryCardCoverage.test.tsx.

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(),
    vectorEmbeddingStatus: vi.fn(),
    vectorSemantic: vi.fn(),
    vectorSemanticWrite: vi.fn(),
    vectorEpisodic: vi.fn(),
    vectorEvents: vi.fn(),
    vectorContextPreview: vi.fn(),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.vectorStats).mockResolvedValue({ semantic_active: 1, episodic_active: 0, embedded_count: 0, migrated: true } as never)
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({ provider: 'none', setup_step: 'idle' } as never)
  vi.mocked(api.vectorSemantic).mockResolvedValue(
    { entries: [{ key: 'pref.editor', value_json: '"vim"', confidence: 1.0, source: 'user_explicit' }] } as never,
  )
  vi.mocked(api.vectorEpisodic).mockResolvedValue({ entries: [] } as never)
  vi.mocked(api.vectorEvents).mockResolvedValue({ events: [] } as never)
  vi.mocked(api.vectorContextPreview).mockResolvedValue(null as never)
})

/** The rejection the api client produces for a coded refusal. */
const refusal = (status: number, code: string, error: string) =>
  new ApiError(status, error, JSON.stringify({ error, code }))

const OWNER_ONLY = refusal(403, 'owner_only', 'owner authorization required')
const UNAVAILABLE_PROSE = "Memory recovery failed for 'default': disk full Inspect or cancel this store's pending restore, then restart the gateway."

describe('VectorMemoryCard semantic write refusals', () => {
  it.each([
    ['owner_only', OWNER_ONLY, 'pages.overview.vectorMemoryCard.semantic_err_owner_only'],
    ['store_unavailable', refusal(503, 'store_unavailable', UNAVAILABLE_PROSE), 'pages.overview.vectorMemoryCard.semantic_err_store_unavailable'],
  ] as const)('shows a coded %s refusal of a new entry from the catalog', async (_code, rejection, key) => {
    const message = i18nT(key, { error: UNAVAILABLE_PROSE })
    vi.mocked(api.vectorSemanticWrite).mockRejectedValueOnce(rejection)
    renderWithProviders(<VectorMemoryCard />)
    fireEvent.change(await screen.findByRole('combobox', { name: 'Key' }), { target: { value: 'pref.shell' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'Value' }), { target: { value: 'zsh' } })

    fireEvent.click(screen.getByRole('button', { name: 'Set' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(message))
  })

  it('shows a coded refusal of an inline edit from the catalog', async () => {
    vi.mocked(api.vectorSemanticWrite).mockRejectedValueOnce(OWNER_ONLY)
    renderWithProviders(<VectorMemoryCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit value of “pref.editor”' }))

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(i18nT('pages.overview.vectorMemoryCard.semantic_err_owner_only')))
    expect(screen.getByRole('alert')).not.toHaveTextContent('owner authorization required')
  })
})
