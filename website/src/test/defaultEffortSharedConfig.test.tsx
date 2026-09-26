/**
 * The composer's default-effort read answers from the shared ['kirocrewConfig']
 * query instead of issuing its own GET of the same body: with the sidebar's
 * observer of that key mounted in the same commit, boot sends ONE
 * `GET /api/config/kirocrew`, and the default effort still comes out of it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    agentDetail: vi.fn(),
    agentResolvedModel: vi.fn(),
  },
}))

import { api } from '../api/client'
import { AcpAdapter } from '../providers/adapters/acp'
import { useKirocrewConfigReader } from '../hooks/useKirocrewConfigReader'

const kirocrewConfig = api.kirocrewConfig as unknown as ReturnType<typeof vi.fn>

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children)
}

/** The two readers as the chat page mounts them: the sidebar's config
 *  observer and the composer's default-effort query. */
function useBootReaders() {
  const provider = new AcpAdapter()
  const cfg = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const readKirocrewConfig = useKirocrewConfigReader()
  const effort = useQuery({
    queryKey: ['default-effort', provider.id],
    queryFn: () => provider.resolveDefaultEffort(readKirocrewConfig),
  })
  return { cfg: cfg.data, effort: effort.data }
}

describe('default effort reads the shared kirocrewConfig query', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('sends one GET /api/config/kirocrew for the sidebar observer and the default effort', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { reasoning_effort: 'high' } })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const { result } = renderHook(() => useBootReaders(), { wrapper: wrapperFor(qc) })
    await waitFor(() => expect(result.current.effort).toBe('high'))
    expect(result.current.cfg).toEqual({ agent: { reasoning_effort: 'high' } })
    expect(kirocrewConfig).toHaveBeenCalledTimes(1)
  })

  it('answers from data already in the shared key without a request', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    qc.setQueryData(['kirocrewConfig'], { agent: { reasoning_effort: 'low' } })
    const { result } = renderHook(() => {
      const read = useKirocrewConfigReader()
      return useQuery({
        queryKey: ['default-effort', 'acp'],
        queryFn: () => new AcpAdapter().resolveDefaultEffort(read),
      }).data
    }, { wrapper: wrapperFor(qc) })
    await waitFor(() => expect(result.current).toBe('low'))
    expect(kirocrewConfig).not.toHaveBeenCalled()
  })

  it('reports "" when the shared read fails, as the adapter always has', async () => {
    kirocrewConfig.mockRejectedValue(new Error('503'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const { result } = renderHook(() => {
      const read = useKirocrewConfigReader()
      return useQuery({
        queryKey: ['default-effort', 'acp'],
        queryFn: () => new AcpAdapter().resolveDefaultEffort(read),
      })
    }, { wrapper: wrapperFor(qc) })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toBe('')
  })
})
