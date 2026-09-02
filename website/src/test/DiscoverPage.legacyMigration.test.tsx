/**
 * Legacy `sessionStorage['appstore-tab']` migration (PR1 App Store split).
 *
 * The pre-split AppsPage persisted its active tab under this key
 * (discover/library/installed/browse). DiscoverPage's mount wrapper migrates
 * it: a library-mapped value redirects to /apps/library ONCE via a REPLACE
 * navigation (no extra history entry, no Discover frame painted first), and
 * the key is cleared in ALL cases — library, discover, or garbage — so the
 * redirect can never fire twice and the legacy key dies here.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route, useNavigationType } from 'react-router-dom'

// Data identity is owned by useAppsData (mocked here — this test pins the
// MIGRATION wrapper, not data derivation). Empty data renders the page's
// empty state, which is all the redirect assertions need.
vi.mock('../pages/apps/useAppsData', async importOriginal => {
  const orig = await importOriginal<typeof import('../pages/apps/useAppsData')>()
  return {
    ...orig,
    default: vi.fn(() => ({
      apps: [], appsLoading: false, appsError: null,
      registry: undefined, registryLoading: false, registryError: null,
      loading: false, registries: [],
      browseApps: [], featuredSections: [], categoryOrder: [], categories: [],
      sources: [], installedApps: [], updatables: [],
      announceAppsChanged: vi.fn(),
    })),
  }
})

import DiscoverPage from '../pages/apps/DiscoverPage'

/** Probe for the /apps/library route: records HOW we arrived (PUSH vs
 *  REPLACE) so the `replace` semantics of the redirect are pinned, not just
 *  the destination. */
function LibraryProbe() {
  const navType = useNavigationType()
  return <div data-testid="library-route" data-nav-type={navType} />
}

function renderApps() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<DiscoverPage />} />
          <Route path="/apps/library" element={<LibraryProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('DiscoverPage — legacy appstore-tab migration', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it("'library' redirects /apps to /apps/library with REPLACE and clears the key", async () => {
    sessionStorage.setItem('appstore-tab', 'library')
    renderApps()
    const probe = await screen.findByTestId('library-route')
    // REPLACE, not PUSH: the redirect must not leave a dead /apps entry the
    // user would bounce off when pressing Back.
    expect(probe.getAttribute('data-nav-type')).toBe('REPLACE')
    await waitFor(() => expect(sessionStorage.getItem('appstore-tab')).toBeNull())
  })

  it("'installed' (the older synonym) also maps to Library", async () => {
    sessionStorage.setItem('appstore-tab', 'installed')
    renderApps()
    expect(await screen.findByTestId('library-route')).toBeInTheDocument()
    await waitFor(() => expect(sessionStorage.getItem('appstore-tab')).toBeNull())
  })

  it("'discover' does NOT redirect, but the key is still cleared", async () => {
    sessionStorage.setItem('appstore-tab', 'discover')
    renderApps()
    // Key clearing is the observable end of the mount effect — once it has
    // run, a redirect would already have happened if it was ever going to.
    await waitFor(() => expect(sessionStorage.getItem('appstore-tab')).toBeNull())
    expect(screen.queryByTestId('library-route')).not.toBeInTheDocument()
  })

  it('redirects only ONCE: a second /apps mount after migration stays on Discover', async () => {
    sessionStorage.setItem('appstore-tab', 'library')
    const first = renderApps()
    await screen.findByTestId('library-route')
    await waitFor(() => expect(sessionStorage.getItem('appstore-tab')).toBeNull())
    first.unmount()

    renderApps()
    // The key is gone, so this mount must render Discover, not bounce again.
    expect(screen.queryByTestId('library-route')).not.toBeInTheDocument()
  })
})
