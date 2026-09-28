/**
 * `usePanelTabDescriptors` shares the `['apps']` query key with
 * `useSessionControls`. React Query gives one key ONE fetch, so an observer that
 * registers without a usable `queryFn` decides the key has none — and the other
 * consumer fails even though its own hook guarded correctly. That is not a test
 * artifact: any partially-mocked or trimmed `api` surface reproduces it, and the
 * visible symptom was the composer rendering "Session controls unavailable".
 *
 * So the guard is on the CALL, not the rejection, matching the sibling hook.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'

const mock = vi.hoisted(() => ({ listApps: undefined as undefined | (() => Promise<unknown>) }))
vi.mock('../api/client', () => ({
  get api() {
    // A PARTIAL api surface: `listApps` is absent unless a test supplies it, which
    // is exactly the shape that made the raw `queryFn: api.listApps` register
    // `undefined`.
    return mock.listApps ? { listApps: mock.listApps } : {}
  },
}))

const wrapper = ({ children }: { children: React.ReactNode }) => {
  // No `defaultOptions.queries.queryFn`, so a missing per-query fn is an error
  // rather than something the client quietly supplies.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return React.createElement(QueryClientProvider, { client }, children)
}

beforeEach(() => { mock.listApps = undefined })

describe('usePanelTabDescriptors — shared ["apps"] observer', () => {
  it('registers a usable queryFn even when api.listApps is absent', async () => {
    const { usePanelTabDescriptors } = await import('../hooks/panelTabRegistry')
    const { result } = renderHook(() => usePanelTabDescriptors(), { wrapper })
    // No descriptors, and no thrown/rejected query: the absent method degrades to
    // an empty app list.
    await waitFor(() => expect(result.current).toEqual([]))
  })

  it('does not poison the key for a co-observer that guards its own fn', async () => {
    const { usePanelTabDescriptors } = await import('../hooks/panelTabRegistry')
    // Stands in for `useSessionControls`: same key, its own guarded fn. It must
    // still resolve while `usePanelTabDescriptors` observes alongside it.
    const { result } = renderHook(
      () => {
        usePanelTabDescriptors()
        return useQuery({ queryKey: ['apps'], queryFn: () => Promise.resolve([{ name: 'a' }]) })
      },
      { wrapper },
    )
    await waitFor(() => expect(result.current.error).toBeNull())
    await waitFor(() => expect(result.current.data).toBeDefined())
  })

  it('resolves descriptors normally once api.listApps exists', async () => {
    mock.listApps = () => Promise.resolve([
      {
        name: 'pippin',
        enabled: true,
        manifest: {
          contributes: {
            panelTabs: [
              { id: 'browser', title: 'Pippin', menuLabel: 'Pippin', icon: 'BookOpen', entry: 'panel.mjs' },
            ],
          },
        },
      },
    ])
    vi.resetModules()
    const { usePanelTabDescriptors } = await import('../hooks/panelTabRegistry')
    const { result } = renderHook(() => usePanelTabDescriptors(), { wrapper })
    await waitFor(() => expect(result.current.map(d => d.kind)).toEqual(['app:pippin:browser']))
  })
})
