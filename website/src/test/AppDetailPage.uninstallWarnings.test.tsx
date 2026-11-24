/**
 * AppDetailPage — an uninstall that landed with gateway warnings says so.
 *
 * The uninstall route still succeeds when a teardown step fails (a hook stop,
 * a backend it could not verify stopped), and names that in `warnings`. The
 * detail page navigates to `/apps` once the app is gone, so the notice must
 * survive that navigation and show on the real Discover page (or on Library,
 * where Discover's legacy redirect sends it).
 */
import { useEffect, useState } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createConnectedTestStore } from './helpers'

const getApp = vi.fn()
const listRegistry = vi.fn()
const system = vi.fn()
const uninstallApp = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    getApp: (...a: unknown[]) => getApp(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    system: (...a: unknown[]) => system(...a),
    uninstallApp: (...a: unknown[]) => uninstallApp(...a),
    uninstallPreview: vi.fn().mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } }),
    disableApp: vi.fn(),
    updateApp: vi.fn(),
    installFromRegistryStream: vi.fn(),
    trustApp: vi.fn(),
    untrustApp: vi.fn(),
    enableApp: vi.fn(),
    refreshAppStore: vi.fn(),
    refreshRegistries: vi.fn(),
    getMarketRegistries: vi.fn().mockResolvedValue({ offers: [], added: [], status: {} }),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'light' }) }))
vi.mock('../components/AppIcon', () => ({ default: () => <div data-testid="app-icon" /> }))

// Discover's data and update hooks stand settled and empty: these tests need
// only its page notice, which is the real `useAppActions` state.
vi.mock('../pages/apps/useAppsData', async importOriginal => {
  const orig = await importOriginal<typeof import('../pages/apps/useAppsData')>()
  return {
    ...orig,
    default: () => ({
      apps: [], appsLoading: false, appsError: null, registryError: null, loading: false,
      browseApps: [], featuredSections: [], categories: [], sources: [],
      installedApps: [], updatables: [], announceAppsChanged: vi.fn(),
    }),
  }
})
vi.mock('../pages/apps/useAppUpdates', () => ({
  useAppUpdates: () => ({ updatingAll: false, updatePending: {}, runUpdate: vi.fn(), updateAll: vi.fn(), trustHandoff: [] }),
}))

import AppDetailPage from '../pages/AppDetailPage'
import DiscoverPage from '../pages/apps/DiscoverPage'
import LibraryPage from '../pages/apps/LibraryPage'

/** Whether `/apps` still holds router state once it has rendered; never the
 *  state's text, which the notice queries would match. */
function RouteState() {
  return <output data-testid="route-state">{useLocation().state === null ? 'null' : 'set'}</output>
}

/** Discover and Library, plus one urgent update right after mount: the shape of
 *  any unrelated update that lands before the legacy redirect's transition. */
function AppsWithEarlyUpdate() {
  const [, setTick] = useState(0)
  useEffect(() => { setTick(1) }, [])
  return (
    <>
      <Routes>
        <Route path="/apps" element={<DiscoverPage />} />
        <Route path="/apps/library" element={<LibraryPage />} />
      </Routes>
      <RoutePath />
    </>
  )
}

function RoutePath() {
  return <output data-testid="route-path">{useLocation().pathname}</output>
}

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={createConnectedTestStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps/detail/secretary']}>
          <Routes>
            <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            <Route path="/apps" element={<><DiscoverPage /><RouteState /></>} />
            <Route path="/apps/library" element={<LibraryPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

async function uninstallFromDetail() {
  renderDetail()
  fireEvent.click(await screen.findByRole('button', { name: /^Uninstall/ }))
  const box = await screen.findByRole('dialog', { name: /confirm uninstall/i })
  fireEvent.click(within(box).getByRole('button', { name: 'Uninstall' }))
  await waitFor(() => expect(uninstallApp).toHaveBeenCalledWith('secretary', true))
}

describe('AppDetailPage — uninstall warnings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    system.mockResolvedValue({ hostname: '' })
    getApp.mockResolvedValue({
      name: 'secretary', displayName: 'Secretary', version: '1.0.0', enabled: true,
      source: 'registry:secretary', origin: 'registry',
      resources: 'gateway', lifecycle: 'gateway', installedAt: '2026-07-01T00:00:00Z',
      manifest: { displayName: 'Secretary', description: 'Slack inbox manager.', author: 'zezhexu' },
    })
    listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'darwin', arch: 'arm64' } })
  })

  it('shows the warning the uninstall response carried on the page it lands on', async () => {
    uninstallApp.mockResolvedValue({ ok: true, warnings: ["could not verify secretary's backend stopped"] })
    await uninstallFromDetail()
    expect(await screen.findByText(
      /Secretary is uninstalled, but not every step finished\. Details: could not verify secretary's backend stopped/,
    )).toBeInTheDocument()
    // Cleared from history once read, so a reload or Back does not show it again.
    await waitFor(() => expect(screen.getByTestId('route-state')).toHaveTextContent('null'))
    expect(screen.getByText(/Secretary is uninstalled, but not every step finished/)).toBeInTheDocument()
  })

  it('shows the warning when Apps is already open by the time the uninstall finishes', async () => {
    let finish: (res: unknown) => void = () => {}
    uninstallApp.mockReturnValue(new Promise(resolve => { finish = resolve }))
    await uninstallFromDetail()
    // The confirm dialog can be closed while the request is still pending.
    fireEvent.click(within(screen.getByRole('dialog', { name: /confirm uninstall/i })).getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: /Back to Apps/ }))
    expect(await screen.findByRole('button', { name: 'Refresh the store' })).toBeInTheDocument()
    finish({ ok: true, warnings: ["could not verify secretary's backend stopped"] })
    expect(await screen.findByText(/Secretary is uninstalled, but not every step finished/)).toBeInTheDocument()
  })

  it('keeps the warning through the legacy redirect from /apps to the Library', async () => {
    sessionStorage.setItem('appstore-tab', 'library')
    uninstallApp.mockResolvedValue({ ok: true, warnings: ["could not verify secretary's backend stopped"] })
    await uninstallFromDetail()
    expect(await screen.findByText(/Secretary is uninstalled, but not every step finished/)).toBeInTheDocument()
  })

  it('keeps the legacy redirect to the Library when an update lands before it does', async () => {
    sessionStorage.setItem('appstore-tab', 'library')
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={createConnectedTestStore()}>
        <QueryClientProvider client={qc}>
          <MemoryRouter initialEntries={[{ pathname: '/apps', state: { appNotice: 'Secretary is uninstalled, but not every step finished.' } }]}>
            <AppsWithEarlyUpdate />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    expect(await screen.findByText(/Secretary is uninstalled, but not every step finished/)).toBeInTheDocument()
    expect(screen.getByTestId('route-path')).toHaveTextContent('/apps/library')
  })

  it('lands on /apps with no notice when the uninstall carried no warnings', async () => {
    uninstallApp.mockResolvedValue({ ok: true })
    await uninstallFromDetail()
    expect(await screen.findByRole('button', { name: 'Refresh the store' })).toBeInTheDocument()
    expect(screen.queryByText(/not every step finished/)).not.toBeInTheDocument()
  })
})
