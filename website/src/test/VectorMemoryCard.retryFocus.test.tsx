import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

// A failed read's Retry rendered only while the query held an error, which
// React Query clears when a query with nothing cached refetches. So the pressed
// Retry was swapped for the loading card or unmounted with its card, and
// keyboard focus fell to the page for the whole retry (#32164).

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(),
    vectorEmbeddingStatus: vi.fn(),
    vectorSemantic: vi.fn(),
    vectorEpisodic: vi.fn(),
    vectorEvents: vi.fn(),
    vectorContextPreview: vi.fn(),
  },
}))

const IDLE_STATS = { semantic_active: 0, episodic_active: 0, embedded_count: 0, migrated: false }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.vectorStats).mockResolvedValue(IDLE_STATS as never)
  vi.mocked(api.vectorEmbeddingStatus).mockResolvedValue({ provider: 'none' } as never)
  vi.mocked(api.vectorSemantic).mockResolvedValue({ entries: [] } as never)
})

/** Press the only Retry, then check it is the same focused node, inert while
 *  `read` is in flight, and gone once that read lands. */
async function pressRetryWhileReading(read: ReturnType<typeof vi.fn>, settle: () => void) {
  const retry = await screen.findByRole('button', { name: 'Retry' })
  retry.focus()
  fireEvent.click(retry)
  await waitFor(() => expect(retry).toHaveAttribute('aria-disabled', 'true'))

  expect(retry).toBeInTheDocument()
  expect(retry).toHaveFocus()
  expect(read).toHaveBeenCalledTimes(2)
  fireEvent.click(retry)
  expect(read).toHaveBeenCalledTimes(2)

  settle()
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull())
}

describe('VectorMemoryCard read Retry keeps keyboard focus', () => {
  it('keeps the summary Retry while the stats read retries', async () => {
    let settle: (value: never) => void = () => {}
    vi.mocked(api.vectorStats)
      .mockRejectedValueOnce(new Error('stats read failed'))
      .mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
    renderWithProviders(<VectorMemoryCard />)

    await pressRetryWhileReading(vi.mocked(api.vectorStats), () => settle(IDLE_STATS as never))
  })

  it('keeps the semantic card and its Retry while the semantic read retries', async () => {
    let settle: (value: never) => void = () => {}
    vi.mocked(api.vectorSemantic)
      .mockRejectedValueOnce(new Error('semantic read failed'))
      .mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
    renderWithProviders(<VectorMemoryCard />)

    await pressRetryWhileReading(vi.mocked(api.vectorSemantic), () => settle({ entries: [] } as never))
  })

  it('keeps the audit card and its Retry while the audit read retries', async () => {
    // A 403 is a settled refusal, so the audit read does not retry on its own.
    let settle: (value: never) => void = () => {}
    vi.mocked(api.vectorEvents)
      .mockRejectedValueOnce(Object.assign(new Error('audit read refused'), { status: 403 }))
      .mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
    renderWithProviders(<VectorMemoryCard diagnosticsOnly />)

    await pressRetryWhileReading(vi.mocked(api.vectorEvents), () => settle({ events: [] } as never))
  })
})
