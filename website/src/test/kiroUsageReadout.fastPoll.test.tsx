import { afterEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type Query } from '@tanstack/react-query'
import { KIRO_USAGE_STEADY_POLL_MS, KIRO_USAGE_WARMING_POLL_MS } from '../api/kiroUsage'
import { useKiroUsageReadout } from '../shell/topbar/kiroUsageReadout'

/**
 * The credit pill's query must poll at `kiroUsageRefetchInterval`'s cadence.
 *
 * `kiroUsageRefetchInterval.test.ts` pins the helper as a pure function, but
 * nothing pinned that the pill's `useQuery` actually calls it: a plain
 * `refetchInterval: 30_000` kept every one of those tests green, and a reopened
 * pill went back to waiting a full 30s per warming read. This reads the interval
 * the mounted hook gives its `['kiro-usage']` query, in a warming state and in a
 * settled one, so dropping the wiring fails here.
 *
 * It reads the observer's option rather than watching a timer tick: the interval
 * is what the wiring decides, and resolving it is deterministic, where a faked
 * clock against react-query's own scheduling is not.
 */

vi.mock('../api/client', () => ({
  api: {
    sessionsUsage: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

const KIRO_USAGE_KEY = ['kiro-usage']

function mount() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Wrapper({ children }: { children: React.ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }
  const view = renderHook(() => useKiroUsageReadout(), { wrapper: Wrapper })
  return { queryClient, ...view }
}

/** The interval the mounted pill's query would wait before its next read. */
function resolvedInterval(queryClient: QueryClient): unknown {
  const query = queryClient.getQueryCache().find({ queryKey: KIRO_USAGE_KEY })
  expect(query, 'the pill registered no kiro-usage query').toBeDefined()
  const observer = query!.observers[0]
  expect(observer, 'the kiro-usage query has no mounted observer').toBeDefined()
  const option = observer.options.refetchInterval
  return typeof option === 'function' ? option(query as Query) : option
}

describe('the credit pill polls at the warming cadence', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('polls fast while the cache is still warming', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).kirocrewConfig.mockResolvedValue({ is_kiro_backend: true } as never)
    // An empty usage payload parses to `null`: the gateway's cache has not warmed.
    vi.mocked(api).sessionsUsage.mockResolvedValue({ usage: {} } as never)
    const { queryClient, result } = mount()

    await waitFor(() => expect(api.sessionsUsage).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(result.current.kiroUsageState).toBeNull())
    await waitFor(() =>
      expect(queryClient.getQueryState(KIRO_USAGE_KEY)?.status).toBe('success'),
    )

    expect(resolvedInterval(queryClient)).toBe(KIRO_USAGE_WARMING_POLL_MS)
  })

  it('polls fast while the read fails before kiro-cli is ready', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).kirocrewConfig.mockResolvedValue({ is_kiro_backend: true } as never)
    vi.mocked(api).sessionsUsage.mockRejectedValue(new Error('503 kiro_prerequisite_required'))
    const { queryClient, result } = mount()

    await waitFor(() => expect(result.current.kiroUsageState).toBe('failed'))

    expect(resolvedInterval(queryClient)).toBe(KIRO_USAGE_WARMING_POLL_MS)
  })

  it('drops to the steady cadence once a reading lands', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).kirocrewConfig.mockResolvedValue({ is_kiro_backend: true } as never)
    vi.mocked(api).sessionsUsage.mockResolvedValue({
      usage: { credits_plan: 10_000, credits_used: 100 },
    } as never)
    const { queryClient, result } = mount()

    await waitFor(() => expect(result.current.kiroUsageState).toMatchObject({ used: 100 }))

    expect(resolvedInterval(queryClient)).toBe(KIRO_USAGE_STEADY_POLL_MS)
  })
})
