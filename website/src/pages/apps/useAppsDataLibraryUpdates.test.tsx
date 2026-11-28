/**
 * useAppsData — which Library rows offer an update.
 *
 * A Library tile shows its update badge and Update menu row from the row's
 * `updateAvailable`. The update route refuses any app whose lifecycle is not
 * `gateway` (a locked built-in, or an app that manages its own updates), so a
 * row for such an app must not offer one even when the registry publishes a
 * newer version under the same name. Update All already applies that gate.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { listApps, listRegistry, listRegistries } = vi.hoisted(() => ({
  listApps: vi.fn(),
  listRegistry: vi.fn(),
  listRegistries: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
  },
}))

import useAppsData from './useAppsData'

const installed = (name: string, lifecycle: string) => ({
  name, displayName: name, version: '1.0.0', enabled: true,
  origin: lifecycle === 'locked' ? 'builtin' : 'registry',
  resources: 'gateway', lifecycle,
})

const published = (name: string) => ({
  name, displayName: name, description: '', version: '2.0.0',
  installed: true, installedVersion: '1.0.0', updateAvailable: true,
})

function renderData() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return renderHook(() => useAppsData(), { wrapper })
}

describe('useAppsData Library update offers', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    listRegistries.mockResolvedValue({ registries: [], pinned: [] })
    listApps.mockResolvedValue([
      installed('store-app', 'gateway'),
      installed('locked-builtin', 'locked'),
      installed('self-managed', 'app'),
    ])
    listRegistry.mockResolvedValue({
      apps: [published('store-app'), published('locked-builtin'), published('self-managed')],
      serverPlatform: { os: 'linux', arch: 'x64' },
    })
  })

  it('offers an update only for a gateway-managed app, never for a locked or app-managed one', async () => {
    const { result } = renderData()
    await waitFor(() =>
      expect(result.current.installedApps.find(a => a.name === 'store-app')?.updateAvailable).toBe(true),
    )
    const byName = new Map(result.current.installedApps.map(a => [a.name, a]))
    expect(byName.get('locked-builtin')?.updateAvailable).toBe(false)
    expect(byName.get('self-managed')?.updateAvailable).toBe(false)
  })

  it('keeps the Library offer and the Update All list in agreement', async () => {
    const { result } = renderData()
    await waitFor(() => expect(result.current.updatables.length).toBeGreaterThan(0))
    const offered = result.current.installedApps.filter(a => a.updateAvailable).map(a => a.name)
    expect(offered).toEqual(result.current.updatables.map(a => a.name))
  })
})
