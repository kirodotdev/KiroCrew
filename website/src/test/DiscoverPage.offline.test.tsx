/**
 * DiscoverPage — the store's gateway writes are refused while the gateway is
 * offline (#10463): the store refresh, Enable on a featured card and on a list
 * row, and the Updates tab's Update / Update All dim, say why in their own
 * tooltip, and send no request, the same refusal the Library page uses.
 *
 * `useAppsData` is a fixture (its derivation is pinned in its own tests);
 * `useAppActions` and `useAppUpdates` run real, so a refused click is proven
 * by the api mock never being called. The Sources popover is its own surface.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createConnectedTestStore, createTestStore } from './helpers'
import type { AppsData } from '../pages/apps/useAppsData'
import type { RegistryApp } from '../components/appstore/types'

const mocks = vi.hoisted(() => ({
  appsData: vi.fn(),
  enableApp: vi.fn(),
  updateApp: vi.fn(),
  refreshAppStore: vi.fn(),
  refreshRegistries: vi.fn(),
}))

vi.mock('../pages/apps/useAppsData', async importOriginal => {
  const orig = await importOriginal<typeof import('../pages/apps/useAppsData')>()
  return { ...orig, default: mocks.appsData }
})

vi.mock('../api/client', () => ({
  api: {
    enableApp: (...a: unknown[]) => mocks.enableApp(...a),
    updateApp: (...a: unknown[]) => mocks.updateApp(...a),
    refreshAppStore: (...a: unknown[]) => mocks.refreshAppStore(...a),
    refreshRegistries: (...a: unknown[]) => mocks.refreshRegistries(...a),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))
vi.mock('../components/appstore/SourcesPopover', () => ({ default: () => null }))

import DiscoverPage from '../pages/apps/DiscoverPage'

// An installed but disabled built-in: the one row both the featured card and
// the list offer Enable on.
const board = {
  name: 'board', displayName: 'Board', description: 'Plan work.', version: '1.0.0',
  author: 'Kiro', tags: [], origin: 'builtin', installed: true, enabled: false,
} as unknown as RegistryApp

const notes = {
  name: 'notes', displayName: 'Notes', version: '1.0.0', _newVersion: '1.1.0',
  updateAvailable: true, origin: 'local', source: '/home/u/apps/notes',
  enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['updatables'][number]

function data(): AppsData {
  return {
    apps: [notes] as AppsData['apps'],
    appsLoading: false,
    appsError: null,
    registryError: null,
    loading: false,
    browseApps: [board],
    featuredSections: [{ form: 'full', curated: false, items: [{ type: 'app', appRefs: ['board'], apps: [board] }] }],
    categories: [],
    sources: [],
    registriesUnknown: false,
    registryFetchFailures: [],
    installedApps: [notes],
    updatables: [notes],
    announceAppsChanged: vi.fn(),
  } as AppsData
}

const OFFLINE_TITLE = 'Gateway offline — reconnect to manage apps'

function renderDiscover(store: ReturnType<typeof createTestStore>, path = '/apps') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/apps" element={<DiscoverPage />} />
            <Route path="/apps/-/updates" element={<DiscoverPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  mocks.appsData.mockReturnValue(data())
  mocks.enableApp.mockResolvedValue({ ok: true })
  mocks.updateApp.mockResolvedValue({ ok: true })
  mocks.refreshAppStore.mockResolvedValue({ ok: true })
  mocks.refreshRegistries.mockResolvedValue({ ok: true, refreshed: [], failed: [], results: [] })
})

describe('DiscoverPage offline', () => {
  it('the store refresh and both Enable buttons are inert, say why, and write nothing', () => {
    renderDiscover(createTestStore())
    const refresh = screen.getByRole('button', { name: 'Refresh the store disabled — gateway offline' })
    expect(refresh).toHaveAttribute('aria-disabled', 'true')
    expect(refresh).toHaveAttribute('title', OFFLINE_TITLE)
    fireEvent.click(refresh)

    const enables = screen.getAllByRole('button', { name: 'Enable disabled — gateway offline' })
    // One on the featured card, one on the list row.
    expect(enables).toHaveLength(2)
    for (const enable of enables) {
      expect(enable).toBeDisabled()
      expect(enable).toHaveAttribute('title', OFFLINE_TITLE)
      fireEvent.click(enable)
    }
    expect(mocks.refreshAppStore).not.toHaveBeenCalled()
    expect(mocks.refreshRegistries).not.toHaveBeenCalled()
    expect(mocks.enableApp).not.toHaveBeenCalled()
  })

  it('the Updates tab holds Update and Update All, says why, and updates nothing', () => {
    renderDiscover(createTestStore(), '/apps/-/updates')
    const all = screen.getByRole('button', { name: 'Update All disabled — gateway offline' })
    const row = screen.getByRole('button', { name: 'Update disabled — gateway offline' })
    for (const button of [all, row]) {
      expect(button).toHaveAttribute('aria-disabled', 'true')
      expect(button).toHaveAttribute('title', OFFLINE_TITLE)
      fireEvent.click(button)
    }
    expect(mocks.updateApp).not.toHaveBeenCalled()
  })

  it('connected, Enable writes', async () => {
    renderDiscover(createConnectedTestStore())
    fireEvent.click(screen.getAllByRole('button', { name: 'Enable' })[0])
    await waitFor(() => expect(mocks.enableApp).toHaveBeenCalledWith('board'))
  })

  it('connected, an Updates row writes', async () => {
    renderDiscover(createConnectedTestStore(), '/apps/-/updates')
    fireEvent.click(screen.getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(mocks.updateApp).toHaveBeenCalledTimes(1))
  })
})
