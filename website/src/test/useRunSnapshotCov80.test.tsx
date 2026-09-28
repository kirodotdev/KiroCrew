/**
 * useRunSnapshot — the single-run snapshot fetch behind the chat workflow
 * progress bar and the ActivityViewer Workflows tab.
 *
 * What matters: the URL it builds (run id percent-encoded), that `enabled` and
 * a null run id both hold the fetch back, that a non-2xx becomes a readable
 * error string rather than a thrown render, and that `refresh()` re-fetches.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useRunSnapshot, type RunSnapshot } from '../apps/workflows/useRunSnapshot'

const snapshot = (over: Partial<RunSnapshot> = {}): RunSnapshot => ({
  run_id: 'wf_zzq_1',
  status: 'finished',
  events: [],
  ...over,
})

let fetchMock: ReturnType<typeof vi.fn>

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => {
  fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(snapshot()) }))
  vi.stubGlobal('fetch', fetchMock)
})
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })

describe('useRunSnapshot', () => {
  it('fetches the run snapshot with a percent-encoded id and same-origin credentials', async () => {
    const { result } = renderHook(() => useRunSnapshot('wf zzq/1', { enabled: true }), { wrapper })

    await waitFor(() => expect(result.current.snapshot).not.toBeNull())
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/workflows/runs/wf%20zzq%2F1',
      { credentials: 'same-origin' },
    )
    expect(result.current.snapshot?.run_id).toBe('wf_zzq_1')
    expect(result.current.error).toBeNull()
    expect(result.current.loading).toBe(false)
  })

  it('does not fetch while disabled', () => {
    const { result } = renderHook(() => useRunSnapshot('wf_zzq_1', { enabled: false }), { wrapper })
    expect(fetchMock).not.toHaveBeenCalled()
    expect(result.current.snapshot).toBeNull()
  })

  it('does not fetch without a run id', () => {
    renderHook(() => useRunSnapshot(null, { enabled: true }), { wrapper })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('turns a non-2xx into a readable error string and keeps snapshot null', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 503, json: () => Promise.resolve({}) })
    const { result } = renderHook(() => useRunSnapshot('wf_zzq_2', { enabled: true }), { wrapper })

    await waitFor(() => expect(result.current.error).toBe('GET /runs/wf_zzq_2 → 503'))
    expect(result.current.snapshot).toBeNull()
  })

  it('refresh() re-fetches the snapshot', async () => {
    const { result } = renderHook(() => useRunSnapshot('wf_zzq_3', { enabled: true }), { wrapper })
    await waitFor(() => expect(result.current.snapshot).not.toBeNull())
    expect(fetchMock).toHaveBeenCalledTimes(1)

    result.current.refresh()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  })

  it('polls on the given interval only while the run is still running', async () => {
    fetchMock.mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve(snapshot({ status: 'running' })),
    })
    const { result } = renderHook(
      () => useRunSnapshot('wf_zzq_4', { enabled: true, pollMs: 20 }),
      { wrapper },
    )
    await waitFor(() => expect(result.current.snapshot?.status).toBe('running'))
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1))

    // A finished snapshot stops the poll: the call count settles.
    fetchMock.mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve(snapshot({ status: 'finished' })),
    })
    await waitFor(() => expect(result.current.snapshot?.status).toBe('finished'))
    const settled = fetchMock.mock.calls.length
    await new Promise(r => setTimeout(r, 60))
    expect(fetchMock.mock.calls.length).toBe(settled)
  })
})
