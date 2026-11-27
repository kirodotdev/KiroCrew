import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

/** A store that already holds memories stays browsable while the embedding model downloads. */
const { api } = vi.hoisted(() => ({ api: { vectorStats: vi.fn(), vectorEmbeddingStatus: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
const { useVectorMemoryState } = await import('../pages/overview/useVectorMemoryState')

const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>

beforeEach(() => { vi.clearAllMocks() })

describe('useVectorMemoryState', () => {
  it.each([
    [3, 'none', true],
    [0, 'local', false],
  ])('with %s rows and provider %s during a download is active=%s', async (rows, provider, active) => {
    api.vectorStats.mockResolvedValue({ semantic_active: rows, episodic_active: 0 })
    api.vectorEmbeddingStatus.mockResolvedValue({ provider, setup_step: 'downloading' })
    const { result } = renderHook(() => useVectorMemoryState(), { wrapper })
    await waitFor(() => expect(result.current.settled).toBe(true))
    expect(result.current.active).toBe(active)
  })
})
