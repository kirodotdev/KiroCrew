/**
 * The ordering seam in useAvailableModels.
 *
 * The property that makes the seam correct is WHERE the order is applied: as a
 * `useMemo` over the cached list, not inside the model `queryFn`. So the test
 * that matters is that a change to the shared `['kirocrewConfig']` cache
 * reorders the list on the next render WITHOUT the model list being refetched —
 * exactly what folding the order into the fetcher would have broken.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import React from 'react'
import { renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Mock fns must be created via vi.hoisted so the hoisted vi.mock factories below
// can reference them without a temporal-dead-zone error.
const { fetchAvailableModels, kirocrewConfig } = vi.hoisted(() => ({
  fetchAvailableModels: vi.fn(),
  kirocrewConfig: vi.fn(),
}))

// Provider is the model-list source; count its calls to prove no refetch.
vi.mock('../providers', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../providers')>()),
  useProvider: () => ({ id: 'acp', fetchAvailableModels }),
}))

// The shared config read the order comes from.
vi.mock('../api/client', () => ({ api: { kirocrewConfig } }))

import { useAvailableModels } from '../hooks/useAvailableModels'

/** Fresh hook render with a QueryClient the test can write the config cache into. */
function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { qc, ...renderHook(() => useAvailableModels(), { wrapper }) }
}

beforeEach(() => {
  fetchAvailableModels.mockReset()
  kirocrewConfig.mockReset()
})

describe('useAvailableModels — order seam', () => {
  it('leaves the backend order (auto first) when no order is saved', async () => {
    fetchAvailableModels.mockResolvedValue([
      { name: 'alpha', description: '' },
      { name: 'bravo', description: '' },
    ])
    kirocrewConfig.mockResolvedValue({}) // no agent.model_order

    const { result } = setup()
    await waitFor(() => expect(result.current.map(m => m.name)).toEqual(['auto', 'alpha', 'bravo']))
  })

  it('applies the saved order, then reorders on a config change with NO model refetch', async () => {
    fetchAvailableModels.mockResolvedValue([
      { name: 'alpha', description: '' },
      { name: 'bravo', description: '' },
      { name: 'charlie', description: '' },
    ])
    kirocrewConfig.mockResolvedValue({ agent: { model_order: ['charlie'] } })

    const { qc, result } = setup()

    await waitFor(() =>
      expect(result.current.map(m => m.name)).toEqual(['auto', 'charlie', 'alpha', 'bravo']),
    )
    expect(fetchAvailableModels).toHaveBeenCalledTimes(1)

    // A settings save lands in the SHARED config cache every panel reads.
    kirocrewConfig.mockResolvedValue({ agent: { model_order: ['bravo', 'alpha'] } })
    act(() => {
      qc.setQueryData(['kirocrewConfig'], { agent: { model_order: ['bravo', 'alpha'] } })
    })

    await waitFor(() =>
      expect(result.current.map(m => m.name)).toEqual(['auto', 'bravo', 'alpha', 'charlie']),
    )
    // The reorder was a re-map over the cached list — the model list, which is
    // what spawns kiro-cli, was never re-fetched.
    expect(fetchAvailableModels).toHaveBeenCalledTimes(1)
  })
})
