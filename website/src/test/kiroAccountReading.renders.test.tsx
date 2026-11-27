import { afterEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type Query } from '@tanstack/react-query'
import { KIRO_USAGE_WARMING_POLL_MS } from '../api/kiroUsage'
import { useKiroAccountReading } from '../hooks/useKiroAccountReading'

/**
 * The credit reading is read by the app shell and Settings, so every render it
 * causes re-renders the whole dashboard. A poll that brings back the same
 * value, or fails the same way again, must cause none; and a failing read must
 * wait longer before each next poll.
 */

vi.mock('../api/client', () => ({
  api: {
    sessionsUsage: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

const KIRO_USAGE_KEY = ['kiro-usage']
const READING = { usage: { credits_plan: 10_000, credits_used: 100 } }
const unanswered = () => Object.assign(new Error('kiro_prerequisite_required'), { status: 503 })

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let renders = 0
  function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
  const view = renderHook(() => { renders += 1; return useKiroAccountReading() }, { wrapper: Wrapper })
  return { queryClient, renders: () => renders, ...view }
}

async function pollTimes(queryClient: QueryClient, n: number) {
  for (let i = 0; i < n; i += 1) {
    await act(() => queryClient.refetchQueries({ queryKey: KIRO_USAGE_KEY }))
  }
}

function nextWait(queryClient: QueryClient): unknown {
  const query = queryClient.getQueryCache().find({ queryKey: KIRO_USAGE_KEY })!
  const option = query.observers[0].options.refetchInterval
  return typeof option === 'function' ? option(query as Query) : option
}

describe('the credit reading re-renders only on a change', () => {
  afterEach(() => { vi.clearAllMocks() })

  it('does not re-render when a poll returns the same reading', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).kirocrewConfig.mockResolvedValue({ is_kiro_backend: true } as never)
    vi.mocked(api).sessionsUsage.mockResolvedValue(READING as never)
    const { queryClient, result, renders } = mount()
    await waitFor(() => expect(result.current.kiroUsageState).toMatchObject({ used: 100 }))
    await waitFor(() => expect(queryClient.getQueryState(['kirocrewConfig'])?.status).toBe('success'))
    const settled = renders()

    await pollTimes(queryClient, 3)

    expect(api.sessionsUsage).toHaveBeenCalledTimes(4)
    expect(renders()).toBe(settled)
  })

  it('re-renders when the reading changes', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).kirocrewConfig.mockResolvedValue({ is_kiro_backend: true } as never)
    vi.mocked(api).sessionsUsage.mockResolvedValue(READING as never)
    const { queryClient, result } = mount()
    await waitFor(() => expect(result.current.kiroUsageState).toMatchObject({ used: 100 }))

    vi.mocked(api).sessionsUsage.mockResolvedValue({ usage: { credits_plan: 10_000, credits_used: 250 } } as never)
    await pollTimes(queryClient, 1)

    expect(result.current.kiroUsageState).toMatchObject({ used: 250 })
  })

  it('stays failed without re-rendering while the read keeps failing, and backs off', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).kirocrewConfig.mockResolvedValue({ is_kiro_backend: true } as never)
    vi.mocked(api).sessionsUsage.mockRejectedValue(unanswered())
    const { queryClient, result, renders } = mount()
    await waitFor(() => expect(result.current.kiroUsageState).toBe('failed'))
    await waitFor(() => expect(queryClient.getQueryState(['kirocrewConfig'])?.status).toBe('success'))
    expect(nextWait(queryClient)).toBe(KIRO_USAGE_WARMING_POLL_MS)
    const settled = renders()

    await pollTimes(queryClient, 2)

    expect(result.current.kiroUsageState).toBe('failed')
    expect(renders()).toBe(settled)
    expect(nextWait(queryClient)).toBe(KIRO_USAGE_WARMING_POLL_MS * 4)
  })
})
