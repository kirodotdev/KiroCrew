import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * "X of Y embedded" counts every fact, lesson and experience with a vector,
 * the rows search by meaning reads, and its help tip says what that means.
 */
const { api } = vi.hoisted(() => ({ api: { vectorEmbeddingStatus: vi.fn(), vectorStats: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
const { MemoryEmbeddedLine } = await import('../pages/overview/memoryPage/MemoryEmbeddedLine')

beforeEach(() => {
  vi.clearAllMocks()
  api.vectorEmbeddingStatus.mockResolvedValue({ provider: 'local' })
})

describe('the embedded count', () => {
  it('adds the facts and lessons with a vector to the experiences, and explains the rest', async () => {
    api.vectorStats.mockResolvedValue({ semantic_active: 6, episodic_active: 3, embedded_count: 2, semantic_embedded: 4 })
    renderWithProviders(<MemoryEmbeddedLine store="member-reviewer" />)
    expect(await screen.findByText('6 of 9 embedded')).toBeVisible()
    const help = screen.getByRole('button', { name: 'More information' })
    expect(help).toHaveAttribute('title', '6 of 9 memories can be found by meaning. The rest are found by words only until they’re filled in, which happens in the background.')
  })

  it('reads experiences only from a gateway that does not report facts and lessons', async () => {
    api.vectorStats.mockResolvedValue({ semantic_active: 6, episodic_active: 3, embedded_count: 2 })
    renderWithProviders(<MemoryEmbeddedLine store="" />)
    expect(await screen.findByText('2 of 9 embedded')).toBeVisible()
  })
})
