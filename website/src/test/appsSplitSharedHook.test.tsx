/**
 * Shared-contract pin (PR1 App Store split): BOTH DiscoverPage and
 * LibraryPage must take their data identity from the single `useAppsData`
 * hook.
 *
 * The split's core invariant — same reason `mergeBuiltinRow` exists: two
 * inline derivations of the registry/installed merge in two files can
 * contradict each other (the AppsPage/AppDetailPage author-precedence bug);
 * one shared module cannot. If either page stops calling the hook and grows
 * its own registry/installed query, this test goes red even though the page
 * might still LOOK right.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const useAppsDataSpy = vi.hoisted(() => vi.fn(() => ({
  apps: [], appsLoading: false, appsError: null,
  registry: [], registryError: null,
  loading: false, registries: [],
  browseApps: [], featuredSections: [], categories: [],
  sources: [], installedApps: [], updatables: [],
  announceAppsChanged: vi.fn(),
})))

vi.mock('../pages/apps/useAppsData', async importOriginal => {
  const orig = await importOriginal<typeof import('../pages/apps/useAppsData')>()
  return { ...orig, default: useAppsDataSpy as unknown as typeof orig.default }
})

import DiscoverPage from '../pages/apps/DiscoverPage'
import LibraryPage from '../pages/apps/LibraryPage'

function renderPage(ui: React.ReactElement, path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('App Store split — shared useAppsData contract', () => {
  beforeEach(() => {
    useAppsDataSpy.mockClear()
    sessionStorage.clear()
  })

  it('DiscoverPage renders from useAppsData', () => {
    renderPage(<DiscoverPage />, '/apps')
    expect(useAppsDataSpy).toHaveBeenCalled()
  })

  it('LibraryPage renders from useAppsData', () => {
    renderPage(<LibraryPage />, '/apps/library')
    expect(useAppsDataSpy).toHaveBeenCalled()
  })
})
