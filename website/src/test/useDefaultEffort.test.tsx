/**
 * The composer's default effort is read from the shared ['kirocrewConfig']
 * query. Boot sends ONE `GET /api/config/kirocrew` for it and the sidebar's
 * observer of the same key, and a Settings save, which invalidates that key,
 * shows in the composer without a reload.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'

vi.mock('../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
  },
}))

import { api } from '../api/client'
import { useDefaultEffort } from '../hooks/useDefaultEffort'

const kirocrewConfig = api.kirocrewConfig as unknown as ReturnType<typeof vi.fn>

function wrapperFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children)
}

const client = () => new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })

describe('useDefaultEffort', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('shares one GET with the sidebar observer of the same key', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { reasoning_effort: 'high' } })
    const qc = client()
    const { result } = renderHook(() => {
      const cfg = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
      return { cfg: cfg.data, effort: useDefaultEffort(true) }
    }, { wrapper: wrapperFor(qc) })
    await waitFor(() => expect(result.current.effort).toBe('high'))
    expect(result.current.cfg).toEqual({ agent: { reasoning_effort: 'high' } })
    expect(kirocrewConfig).toHaveBeenCalledTimes(1)
  })

  it('follows a Settings save that invalidates the shared key', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { reasoning_effort: 'low' } })
    const qc = client()
    const { result } = renderHook(() => useDefaultEffort(true), { wrapper: wrapperFor(qc) })
    await waitFor(() => expect(result.current).toBe('low'))

    kirocrewConfig.mockResolvedValue({ agent: { reasoning_effort: 'max' } })
    await act(() => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }))

    await waitFor(() => expect(result.current).toBe('max'))
  })

  it('answers "" when nothing is set, when the read fails, and when disabled', async () => {
    kirocrewConfig.mockResolvedValue({ agent: {} })
    const unset = renderHook(() => useDefaultEffort(true), { wrapper: wrapperFor(client()) })
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled())
    expect(unset.result.current).toBe('')

    kirocrewConfig.mockRejectedValue(new Error('503'))
    const failed = renderHook(() => useDefaultEffort(true), { wrapper: wrapperFor(client()) })
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalledTimes(2))
    expect(failed.result.current).toBe('')

    const off = renderHook(() => useDefaultEffort(false), { wrapper: wrapperFor(client()) })
    expect(off.result.current).toBe('')
    expect(kirocrewConfig).toHaveBeenCalledTimes(2)
  })
})
