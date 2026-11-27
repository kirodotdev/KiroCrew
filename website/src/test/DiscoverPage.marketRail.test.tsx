/**
 * Discover with a Kiro Space store picked in the rail: the rail counts that
 * store's rows (All apps, each category, Installed), not the git rows, and
 * the store's own rail row follows the search box like a git source does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createConnectedTestStore } from './helpers'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import type { MarketListingRow } from '../components/appstore/types'

const api = vi.hoisted(() => ({
  listApps: vi.fn(),
  listRegistry: vi.fn(),
  listRegistries: vi.fn(),
  getMarketRegistries: vi.fn(),
  listMarketListings: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
    ...api,
    updateRegistries: vi.fn(),
    refreshRegistries: vi.fn(),
    enableApp: vi.fn(),
    disableApp: vi.fn(),
    updateApp: vi.fn(),
    uninstallApp: vi.fn(),
    installApp: vi.fn(),
    openApp: vi.fn(),
  },
}))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))

import AppsPage from '../pages/apps/DiscoverPage'

/** These waits sit at the end of a CHAINED load: the store-list query
 *  (`getMarketRegistries`) must answer before the listings query
 *  (`listMarketListings` / `getMarketListing`) is enabled, and only then does
 *  the asserted text render. On a loaded coverage shard that chain can pass
 *  Testing Library's one-second default, so each wait names its own deadline. */
const STORE_CHAIN_TIMEOUT_MS = 5000
const STORE_CHAIN = { timeout: STORE_CHAIN_TIMEOUT_MS }


function row(extra: Partial<MarketListingRow>): MarketListingRow {
  return {
    registry: 'team-store', ref: 'acme/board', name: 'board', displayName: 'Board',
    summary: 'A board', category: 'productivity', tags: [], publisher: 'acme',
    latestVersion: 1, latestSemver: '1.0.0', trust: 'COMMUNITY', official: false, canPublish: false,
    rating: { status: 'absent' }, ratingCount: { status: 'absent' }, downloads: { status: 'absent' },
    installed: false, installedVersion: null, nameTaken: false, ...extra,
  }
}

const STORE_ROWS = [
  row({}),
  row({ ref: 'acme/notes', name: 'notes', displayName: 'Notes', summary: 'Notes', category: 'research' }),
  row({ ref: 'acme/inbox', name: 'inbox', displayName: 'Inbox', summary: 'Mail', category: 'productivity', installed: true, installedVersion: 1 }),
]

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={createConnectedTestStore()}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/apps']}>
          <Routes>
            <Route path="/apps" element={<AppsPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

describe('Discover with a Kiro Space store in the rail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    api.listApps.mockResolvedValue([])
    api.listRegistry.mockResolvedValue({
      apps: [{ name: 'oncall-radar', displayName: 'Oncall Radar', author: 'x', description: 'Ops', version: '1.0.0', tags: ['oncall'], installed: false }],
      serverPlatform: { os: 'linux', arch: 'x64' },
    })
    api.listRegistries.mockResolvedValue({ registries: [] })
    api.getMarketRegistries.mockResolvedValue({ offers: [{ id: 'team-store', label: 'Team store', web: '' }], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } })
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: STORE_ROWS, truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
  })

  it('counts no rows of a store whose status is not signed_in', async () => {
    api.listMarketListings.mockResolvedValue({
      ok: true, registry: 'team-store', rows: STORE_ROWS, truncated: false,
      status: { state: 'unknown', account: '' }, error: { code: 'market_unavailable' },
    })
    renderPage()
    await waitFor(() => expect(api.listMarketListings).toHaveBeenCalled(), STORE_CHAIN)
    expect(await screen.findByRole('radio', { name: 'Team store 0 apps' }, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /All apps 1/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Installed 0' })).toBeInTheDocument()
  })

  it('keeps the store rail and rows when the git catalog read fails', async () => {
    api.listRegistry.mockRejectedValue(new Error('catalog down'))
    renderPage()
    expect(await screen.findByRole('radio', { name: 'Team store 3 apps' }, STORE_CHAIN)).toBeInTheDocument()
    expect(await screen.findByTestId('market-row-acme/board', {}, STORE_CHAIN)).toBeInTheDocument()
  })

  it('says the store list could not be read even when the git catalog is empty', async () => {
    api.listRegistry.mockResolvedValue({ apps: [], serverPlatform: { os: 'linux', arch: 'x64' } })
    api.getMarketRegistries.mockRejectedValue(new Error('gateway did not answer'))
    renderPage()
    expect(await screen.findByTestId('market-registries-error', {}, STORE_CHAIN)).toHaveTextContent('gateway did not answer')
  })

  it('keeps a picked store-only category on the rail while searching', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('radio', { name: /Productivity 2/ }, STORE_CHAIN))
    fireEvent.change(screen.getByLabelText('Search apps'), { target: { value: 'notes' } })
    const picked = await screen.findByRole('radio', { name: /Productivity 0/ }, STORE_CHAIN)
    expect(picked).toHaveAttribute('aria-checked', 'true')
  })

  it('counts every added store in the default All sources view', async () => {
    renderPage()
    // 1 git row + 3 store rows; Productivity and Research exist only in the store.
    await waitFor(() => expect(screen.getByRole('radio', { name: /All apps 4/ })).toBeInTheDocument(), STORE_CHAIN)
    expect(screen.getByRole('radio', { name: /Productivity 2/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Research & Writing 1/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /On-call & Ops 1/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Installed 1' })).toBeInTheDocument()
  })

  it("counts the picked store's rows in the rail", async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('radio', { name: 'Team store 3 apps' }, STORE_CHAIN))
    await waitFor(() => expect(screen.getByRole('radio', { name: /All apps 3/ })).toBeInTheDocument(), STORE_CHAIN)
    expect(screen.getByRole('radio', { name: /Productivity 2/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Research & Writing 1/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /On-call & Ops 0/ })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Installed 1' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: /Research & Writing 1/ }))
    expect(await screen.findByRole('radio', { name: 'Installed 0' }, STORE_CHAIN)).toBeInTheDocument()
  })

  it("follows the search box in the store's rail row", async () => {
    renderPage()
    await screen.findByRole('radio', { name: 'Team store 3 apps' }, STORE_CHAIN)
    fireEvent.change(screen.getByLabelText('Search apps'), { target: { value: 'notes' } })
    expect(await screen.findByRole('radio', { name: 'Team store 1 app' }, STORE_CHAIN)).toBeInTheDocument()
  })
})
