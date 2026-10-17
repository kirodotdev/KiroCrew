/**
 * The store header's manual refresh button (DiscoverPage — the storefront
 * half of the Discover/Library split).
 *
 * The two cache layers behind the store degrade silently -- a failed catalog
 * fetch leaves the seed listing pinned for up to an hour -- so the header
 * carries an explicit refresh. The contract asserted here is the ORDER and the
 * SHAPE of what a click does:
 *
 * 1. Both cache-busting POSTs fire first (`refreshAppStore` for the official
 *    documents, `refreshRegistries` for the user's external registries --
 *    both sources the store renders), and
 * 2. both lists are refetched AFTER the busts, so the refetch reads the
 *    rebuilt caches rather than racing the deletion.
 * 3. One source being unreachable must not stop the refetch from repairing
 *    the other (the POSTs are allSettled, not all).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

const listApps = vi.fn()
const listRegistry = vi.fn()
const listRegistries = vi.fn()
const refreshAppStore = vi.fn()
const refreshRegistries = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listApps: (...a: unknown[]) => listApps(...a),
    listRegistry: (...a: unknown[]) => listRegistry(...a),
    listRegistries: (...a: unknown[]) => listRegistries(...a),
    refreshAppStore: (...a: unknown[]) => refreshAppStore(...a),
    refreshRegistries: (...a: unknown[]) => refreshRegistries(...a),
    updateRegistries: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    updateApp: vi.fn(),
    uninstallApp: vi.fn(),
    uninstallPreview: vi.fn().mockResolvedValue({ dependencies: { removable: [], shared: [], userInstalled: [] } }),
    installApp: vi.fn(),
    openApp: vi.fn(),
  },
}))

vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

vi.mock('../components/AppIcon', () => ({
  default: () => <div data-testid="app-icon" />,
}))

import DiscoverPage from '../pages/apps/DiscoverPage'

function renderPage() {
  // A fresh client per test: the refresh contract is about cache-busting, so a
  // cache shared across tests would let one test's refetch satisfy another's
  // assertion.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<DiscoverPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear()
  listApps.mockResolvedValue([])
  listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
  listRegistries.mockResolvedValue({ registries: [] })
  refreshAppStore.mockResolvedValue({ ok: true })
  refreshRegistries.mockResolvedValue({ ok: true, refreshed: [], failed: [], results: [], apps: 0, lastSyncedAt: '' })
})

describe('the store refresh button', () => {
  it('busts both server caches, then refetches both lists', async () => {
    renderPage()
    await waitFor(() => expect(listRegistry).toHaveBeenCalledTimes(1))
    const initialLists = listApps.mock.calls.length

    await userEvent.click(screen.getByRole('button', { name: 'Refresh the store' }))

    await waitFor(() => {
      expect(refreshAppStore).toHaveBeenCalledTimes(1)
      expect(refreshRegistries).toHaveBeenCalledTimes(1)
      // The refetches land AFTER the busts: a refetch racing the deletion
      // would read the very cache the click was meant to drop.
      expect(listRegistry).toHaveBeenCalledTimes(2)
      expect(listApps.mock.calls.length).toBeGreaterThan(initialLists)
    })
    expect(refreshAppStore.mock.invocationCallOrder[0]).toBeLessThan(
      listRegistry.mock.invocationCallOrder[1],
    )
  })

  it('still refetches when a cache-bust POST rejects', async () => {
    // One unreachable source must not stop the refetch from repairing the
    // other -- the degraded state is exactly when the button gets pressed.
    refreshAppStore.mockRejectedValue(new Error('offline'))
    renderPage()
    await waitFor(() => expect(listRegistry).toHaveBeenCalledTimes(1))

    await userEvent.click(screen.getByRole('button', { name: 'Refresh the store' }))

    await waitFor(() => expect(listRegistry).toHaveBeenCalledTimes(2))
  })
})
