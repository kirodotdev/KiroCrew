/**
 * `useSessionKindDescriptors` reads the shared, guarded `['apps']` observer
 * (`useInstalledApps`) rather than registering its own: a second hand-rolled
 * observer on that key is a second chance to register an unusable `queryFn`, and
 * the failure lands in an unrelated consumer (see panelTabDescriptorsSharedQuery).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mock = vi.hoisted(() => ({ listApps: undefined as undefined | (() => Promise<unknown>) }))
vi.mock('../api/client', () => ({
  get api() {
    return mock.listApps ? { listApps: mock.listApps } : {}
  },
}))

const wrapper = ({ children }: { children: React.ReactNode }) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return React.createElement(QueryClientProvider, { client }, children)
}

beforeEach(() => { mock.listApps = undefined })

describe('useSessionKindDescriptors — shared ["apps"] observer', () => {
  it('degrades to no kinds when api.listApps is absent', async () => {
    const { useSessionKindDescriptors } = await import('../hooks/sessionKindRegistry')
    const { result } = renderHook(() => useSessionKindDescriptors(), { wrapper })
    await waitFor(() => expect(result.current).toEqual([]))
  })

  it('resolves the enabled apps’ kinds once api.listApps answers', async () => {
    mock.listApps = () => Promise.resolve([
      { name: 'project-apps', enabled: true, manifest: { contributes: { sessionKinds: [
        { id: 'project', title: 'Project app', menuLabel: 'New project app', entry: 'dist/pane.mjs' },
      ] } } },
      { name: 'off', enabled: false, manifest: { contributes: { sessionKinds: [
        { id: 'project', title: 'Off', menuLabel: 'Off', entry: 'x.mjs' },
      ] } } },
    ])
    vi.resetModules()
    const { useSessionKindDescriptors } = await import('../hooks/sessionKindRegistry')
    const { result } = renderHook(() => useSessionKindDescriptors(), { wrapper })
    await waitFor(() => expect(result.current.map(d => `${d.appName}:${d.kindId}`)).toEqual(['project-apps:project']))
  })
})
