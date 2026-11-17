import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import VectorMemoryCard from './VectorMemoryCard'

const { api } = vi.hoisted(() => ({
  api: {
    vectorStats: vi.fn(),
    vectorEmbeddingStatus: vi.fn(),
    vectorSemantic: vi.fn(),
    vectorEpisodic: vi.fn(),
    vectorEpisodicSearch: vi.fn(),
    vectorContextPreview: vi.fn(),
    vectorEvents: vi.fn(),
    // ThemeProvider in the shared wrapper reads these on mount.
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
  },
}))
vi.mock('../../api/client', () => ({ api, ApiError: class ApiError extends Error {} }))

beforeEach(() => {
  vi.clearAllMocks()
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
  // An active store with one semantic entry, so the card shows its browser.
  api.vectorStats.mockResolvedValue({ semantic_active: 1, episodic_active: 1 })
  api.vectorEmbeddingStatus.mockResolvedValue({ provider: 'local' })
  api.vectorSemantic.mockResolvedValue({ entries: [] })
  api.vectorContextPreview.mockResolvedValue({})
  api.vectorEvents.mockResolvedValue({ events: [] })
})

describe('VectorMemoryCard episodic Search keeps focus while busy', () => {
  it('keeps the pressed Search button focusable and inert while the read runs', async () => {
    api.vectorEpisodic.mockResolvedValue({ episodic: [] })
    const u = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)

    // Switch to the Episodic view, which mounts the Search button.
    await u.click(await screen.findByRole('radio', { name: 'Episodic' }))
    const searchBtn = await screen.findByRole('button', { name: 'Search' })

    // Hold the next episodic read open so the button is observed mid-flight.
    let settle: (value: { episodic: never[] }) => void = () => {}
    api.vectorEpisodic.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))

    searchBtn.focus()
    await u.click(searchBtn)

    // The read is in flight: a native `disabled` would have pulled the button
    // out of the tab order and dropped focus to the body. aria-disabled keeps
    // it focusable while marking it busy and inert.
    await waitFor(() => expect(searchBtn).toHaveAttribute('aria-busy', 'true'))
    expect(searchBtn).toHaveAttribute('aria-disabled', 'true')
    expect(searchBtn).not.toBeDisabled()
    expect(searchBtn).toHaveFocus()

    settle({ episodic: [] })
    await waitFor(() => expect(searchBtn).not.toHaveAttribute('aria-disabled'))
  })
})
