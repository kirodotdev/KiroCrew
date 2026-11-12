import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import MemoryRetiredCard from './MemoryRetiredCard'

const { api } = vi.hoisted(() => ({
  api: {
    memoryRetired: vi.fn(),
    memoryRestoreRetired: vi.fn(),
    // ThemeProvider in the shared wrapper reads these on mount.
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
  },
}))
vi.mock('../../api/client', () => ({ api }))

beforeEach(() => {
  vi.clearAllMocks()
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
})

describe('MemoryRetiredCard Retry memory access', () => {
  it('keeps the pressed Retry mounted and focused while the retry runs', async () => {
    // A 403 is a settled refusal, so the query does not retry on its own.
    api.memoryRetired.mockRejectedValueOnce(Object.assign(new Error('refused'), { status: 403 }))
    let settle: (value: { retired: never[] }) => void = () => {}
    api.memoryRetired.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
    const u = userEvent.setup()
    renderWithProviders(<MemoryRetiredCard store="crew-a" />)

    const retry = await screen.findByRole('button', { name: 'Retry memory access' })
    await u.click(retry)

    // The read is in flight: React Query has cleared `error`, yet the button
    // the user pressed is still the same node, still focused, and inert.
    expect(api.memoryRetired).toHaveBeenCalledTimes(2)
    expect(retry).toBeInTheDocument()
    expect(retry).toHaveFocus()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).not.toBeDisabled()

    // A second press while busy starts no second read.
    await u.click(retry)
    expect(api.memoryRetired).toHaveBeenCalledTimes(2)

    // Once the read lands, the failure surface and its Retry go away.
    settle({ retired: [] })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry memory access' })).not.toBeInTheDocument())
  })
})
