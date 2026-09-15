/**
 * Tests for the React Query discovery path of useSessionControls.
 *
 * `resolveSessionControls` is covered directly in useSessionControls.test.ts —
 * this file covers only what the query layer adds: that the app list is read
 * through the api client, that a partially-mocked api cannot throw
 * synchronously, and that every failure leaves the composer with no controls
 * rather than an error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHookWithProviders } from './helpers'
import { useSessionControls } from '../hooks/useSessionControls'
import { api } from '../api/client'

vi.mock('../api/client', () => ({ api: { listApps: vi.fn() } }))

const mockListApps = api.listApps as unknown as ReturnType<typeof vi.fn>

const app = () => ({
  name: 'test-app',
  version: '0.1.0',
  displayName: 'Test App',
  enabled: true,
  manifest: {
    version: '0.1.0',
    displayName: 'Test App',
    ui: {},
    contributes: {
      sessionControls: [
        { id: 'scope', entryPoint: 'dist/session-control.mjs', label: 'Scope' },
      ],
    },
    permissions: { api: [], events: [] },
  },
})

describe('useSessionControls', () => {
  beforeEach(() => vi.clearAllMocks())

  it('resolves controls from the app list', async () => {
    mockListApps.mockResolvedValue([app()])
    const { result } = renderHookWithProviders(() => useSessionControls().controls)
    await waitFor(() => expect(result.current).toHaveLength(1))
    expect(result.current[0].key).toBe('test-app:scope')
  })

  it('accepts the { apps: [...] } envelope as well as a bare array', async () => {
    mockListApps.mockResolvedValue({ apps: [app()] })
    const { result } = renderHookWithProviders(() => useSessionControls().controls)
    await waitFor(() => expect(result.current).toHaveLength(1))
  })

  it('returns no controls while the query is in flight', () => {
    // The composer renders on the first paint, before any app list has arrived.
    mockListApps.mockReturnValue(new Promise(() => {}))
    const { result } = renderHookWithProviders(() => useSessionControls().controls)
    expect(result.current).toEqual([])
  })

  it('fails closed when the app list is unreachable', async () => {
    mockListApps.mockRejectedValue(new Error('HTTP 503'))
    const { result } = renderHookWithProviders(() => useSessionControls().controls)
    await waitFor(() => expect(mockListApps).toHaveBeenCalled())
    expect(result.current).toEqual([])
  })

  it('reports the app-list error instead of swallowing it', async () => {
    // Failing closed is not the same as failing silently: no chips is
    // indistinguishable from "no app declares one", so the caller needs the
    // error to render a surface. `errors-use-error-notice` names a `useQuery`
    // error as an error for exactly this reason.
    mockListApps.mockRejectedValue(new Error('HTTP 503'))
    const { result } = renderHookWithProviders(() => useSessionControls())
    await waitFor(() => expect(result.current.error).toBeTruthy())
    expect(result.current.error?.message).toContain('HTTP 503')
    expect(result.current.controls).toEqual([])
  })

  it('normalizes a non-array payload rather than crashing on it', async () => {
    // A `= []` default only covers undefined; a non-array resolves past it and
    // previously crashed the whole chat page downstream on `.find`.
    mockListApps.mockResolvedValue('nope')
    const { result } = renderHookWithProviders(() => useSessionControls().controls)
    await waitFor(() => expect(mockListApps).toHaveBeenCalled())
    expect(result.current).toEqual([])
  })

  it('survives an api surface with no listApps at all', async () => {
    // Guarding the call, not just its rejection. Asserting on the returned array
    // cannot test this: `data ?? []` yields [] both when the guard returns []
    // AND when the queryFn throws and React Query records an error — so the
    // obvious assertion passes with the guard deleted. The query's own status is
    // the only thing that differs, so this drives its own QueryClient in order
    // to read it.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )
    const original = api.listApps
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (api as any).listApps
    try {
      const { result } = renderHook(() => useSessionControls().controls, { wrapper })
      // The hook shares the app list's ['apps'] key, so that is the state to
      // read — a private ['session-controls'] key would never have been
      // invalidated by an app being enabled or disabled.
      await waitFor(() =>
        expect(queryClient.getQueryState(['apps'])?.status).toBe('success'),
      )
      expect(result.current).toEqual([])
    } finally {
      // try/finally so a failed assertion cannot leak the deleted method into
      // every test that runs after this one.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ;(api as any).listApps = original
      queryClient.clear()
    }
  })
})
