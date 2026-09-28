/**
 * `usePackDetail` -- the pack read as a React Query entry.
 *
 * What the query configuration buys: one request however many avatars mount,
 * the shared client's retry (one, here with a short delay) rather than a
 * per-hook override, an error state that is refetched rather than kept, and
 * invalidation that reaches a MOUNTED subscriber (a roster row wearing a pack
 * the Library just re-imported).
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const detail = vi.fn()
vi.mock('../api/client', () => ({
  api: { appearances: { detail: (id: string) => detail(id) } },
}))

import { retryPolicy } from '../api/queryClient'
import { useInvalidatePackDetail, usePackDetail } from '../hooks/usePackDetail'

const svgPack = (content: string) => ({ animations: { idle: { content, format: 'svg' } } })

let qc: QueryClient
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

beforeEach(() => {
  // The app's retry policy with its backoff shortened: the hook must inherit
  // it, not pin its own count, so that is what the client carries.
  qc = new QueryClient({ defaultOptions: { queries: { retry: retryPolicy, retryDelay: 20 } } })
  detail.mockReset()
})

describe('usePackDetail', () => {
  it('reads a pack once however many components mount it', async () => {
    detail.mockResolvedValue(svgPack('<svg/>'))
    const a = renderHook(() => usePackDetail('aurora'), { wrapper })
    const b = renderHook(() => usePackDetail('aurora'), { wrapper })
    await waitFor(() => expect(a.result.current.data).toBeTruthy())
    await waitFor(() => expect(b.result.current.data).toBeTruthy())
    expect(detail).toHaveBeenCalledTimes(1)
    // Still one after the answer is in: `staleTime: Infinity` means a later
    // mount is served from cache.
    renderHook(() => usePackDetail('aurora'), { wrapper })
    expect(detail).toHaveBeenCalledTimes(1)
  })

  it('normalizes the answer through packDetailFrom', async () => {
    detail.mockResolvedValue({ animations: { idle: { content: 'x', format: 'webp' } }, junk: 1 })
    const { result } = renderHook(() => usePackDetail('aurora'), { wrapper })
    await waitFor(() => expect(result.current.data).toBeTruthy())
    expect(result.current.data).toEqual({ animations: { idle: { content: '', format: 'svg' } }, sprite: undefined })
  })

  it('retries per the shared policy (once), then reports the error', async () => {
    detail.mockRejectedValue(new Error('pack_not_found'))
    const { result } = renderHook(() => usePackDetail('gone'), { wrapper })
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(detail).toHaveBeenCalledTimes(2)
  })

  it('recovers on the retry when the first read was a blip', async () => {
    detail.mockRejectedValueOnce(new Error('ECONNREFUSED')).mockResolvedValue(svgPack('<svg/>'))
    const { result } = renderHook(() => usePackDetail('aurora'), { wrapper })
    await waitFor(() => expect(result.current.data).toBeTruthy())
    expect(result.current.isError).toBe(false)
  })

  it('does not keep a failed read: a later mount asks again', async () => {
    detail.mockRejectedValue(new Error('ECONNREFUSED'))
    const first = renderHook(() => usePackDetail('aurora'), { wrapper })
    await waitFor(() => expect(first.result.current.isError).toBe(true))
    first.unmount()

    detail.mockReset()
    detail.mockResolvedValue(svgPack('<svg/>'))
    const second = renderHook(() => usePackDetail('aurora'), { wrapper })
    await waitFor(() => expect(second.result.current.data).toBeTruthy())
    expect(detail).toHaveBeenCalled()
  })
})

describe('useInvalidatePackDetail', () => {
  it('re-reads for a subscriber that is still mounted', async () => {
    // The defect the module-level cache could not fix: a roster row wearing the
    // pack must redraw after the Library re-imports it, without remounting.
    // Lottie, whose bytes the detail keeps: the two versions must be told apart.
    const clip = (v: number) => ({ animations: { idle: { content: `{"v":${v}}`, format: 'lottie' } } })
    detail.mockResolvedValueOnce(clip(1)).mockResolvedValueOnce(clip(2))
    const reader = renderHook(() => usePackDetail('aurora'), { wrapper })
    await waitFor(() => expect(reader.result.current.data?.animations.idle.content).toBe('{"v":1}'))

    const invalidator = renderHook(() => useInvalidatePackDetail(), { wrapper })
    await act(async () => {
      invalidator.result.current('aurora')
    })

    await waitFor(() => expect(reader.result.current.data?.animations.idle.content).toBe('{"v":2}'))
    expect(detail).toHaveBeenCalledTimes(2)
  })

  it('invalidates only the named pack', async () => {
    detail.mockImplementation(async (id: string) => svgPack(`<svg id="${id}"/>`))
    const a = renderHook(() => usePackDetail('aurora'), { wrapper })
    const n = renderHook(() => usePackDetail('nebula'), { wrapper })
    await waitFor(() => expect(a.result.current.data).toBeTruthy())
    await waitFor(() => expect(n.result.current.data).toBeTruthy())
    expect(detail).toHaveBeenCalledTimes(2)

    const invalidator = renderHook(() => useInvalidatePackDetail(), { wrapper })
    await act(async () => {
      invalidator.result.current('aurora')
    })
    await waitFor(() => expect(detail).toHaveBeenCalledTimes(3))
    expect(detail).toHaveBeenLastCalledWith('aurora')
  })
})
