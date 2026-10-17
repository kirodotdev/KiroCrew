/**
 * useConfigSchema — the `requiresRestart` flag must survive the wire mapping.
 *
 * The backend OMITS the key for every field that hot-reloads, so the mapper has
 * to keep "absent" and "false" both meaning live; only an explicit `true`
 * reaches the UI as a restart hint.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { PropsWithChildren } from 'react'
import { useConfigSchema } from './useConfigSchema'

const ENTRIES = [
  { path: 'dashboard.url', type: 'string', requiresRestart: true },
  { path: 'session.pool_size', type: 'integer' },
  { path: 'session.timeout_secs', type: 'integer', requiresRestart: false },
]

function wrapper({ children }: PropsWithChildren) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('useConfigSchema', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ entries: ENTRIES }),
    })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('maps requiresRestart only when the backend sent true', async () => {
    const { result } = renderHook(() => useConfigSchema(), { wrapper })
    await waitFor(() => expect(result.current).toBeInstanceOf(Map))
    const map = result.current!
    expect(map.get('dashboard.url')?.requiresRestart).toBe(true)
    expect(map.get('session.pool_size')?.requiresRestart).toBeUndefined()
    expect(map.get('session.timeout_secs')?.requiresRestart).toBeUndefined()
  })
})
