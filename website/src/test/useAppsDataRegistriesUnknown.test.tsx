/**
 * `useAppsData().registriesUnknown` is what makes the install consent gate fail
 * closed on DISPLAY (#11159, r105): while `listRegistries` is still loading OR
 * its fetch failed, the tier is unknown and the not-vetted box shows. Only a
 * landed list clears it. Discover and Library both read it from this one hook.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const listRegistries = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  api: {
    listApps: vi.fn(async () => []),
    listRegistry: vi.fn(async () => ({ apps: [] })),
    listRegistries,
  },
}))

import useAppsData from '../pages/apps/useAppsData'
import { namedCeiling } from './namedCeiling'

function run() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return { qc, ...renderHook(() => useAppsData(), { wrapper }) }
}

describe('useAppsData registriesUnknown', () => {
  beforeEach(() => { listRegistries.mockReset() })

  it('is true while listRegistries is still loading', () => {
    listRegistries.mockReturnValue(new Promise(() => {}))
    const { result } = run()
    expect(result.current.registriesUnknown).toBe(true)
  })

  it('stays true when the listRegistries fetch fails', async () => {
    listRegistries.mockImplementation(() => Promise.reject(new Error('registries down')))
    const { qc, result } = run()
    // Wait for the rejection to settle; the tier must still read as unknown.
    await waitFor(
      () => expect(qc.getQueryState(['registries'])?.status).toBe('error'),
      namedCeiling('REGISTRIES_ERROR', 5000),
    )
    expect(result.current.registriesUnknown).toBe(true)
  })

  it('turns false once the list lands', async () => {
    listRegistries.mockResolvedValue({ registries: [{ name: 'community', review: 'community', trust: 'index' }], pinned: [] })
    const { result } = run()
    await waitFor(() => expect(result.current.registriesUnknown).toBe(false))
  })
})
