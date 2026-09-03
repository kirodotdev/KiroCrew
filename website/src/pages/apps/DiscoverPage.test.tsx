/**
 * DiscoverPage — the header's manual store refresh must REPORT its outcome.
 *
 * The refresh handler fires `api.refreshAppStore()` + `api.refreshRegistries()`
 * through `Promise.allSettled` (deliberate: one unreachable source must not stop
 * the other from being repaired). These tests pin that the settled results are
 * READ, not discarded:
 *
 * - a fulfilled registries call reporting `ok: false` with a populated `failed`
 *   array surfaces the failed source names in the page error banner;
 * - an outright rejected refresh POST surfaces its error message;
 * - reporting a failure still runs BOTH query invalidations, so the source that
 *   was repairable is still refetched (a `return` after the report would look
 *   like a harmless simplification and silently break that);
 * - a fully successful refresh surfaces nothing and CLEARS a banner left by an
 *   earlier failed refresh, but does NOT clear a banner another writer put
 *   there — `setError` is shared page state, and Update All's failure notice
 *   has no auto-dismiss.
 *
 * `i18nT` is mocked to `key {params}` so assertions pin the KEY and the
 * interpolated names, not any locale's copy (same style as useAppUpdates.test).
 * `useAppsData` is mocked to an empty, settled shelf: these tests exercise the
 * refresh wiring, not the query layer. `useAppUpdates` is mocked to capture the
 * REAL `setError` the page hands it, which is how a test stands in for one of
 * the other writers of that shared banner without mocking the banner itself.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

const { refreshAppStore, refreshRegistries } = vi.hoisted(() => ({
  refreshAppStore: vi.fn(),
  refreshRegistries: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: {
    refreshAppStore: (...a: unknown[]) => refreshAppStore(...a),
    refreshRegistries: (...a: unknown[]) => refreshRegistries(...a),
  },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key} ${JSON.stringify(params)}` : key,
  }
})

// An empty, settled data shape: no featured blocks, no shelf rows, no pending
// updates — the page renders its empty states and the header controls, which is
// all the refresh wiring needs.
vi.mock('./useAppsData', async importOriginal => {
  const orig = await importOriginal<typeof import('./useAppsData')>()
  return {
    ...orig,
    default: () => ({
      apps: [],
      appsLoading: false,
      appsError: null,
      registryError: null,
      loading: false,
      browseApps: [],
      featuredSections: [],
      categories: [],
      sources: [],
      installedApps: [],
      updatables: [],
      announceAppsChanged: vi.fn(),
    }),
  }
})

/**
 * The page's own `setError`, captured through the seam the update hook uses.
 *
 * The real `useAppUpdates` writes the shared banner via this exact setter
 * (`setError` in its input), so calling it in a test reproduces a foreign
 * write faithfully while the banner state itself stays real.
 */
let pageSetError: ((message: string) => void) | null = null

vi.mock('./useAppUpdates', () => ({
  useAppUpdates: ({ setError }: { setError: (m: string) => void }) => {
    pageSetError = setError
    return {
      updatingAll: false,
      updatePending: {},
      runUpdate: vi.fn(),
      updateAll: vi.fn(),
    }
  },
}))

import DiscoverPage from './DiscoverPage'

/** The registries response shape (api/client.ts `refreshRegistries`). */
function registriesResult(over: Partial<{
  ok: boolean; refreshed: string[]; failed: string[]
  results: { name: string; ok: boolean }[]; apps: number; lastSyncedAt: string
}> = {}) {
  return {
    ok: true, refreshed: [], failed: [], results: [], apps: 0, lastSyncedAt: '',
    ...over,
  }
}

const REFRESH_LABEL = 'pages.appsPage.refresh_store'
const PARTIAL_FAILURE_KEY = 'components.registryManager.could_not_refresh_still_showing_last_synced'
const FAILED_BANNER = `${PARTIAL_FAILURE_KEY} {"names":"broken-registry"}`

/**
 * Click refresh and wait for the handler to have SETTLED.
 *
 * The wait observes the handler's own progress (both refresh calls made, then
 * the button re-enabled in its `finally`). Waiting only on the disabled
 * attribute would be satisfied on the first poll by a button that was never
 * disabled, so a click that did nothing would read as a completed refresh —
 * which false-greens any assertion that something is ABSENT afterwards.
 */
async function clickRefresh() {
  const callsBefore = refreshRegistries.mock.calls.length
  const btn = await screen.findByRole('button', { name: REFRESH_LABEL })
  fireEvent.click(btn)
  await waitFor(() => {
    expect(refreshRegistries.mock.calls.length).toBe(callsBefore + 1)
    expect(refreshAppStore.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(btn).not.toBeDisabled()
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  pageSetError = null
  refreshAppStore.mockResolvedValue({ ok: true })
  refreshRegistries.mockResolvedValue(registriesResult())
})

describe('DiscoverPage manual refresh outcome reporting', () => {
  it('surfaces failed registry sources when the refresh reports ok:false', async () => {
    refreshRegistries.mockResolvedValue(
      registriesResult({ ok: false, failed: ['broken-registry'] }),
    )
    renderWithProviders(<DiscoverPage />)
    await clickRefresh()
    expect(await screen.findByText(FAILED_BANNER)).toBeInTheDocument()
  })

  it('surfaces a rejected refresh POST via its error message, and still refetches', async () => {
    refreshRegistries.mockRejectedValue(new Error('registry backend unreachable'))
    const { queryClient } = renderWithProviders(<DiscoverPage />)
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    await clickRefresh()
    expect(
      await screen.findByText('registry backend unreachable'),
    ).toBeInTheDocument()
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['registry'] })
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['apps'] })
    })
  })

  it('keeps both sources firing: a rejected store refresh still reports, and the registries call still ran', async () => {
    refreshAppStore.mockRejectedValue(new Error('store refresh failed'))
    renderWithProviders(<DiscoverPage />)
    await clickRefresh()
    expect(await screen.findByText('store refresh failed')).toBeInTheDocument()
    expect(refreshRegistries).toHaveBeenCalledTimes(1)
  })

  it('still refetches both lists when it reports a failure, so the healthy source is repaired', async () => {
    refreshRegistries.mockResolvedValue(
      registriesResult({ ok: false, failed: ['broken-registry'] }),
    )
    const { queryClient } = renderWithProviders(<DiscoverPage />)
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    await clickRefresh()
    expect(await screen.findByText(FAILED_BANNER)).toBeInTheDocument()
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['registry'] })
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['apps'] })
    })
  })

  it('shows no banner on a fully successful refresh, and clears one left by an earlier failure', async () => {
    refreshRegistries.mockResolvedValueOnce(
      registriesResult({ ok: false, failed: ['broken-registry'] }),
    )
    renderWithProviders(<DiscoverPage />)
    await clickRefresh()
    expect(await screen.findByText(FAILED_BANNER)).toBeInTheDocument()

    // The next refresh succeeds (the beforeEach default resumes after the
    // mockResolvedValueOnce above): the stale banner must clear.
    await clickRefresh()
    await waitFor(() =>
      expect(screen.queryByText(FAILED_BANNER)).not.toBeInTheDocument(),
    )
  })

  it('leaves a banner another writer set alone on a successful refresh', async () => {
    renderWithProviders(<DiscoverPage />)
    await screen.findByRole('button', { name: REFRESH_LABEL })

    // Stand in for the update hook reporting a failed Update All — the same
    // shared setter, and a notice with no auto-dismiss.
    const foreign = 'pages.appsPage.failed_to_update {"names":"some-app"}'
    act(() => pageSetError!(foreign))
    expect(await screen.findByText(foreign)).toBeInTheDocument()

    await clickRefresh()
    expect(screen.getByText(foreign)).toBeInTheDocument()
  })
})
