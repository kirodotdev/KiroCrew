/**
 * Discover sub-tabs + the Updates sub-page (PR2 App Store split).
 *
 * Pins the page-level contract around the `Featured | Updates` sub-tabs:
 *
 * - URL sync: the tabs are navigation between two addressable screens —
 *   `/apps` (Featured) and `/apps/-/updates` — and a deep-link/refresh renders
 *   the Updates tab active on the FIRST frame (synchronous init from the URL,
 *   no Featured flash).
 * - The legacy `appstore-tab` key must not hijack an Updates deep-link: the
 *   redirect wrapper only applies to the bare `/apps` mount.
 * - The Updates list renders one row per `updatables` entry (name + version
 *   diff + in-place Update), rows leave via the DATA refresh (a shrunk
 *   `updatables` drops the row and the tab count together), the per-row
 *   pending state disables only ITS row, and Update All replaces its own
 *   button text with the sequential `{done,total}` progress while freezing
 *   every row.
 * - The zero-updates state: check-mark empty state with a back-to-Featured
 *   action, and no count badge on the tab.
 *
 * `useAppsData` is mocked (data derivation is pinned in its own tests);
 * `useAppUpdates` runs REAL against a deferred `api.updateApp` so the
 * pending/progress renders are exercised through the page, not simulated.
 * `i18nT` is mocked to `key {params}` so assertions pin keys, not copy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, waitFor, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom'
import type { AppsData } from '../pages/apps/useAppsData'

const mocks = vi.hoisted(() => ({
  appsData: vi.fn(),
  apiUpdateApp: vi.fn(),
  setError: vi.fn(),
  updateAppNav: vi.fn(),
  announce: vi.fn(),
}))

// Data identity is pinned in useAppsData's own tests — here it is a fixture
// the test swaps to simulate the refresh that follows announceAppsChanged.
vi.mock('../pages/apps/useAppsData', async importOriginal => {
  const orig = await importOriginal<typeof import('../pages/apps/useAppsData')>()
  return { ...orig, default: mocks.appsData }
})

// Action plumbing (detail navigation, trust consent, enable) is
// useAppActions' contract, pinned elsewhere. The page only needs the shape.
vi.mock('../pages/apps/useAppActions', () => ({
  useAppActions: () => ({
    setError: mocks.setError,
    displayError: '',
    dismissError: vi.fn(),
    openDetail: vi.fn(),
    getApp: vi.fn(),
    updateApp: mocks.updateAppNav,
    trustTarget: vi.fn(),
    runEnable: vi.fn(),
    trust: {
      target: null, pending: false, failed: false, granted: false,
      open: vi.fn(), cancel: vi.fn(), confirm: vi.fn(),
    },
  }),
}))

vi.mock('../api/client', () => ({
  api: { updateApp: (...a: unknown[]) => mocks.apiUpdateApp(...a) },
}))

vi.mock('../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key} ${JSON.stringify(params)}` : key,
  }
})

// Chrome the sub-tab tests never drive: the Sources popover owns its own
// tests, and the trust modal renders null for a null target anyway.
vi.mock('../components/appstore/SourcesPopover', () => ({ default: () => null }))
vi.mock('../components/appstore/TrustAppModal', () => ({
  default: () => null,
  isTrustDeniedError: () => false,
}))

import DiscoverPage from '../pages/apps/DiscoverPage'

// LibraryApp rows at exactly the fields the Updates surfaces read: identity +
// version diff for the row, origin/source for the recorded-source routing
// (path-installed, so runUpdate calls api.updateApp in place), lifecycle for
// the updatable filter the fixture list already applied.
const notes = {
  name: 'notes', displayName: 'Notes', version: '1.0.0', _newVersion: '1.1.0',
  updateAvailable: true, origin: 'local', source: '/home/u/apps/notes',
  enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['updatables'][number]
const docs = {
  name: 'docs', displayName: 'Docs', version: '2.0.0', _newVersion: '2.1.0',
  updateAvailable: true, origin: 'local', source: '/home/u/apps/docs',
  enabled: true, lifecycle: 'gateway',
} as unknown as AppsData['updatables'][number]

function baseData(over: Partial<AppsData> = {}): AppsData {
  return {
    apps: [notes, docs] as AppsData['apps'],
    appsLoading: false,
    appsError: null,
    registryError: null,
    loading: false,
    browseApps: [],
    featuredSections: [],
    categories: [],
    sources: [],
    installedApps: [notes, docs],
    updatables: [notes, docs],
    announceAppsChanged: mocks.announce,
    ...over,
  }
}

/** Records the live pathname so URL-sync assertions read the router itself. */
function PathProbe() {
  const { pathname } = useLocation()
  return <div data-testid="path">{pathname}</div>
}

function discoverUi(initialPath: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <PathProbe />
        <Routes>
          <Route path="/apps" element={<DiscoverPage />} />
          <Route path="/apps/-/updates" element={<DiscoverPage />} />
          <Route path="/apps/library" element={<div data-testid="library-route" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const renderDiscover = (initialPath: string) => render(discoverUi(initialPath))

/** A deferred api.updateApp call the test resolves/rejects by hand. */
function deferUpdates() {
  const deferred: Array<{ resolve: () => void; reject: (e: Error) => void }> = []
  mocks.apiUpdateApp.mockImplementation(
    () => new Promise<void>((resolve, reject) => { deferred.push({ resolve, reject }) }),
  )
  return deferred
}

const featuredTab = () => screen.getByRole('tab', { name: /pages\.discoverPage\.tab_featured/ })
const updatesTab = () => screen.getByRole('tab', { name: /pages\.discoverPage\.tab_updates/ })
/** The per-row Update buttons, in `updatables` order. */
const rowUpdateButtons = () =>
  screen.getAllByRole('button', { name: 'components.appstore.installedAppCard.update' })
const updateAllButton = () =>
  screen.getByRole('button', { name: /pages\.appsPage\.update_all|pages\.appsPage\.updating_progress/ })

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  mocks.appsData.mockReturnValue(baseData())
  mocks.apiUpdateApp.mockResolvedValue(undefined)
})

describe('Discover sub-tabs — URL sync', () => {
  it('a /apps/-/updates deep-link renders the Updates tab active on the first frame', () => {
    renderDiscover('/apps/-/updates')
    // Synchronous init from the URL: no waitFor — the FIRST render must
    // already be the Updates tab, or a refresh flashes Featured.
    expect(updatesTab().getAttribute('aria-selected')).toBe('true')
    expect(featuredTab().getAttribute('aria-selected')).toBe('false')
    expect(screen.getByRole('heading', { name: /pages\.appsPage\.update /, level: 3 })).toBeInTheDocument()
  })

  it('clicking the tabs navigates between /apps and /apps/-/updates', async () => {
    renderDiscover('/apps')
    expect(featuredTab().getAttribute('aria-selected')).toBe('true')
    expect(screen.getByTestId('path').textContent).toBe('/apps')

    // Radix's tab trigger activates on mousedown, not click.
    fireEvent.mouseDown(updatesTab())
    await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/apps/-/updates'))
    expect(updatesTab().getAttribute('aria-selected')).toBe('true')
    // The Updates worklist replaced the storefront.
    expect(rowUpdateButtons()).toHaveLength(2)

    fireEvent.mouseDown(featuredTab())
    await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/apps'))
    expect(featuredTab().getAttribute('aria-selected')).toBe('true')
  })

  it('a stale legacy library key does NOT hijack an Updates deep-link (but is still cleared)', async () => {
    sessionStorage.setItem('appstore-tab', 'library')
    renderDiscover('/apps/-/updates')
    expect(screen.queryByTestId('library-route')).not.toBeInTheDocument()
    expect(updatesTab().getAttribute('aria-selected')).toBe('true')
    await waitFor(() => expect(sessionStorage.getItem('appstore-tab')).toBeNull())
  })
})

describe('Updates sub-page — list rendering', () => {
  it('renders one row per updatable: name, version diff, and an Update button', () => {
    renderDiscover('/apps/-/updates')
    expect(screen.getByText('Notes')).toBeInTheDocument()
    expect(screen.getByText('Docs')).toBeInTheDocument()
    // Version diff pins the from/to interpolation, not any locale's copy.
    expect(screen.getByText('pages.discoverPage.version_diff {"from":"1.0.0","to":"1.1.0"}')).toBeInTheDocument()
    expect(screen.getByText('pages.discoverPage.version_diff {"from":"2.0.0","to":"2.1.0"}')).toBeInTheDocument()
    expect(rowUpdateButtons()).toHaveLength(2)
    // The tab wears the same count the list has rows.
    expect(within(updatesTab()).getByText('2')).toBeInTheDocument()
  })

  it('per-row pending state disables ONLY the updating row, with an in-flight label', async () => {
    const deferred = deferUpdates()
    renderDiscover('/apps/-/updates')

    fireEvent.click(rowUpdateButtons()[0])
    // The pending row's button swaps to the in-flight form and disables;
    // its accessible name changes with it, so locate it by the new label.
    const pendingButton = await screen.findByRole('button', { name: 'pages.discoverPage.updating' })
    expect(pendingButton).toBeDisabled()
    // EVERY row freezes while one update runs: `updatePending` is a single
    // slot, so a clickable sibling would invite a dispatch the hook refuses —
    // a dead click. Mirrors the Update All freeze.
    expect(rowUpdateButtons()[0]).toBeDisabled()
    expect(mocks.apiUpdateApp).toHaveBeenCalledExactlyOnceWith('notes')

    await act(async () => { deferred[0].resolve() })
    // Settled: the in-flight form leaves and both rows read Update again.
    await waitFor(() => expect(rowUpdateButtons()).toHaveLength(2))
    expect(rowUpdateButtons()[0]).not.toBeDisabled()
    // The success announced the data change — the refresh (next test) is what
    // actually removes the row.
    expect(mocks.announce).toHaveBeenCalledTimes(1)
  })

  it('a row leaves the list via the data refresh, and the tab count drops with it', async () => {
    const view = renderDiscover('/apps/-/updates')
    expect(screen.getByText('Notes')).toBeInTheDocument()
    expect(within(updatesTab()).getByText('2')).toBeInTheDocument()

    // The refresh that follows announceAppsChanged: registry/apps refetch,
    // `updatables` no longer carries the updated app.
    mocks.appsData.mockReturnValue(baseData({
      installedApps: [docs],
      updatables: [docs],
    }))
    view.rerender(discoverUi('/apps/-/updates'))

    await waitFor(() => expect(screen.queryByText('Notes')).not.toBeInTheDocument())
    expect(screen.getByText('Docs')).toBeInTheDocument()
    expect(rowUpdateButtons()).toHaveLength(1)
    expect(within(updatesTab()).getByText('1')).toBeInTheDocument()
  })

  it('Update All shows sequential {done,total} progress and freezes every row', async () => {
    const deferred = deferUpdates()
    renderDiscover('/apps/-/updates')

    fireEvent.click(updateAllButton())
    // The disabled button IS the progress surface.
    await waitFor(() =>
      expect(updateAllButton()).toHaveTextContent('pages.appsPage.updating_progress {"done":0,"total":2}'),
    )
    expect(updateAllButton()).toBeDisabled()
    for (const btn of rowUpdateButtons()) expect(btn).toBeDisabled()

    await act(async () => { deferred[0].resolve() })
    await waitFor(() =>
      expect(updateAllButton()).toHaveTextContent('pages.appsPage.updating_progress {"done":1,"total":2}'),
    )

    await act(async () => { deferred[1].resolve() })
    await waitFor(() =>
      expect(updateAllButton()).toHaveTextContent('pages.appsPage.update_all'),
    )
    // The batch's success lands in the page's own notice surface.
    expect(screen.getByText('pages.appsPage.updated_app {"count":2}')).toBeInTheDocument()
    expect(mocks.apiUpdateApp).toHaveBeenNthCalledWith(1, 'notes')
    expect(mocks.apiUpdateApp).toHaveBeenNthCalledWith(2, 'docs')
  })
})

describe('Updates sub-page — empty state', () => {
  beforeEach(() => {
    mocks.appsData.mockReturnValue(baseData({
      installedApps: [], updatables: [],
    }))
  })

  it('renders the all-current empty state with a back-to-Featured action, and no tab count', async () => {
    renderDiscover('/apps/-/updates')
    expect(screen.getByTestId('empty-state-title')).toHaveTextContent('pages.discoverPage.updates_empty')
    // TabsCount hides the count at zero — no "0" badge noise.
    expect(within(updatesTab()).queryByText('0')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'pages.discoverPage.updates_empty_back_to_featured' }))
    await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/apps'))
    expect(featuredTab().getAttribute('aria-selected')).toBe('true')
  })

  it('a failed registry fetch never asserts the false all-clear', () => {
    // An empty list under an error means the count is UNKNOWN, not zero:
    // once the dismissible error notice is closed, an "up to date" claim
    // would be the only thing left standing.
    mocks.appsData.mockReturnValue(baseData({
      installedApps: [], updatables: [], registryError: new Error('registry unreachable'),
    }))
    renderDiscover('/apps/-/updates')
    expect(screen.getByTestId('empty-state-title')).toHaveTextContent('pages.discoverPage.updates_check_failed')
    expect(screen.queryByText('pages.discoverPage.updates_empty')).not.toBeInTheDocument()
  })
})
