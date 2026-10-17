/**
 * A metrics fetch that has never produced a frame refetches through react-query's
 * `pending` state, which clears `isError` while the request is in flight. The
 * readout must keep reporting the failure through that window, or the failure
 * notice and the desktop top bar's collapse level blink on every poll.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const system = vi.fn()
vi.mock('../api/client', () => ({ api: { system: () => system() } }))

import { useMetricsReadout } from '../shell/topbar/metricsReadout'

const FRAME = { mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }

afterEach(() => {
  system.mockReset()
  localStorage.clear()
})

describe('useMetricsReadout: a failing fetch', () => {
  it('keeps reporting the failure while a refetch is in flight, and clears it once a frame lands', async () => {
    localStorage.setItem('mc-topbar-metrics', '1')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    system.mockRejectedValue(new Error('probe unreachable'))
    const { result } = renderHook(() => useMetricsReadout(false, false, 0), { wrapper })
    await waitFor(() => expect(result.current.sysMetricsError).toBe(true))

    // The poll fires again; hold the request open so the query sits in `pending`.
    let answer: (v: typeof FRAME) => void = () => {}
    system.mockImplementation(() => new Promise(res => { answer = res }))
    act(() => { void client.refetchQueries({ queryKey: ['system-metrics'] }) })
    await waitFor(() => expect(client.getQueryState(['system-metrics'])?.fetchStatus).toBe('fetching'))
    expect(client.getQueryState(['system-metrics'])?.status).toBe('pending')
    expect(result.current.sysMetricsError).toBe(true)

    await act(async () => { answer(FRAME) })
    await waitFor(() => expect(result.current.sysMetrics).toBeTruthy())
    expect(result.current.sysMetricsError).toBe(false)
  })
})
