/**
 * Kiro Space stores as App Store sources (the S2b surfaces).
 *
 * - The Sources block and the Discover sections are absent when the build offers
 *   no store (the open-source build has no provider).
 * - A signed-out store shows a sign-in card; `no_access` offers both Sign out and
 *   Sign in.
 * - A store row and a git row of the same name are two cards with different keys,
 *   and the store row installs by ref through `/api/apps/market/install`, never the
 *   by-name registry install. Opening it goes to `/apps/-/market/...`.
 * - `truncated` shows the "first 1,000" note; confirming the dialog calls confirm.
 * - A git row whose installed app came from a store says so and has no Update.
 * - A `market:` installed app has no Sync or Update anywhere it would render one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import '../i18n/all'

const api = vi.hoisted(() => ({
  getMarketRegistries: vi.fn(),
  setMarketRegistries: vi.fn(),
  marketSignIn: vi.fn(),
  marketSignOut: vi.fn(),
  listMarketListings: vi.fn(),
  getMarketListing: vi.fn(),
  prepareMarketInstall: vi.fn(),
  confirmMarketInstall: vi.fn(),
  installFromRegistry: vi.fn(),
  installFromRegistryStream: vi.fn(),
  updateApp: vi.fn(),
}))
vi.mock('../api/client', () => ({ api }))

import MarketRegistryManager from '../components/appstore/MarketRegistryManager'
import { MarketStoreSections, marketListingPath, marketSourceKey, useMarketSourceRows } from '../components/appstore/MarketListingRows'
import { SIGN_IN_POLL_MS, SIGN_IN_WAIT_MS } from '../components/appstore/MarketRegistryManager'
import {
  MARKET_RELOAD_AT_KEY, MARKET_STATUS_POLL_MS, marketPage, resetMarketAccountsForTests,
} from '../components/appstore/marketAccount'
import AppListRow from '../components/appstore/AppListRow'
import { isMarketSourced, type MarketListingRow, type RegistryApp } from '../components/appstore/types'

/** These waits sit at the end of a CHAINED load: the store-list query
 *  (`getMarketRegistries`) must answer before the listings query
 *  (`listMarketListings` / `getMarketListing`) is enabled, and only then does
 *  the asserted text render. On a loaded coverage shard that chain can pass
 *  Testing Library's one-second default, so each wait names its own deadline. */
const STORE_CHAIN_TIMEOUT_MS = 5000
const STORE_CHAIN = { timeout: STORE_CHAIN_TIMEOUT_MS }


const OFFER = { id: 'team-store', label: 'Team store', web: 'https://store.example' }

function row(extra: Partial<MarketListingRow> = {}): MarketListingRow {
  return {
    registry: 'team-store', ref: 'acme/board', name: 'board', displayName: 'Board',
    summary: 'A board', category: 'productivity', tags: [], publisher: 'acme',
    latestVersion: 2, latestSemver: '1.1.0', trust: 'COMMUNITY', official: false, canPublish: false,
    rating: { status: 'absent' }, ratingCount: { status: 'ok', value: 0 }, downloads: { status: 'absent' },
    installed: false, installedVersion: null, nameTaken: false, ...extra,
  }
}

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname}</div>
}

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<>{node}<Where /></>} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

let reload: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  resetMarketAccountsForTests()
  window.sessionStorage.removeItem(MARKET_RELOAD_AT_KEY)
  reload = vi.spyOn(marketPage, 'reload').mockImplementation(() => {})
})

afterEach(() => {
  vi.useRealTimers()
  reload.mockRestore()
})

describe('with no store offered', () => {
  it('draws neither the Sources block nor a Discover section', async () => {
    api.getMarketRegistries.mockResolvedValue({ offers: [], added: [], status: {} })
    const { container } = wrap(<><MarketRegistryManager /><MarketStoreSections /></>)
    await waitFor(() => expect(api.getMarketRegistries).toHaveBeenCalled(), STORE_CHAIN)
    expect(screen.queryByTestId('market-registry-manager')).toBeNull()
    expect(container.querySelector('[data-testid^="market-section-"]')).toBeNull()
    expect(api.listMarketListings).not.toHaveBeenCalled()
  })
})

describe('the Sources block', () => {
  it('offers Add for a store that is not added, and adds it by id', async () => {
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: [], status: {} })
    api.setMarketRegistries.mockResolvedValue({ ok: true, added: ['team-store'] })
    wrap(<MarketRegistryManager />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add' }, STORE_CHAIN))
    await waitFor(() => expect(api.setMarketRegistries).toHaveBeenCalledWith(['team-store']), STORE_CHAIN)
  })

  it('shows Sign in, with Sign out and Remove in the More actions menu, for no_access', async () => {
    api.getMarketRegistries.mockResolvedValue({
      offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'no_access', account: '' } },
    })
    wrap(<MarketRegistryManager />)
    expect(await screen.findByText('Kiro Space refused this sign-in.', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
    const more = screen.getByRole('button', { name: 'More actions' })
    fireEvent.pointerDown(more, { button: 0, pointerType: 'mouse' })
    expect(await screen.findByRole('menuitem', { name: 'Sign out' }, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Remove' })).toBeInTheDocument()
  })

  it('shows the code and the approval page after Sign in', async () => {
    api.getMarketRegistries.mockResolvedValue({
      offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'needs_sign_in', account: '' } },
    })
    api.marketSignIn.mockResolvedValue({ ok: true, registry: 'team-store', state: 'pending', code: 'WXYZ', approvalPage: 'https://store.example/approve' })
    wrap(<MarketRegistryManager />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }, STORE_CHAIN))
    expect(await screen.findByText('WXYZ', {}, STORE_CHAIN)).toBeInTheDocument()
    // The link names the site it opens.
    expect(screen.getByRole('link', { name: 'Open store.example to approve' })).toHaveAttribute('href', 'https://store.example/approve')
    expect(api.marketSignIn).toHaveBeenCalledWith('team-store')
  })

  it('offers no Add for a second store while one is added', async () => {
    const OTHER = { id: 'other-store', label: 'Other store', web: '' }
    api.getMarketRegistries.mockResolvedValue({
      offers: [OFFER, OTHER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } },
    })
    wrap(<MarketRegistryManager />)
    const other = await screen.findByTestId('market-store-other-store', {}, STORE_CHAIN)
    expect(other.querySelector('button')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Add' })).toBeNull()
  })

  it('says on Discover, outside Sources, when the store list cannot be read', async () => {
    api.getMarketRegistries.mockRejectedValue(new Error('gateway did not answer'))
    wrap(<MarketStoreSections />)
    expect(await screen.findByTestId('market-registries-error', {}, STORE_CHAIN)).toHaveTextContent('gateway did not answer')
  })

  it('shows a sign-in the store answered as failed as an error notice', async () => {
    api.getMarketRegistries.mockResolvedValue({
      offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'needs_sign_in', account: '' } },
    })
    api.marketSignIn.mockResolvedValue({ ok: false, registry: 'team-store', state: 'failed' })
    wrap(<MarketRegistryManager />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }, STORE_CHAIN))
    const alert = await screen.findByRole('alert', {}, STORE_CHAIN)
    expect(alert).toHaveTextContent("Sign-in didn't finish. Sign in again.")
    expect(screen.getByText('Ask the agent')).toBeInTheDocument()
  })

  it('keeps the rows and shows the error when a later status read fails', async () => {
    api.getMarketRegistries.mockResolvedValueOnce({ offers: [OFFER], added: [], status: {} })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><MemoryRouter><MarketRegistryManager /></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByRole('button', { name: 'Add' }, STORE_CHAIN)).toBeInTheDocument()
    api.getMarketRegistries.mockRejectedValueOnce(new Error('gateway did not answer'))
    await act(async () => { await client.refetchQueries({ queryKey: ['market-registries'] }) })
    expect(await screen.findByText('gateway did not answer', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add' })).toBeInTheDocument()
  })
})

describe('the Discover section', () => {
  const added = { offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } }

  it('draws a sign-in card for a signed-out store', async () => {
    api.getMarketRegistries.mockResolvedValue({ ...added, status: { 'team-store': { state: 'needs_sign_in', account: '' } } })
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [], truncated: false, status: { state: 'needs_sign_in', account: '' } })
    wrap(<MarketStoreSections />)
    expect(await screen.findByTestId('market-signin-card', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.getByText('Sign in to Team store')).toBeInTheDocument()
  })

  it('installs by ref through the market route, not the by-name registry install', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row()], truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
    api.prepareMarketInstall.mockResolvedValue({
      ok: true, stagingId: 'S'.repeat(22),
      confirm: {
        app: { ref: 'acme/board', version: 2, appVersion: '1.1.0', publisher: 'acme', sha256Prefix: 'abcdef012345' },
        components: [], size: 2048, trustLabel: 'Community, not reviewed by Kiro Space',
        permissions: { notice: 'Declared by the publisher, not enforced.', commands: [], network: ['api.example'], files: [], background: [], mcpServers: [], agents: [] },
      },
    })
    api.confirmMarketInstall.mockResolvedValue({ ok: true, name: 'board' })
    wrap(<MarketStoreSections />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }, STORE_CHAIN))
    expect(await screen.findByTestId('market-install-confirm', {}, STORE_CHAIN)).toBeInTheDocument()
    // The dialog names the store the card sits under, not a third name.
    expect(screen.getByRole('dialog')).toHaveTextContent('Install Board from Team store')
    expect(api.prepareMarketInstall).toHaveBeenCalledWith('acme/board', 'a', 'team-store')
    expect(screen.getByText('abcdef012345')).toBeInTheDocument()
    expect(screen.getByText('api.example')).toBeInTheDocument()
    const dialogInstall = screen.getAllByRole('button', { name: 'Install' }).at(-1)!
    fireEvent.click(dialogInstall)
    await waitFor(() => expect(api.confirmMarketInstall).toHaveBeenCalledWith('S'.repeat(22), true), STORE_CHAIN)
    expect(api.installFromRegistry).not.toHaveBeenCalled()
    expect(api.installFromRegistryStream).not.toHaveBeenCalled()
  })

  it('opens the listing at /apps/-/market/...', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row()], truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
    wrap(<MarketStoreSections />)
    fireEvent.click(await screen.findByTestId('market-row-acme/board', {}, STORE_CHAIN))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/apps/-/market/team-store/acme/board'), STORE_CHAIN)
    expect(marketListingPath(row())).toBe('/apps/-/market/team-store/acme/board')
  })

  it('says only the first 1,000 show when the list is truncated', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row()], truncated: true, status: { state: 'signed_in', account: 'a' }, account: 'a' })
    wrap(<MarketStoreSections />)
    expect(await screen.findByTestId('market-truncated', {}, STORE_CHAIN)).toHaveTextContent('Showing only the first 1,000 apps.')
  })

  it('keeps a same-name store row and git row as two cards with their own labels', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row({ nameTaken: true })], truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
    const git = { name: 'board', displayName: 'Board', description: '', version: '1.0.0', author: 'Kiro Crew', tags: [], installed: true } as RegistryApp
    const noop = () => {}
    wrap(<><MarketStoreSections /><AppListRow connected app={git} onOpen={noop} onGet={noop} onUpdate={noop} onEnable={noop} /></>)
    const storeRow = await screen.findByTestId('market-row-acme/board', {}, STORE_CHAIN)
    expect(storeRow).toHaveTextContent('Team store · acme')
    expect(storeRow).toHaveTextContent('An app with this name is already installed from another source.')
    // The notice sits in the shrinking text column, beside the name, so it
    // cannot squeeze the name to an ellipsis.
    const notice = screen.getByText('An app with this name is already installed from another source.')
    expect(notice.closest('.flex-1.min-w-0')).not.toBeNull()
    expect(notice.closest('.shrink-0')).toBeNull()
    expect(screen.getAllByText('Board')).toHaveLength(2)
  })
})

describe('a git row over a Kiro Space install', () => {
  it('says it came from Kiro Space and offers no Update', () => {
    const noop = () => {}
    const app = { name: 'board', displayName: 'Board', description: '', version: '9.0.0', author: 'x', tags: [], installed: true, updateAvailable: true, installedFrom: 'market' } as RegistryApp
    render(<AppListRow connected app={app} onOpen={noop} onGet={noop} onUpdate={noop} onEnable={noop} />)
    expect(screen.getByTestId('installed-from-market')).toHaveTextContent('Installed from Kiro Space')
    expect(screen.queryByRole('button', { name: /update/i })).toBeNull()
  })
})

describe('isMarketSourced', () => {
  it('is true only for a market: source string', () => {
    expect(isMarketSourced({ source: 'market:acme/board@2' })).toBe(true)
    expect(isMarketSourced({ source: 'registry:board' })).toBe(false)
    expect(isMarketSourced({ source: { type: 'git' } as unknown as string })).toBe(false)
  })
})

describe('a Kiro Space install in the Library', () => {
  it('has no Sync in its tile menu', async () => {
    const { default: LaunchpadTile } = await import('../pages/apps/LaunchpadTile')
    const { act } = await import('@testing-library/react')
    vi.useFakeTimers()
    const app = {
      name: 'board', displayName: 'Board', version: '1.0.0', enabled: true, lifecycle: 'gateway',
      updateAvailable: false, manifest: {}, source: 'market:acme/board@2',
    } as unknown as import('../pages/apps/useAppsData').LibraryApp
    render(
      <LaunchpadTile app={app} pinned connected actionLoading={null} onTogglePin={() => {}}
        onAction={() => {}} onOpen={() => {}} onDetail={() => {}} />,
    )
    const trigger = screen.getByRole('button', { name: 'More actions for Board' })
    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Enter' })
    act(() => { vi.runAllTimers() })
    expect(screen.getByRole('menuitem', { name: 'Details' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Sync' })).toBeNull()
  })

  it('is skipped by a row update and by Update All', async () => {
    const { renderHook, act } = await import('@testing-library/react')
    const { useAppUpdates } = await import('../pages/apps/useAppUpdates')
    const updateApp = vi.fn()
    const market = { name: 'board', displayName: 'Board', source: 'market:acme/board@2' }
    const { result } = renderHook(() => useAppUpdates({
      apps: [market] as never, updatables: [market] as never, announceAppsChanged: () => {},
      updateApp, setError: () => {}, setSuccess: () => {},
    } as never))
    await act(async () => { await result.current.runUpdate('board') })
    await act(async () => { await result.current.updateAll() })
    expect(updateApp).not.toHaveBeenCalled()
    expect(api.updateApp).not.toHaveBeenCalled()
  })
})

describe('a sign-in in progress', () => {
  const needs = { offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'needs_sign_in', account: '' } } }
  const done = { offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'alice' } } }

  it('polls the status while the code shows, and reloads the page once signed in', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    vi.setSystemTime(new Date('2026-11-14T12:00:00Z'))
    api.getMarketRegistries.mockResolvedValue(needs)
    api.marketSignIn.mockResolvedValue({ ok: true, registry: 'team-store', state: 'pending', code: 'WXYZ', approvalPage: 'https://store.example/a' })
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [], truncated: false, status: { state: 'signed_in', account: 'alice' }, account: 'alice' })
    wrap(<MarketRegistryManager />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign in' })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByText('WXYZ')).toBeInTheDocument()
    expect(screen.getByText(/ask the Kiro Space team to add you/)).toBeInTheDocument()
    const before = api.getMarketRegistries.mock.calls.length
    api.getMarketRegistries.mockResolvedValue(done)
    await act(async () => { await vi.advanceTimersByTimeAsync(SIGN_IN_POLL_MS + 10) })
    expect(api.getMarketRegistries.mock.calls.length).toBeGreaterThan(before)
    // Signed out to alice is an account change: the page starts again.
    expect(reload).toHaveBeenCalled()
  })

  it('stops waiting after the limit and says the sign-in did not finish', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    vi.setSystemTime(new Date('2026-11-14T12:00:00Z'))
    api.getMarketRegistries.mockResolvedValue(needs)
    api.marketSignIn.mockResolvedValue({ ok: true, registry: 'team-store', state: 'pending', code: 'WXYZ' })
    wrap(<MarketRegistryManager />)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign in' })) })
    await act(async () => { await vi.advanceTimersByTimeAsync(SIGN_IN_WAIT_MS + SIGN_IN_POLL_MS) })
    expect(screen.queryByText('WXYZ')).toBeNull()
    expect(screen.getByText("Sign-in didn't finish. Sign in again.")).toBeInTheDocument()
    // The 3 s poll has stopped: ten more minutes bring only the 30 s status reads.
    const after = api.getMarketRegistries.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(SIGN_IN_WAIT_MS) })
    expect(api.getMarketRegistries.mock.calls.length - after).toBeLessThanOrEqual(SIGN_IN_WAIT_MS / MARKET_STATUS_POLL_MS)
  })
})

describe('the store as a rail source', () => {
  const added = { offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } }
  const rows = [
    row(),
    row({ ref: 'acme/notes', name: 'notes', displayName: 'Notes', category: 'research', tags: [], installed: true, installedVersion: 1 }),
  ]

  it('gives one rail row per added store, counted from its listing', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows, truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useMarketSourceRows(), {
      wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
    })
    await waitFor(() => expect(result.current).toEqual([{ name: 'space:team-store', label: 'Team store', count: 2, builtin: false }]), STORE_CHAIN)
    expect(marketSourceKey('team-store')).toBe('registry:space:team-store')
  })

  it('applies the category and installed-only filters to store rows', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows, truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
    const { unmount } = wrap(<MarketStoreSections category="Productivity" />)
    expect(await screen.findByTestId('market-row-acme/board', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('market-row-acme/notes')).toBeNull()
    unmount()
    wrap(<MarketStoreSections installedOnly />)
    expect(await screen.findByTestId('market-row-acme/notes', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('market-row-acme/board')).toBeNull()
  })

  it('hides every store block for a git source, and shows its own for its source', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows, truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
    const { unmount } = wrap(<MarketStoreSections source="registry:community" />)
    await waitFor(() => expect(api.getMarketRegistries).toHaveBeenCalled(), STORE_CHAIN)
    expect(screen.queryByTestId('market-section-team-store')).toBeNull()
    unmount()
    wrap(<MarketStoreSections source={marketSourceKey('team-store')} />)
    expect(await screen.findByTestId('market-section-team-store', {}, STORE_CHAIN)).toBeInTheDocument()
  })

  it('still names a failed store read, by store, under a git source', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({
      ok: true, registry: 'team-store', rows: [], truncated: false,
      status: { state: 'signed_in', account: 'a' }, account: 'a', error: { code: 'market_api_unreachable' },
    })
    wrap(<MarketStoreSections source="registry:community" />)
    const notice = await screen.findByTestId('market-store-errors-team-store', {}, STORE_CHAIN)
    expect(notice).toHaveTextContent('Team store')
    expect(notice).toHaveTextContent("Can't reach Kiro Space right now.")
    expect(screen.queryByTestId('market-section-team-store')).toBeNull()
  })

  it('still says a failed store-list read under a git source', async () => {
    api.getMarketRegistries.mockRejectedValue(new Error('gateway down'))
    wrap(<MarketStoreSections source="registry:community" />)
    expect(await screen.findByTestId('market-registries-error', {}, STORE_CHAIN)).toHaveTextContent('gateway down')
  })

  it('names nothing under a git source for a store that is only signed out', async () => {
    api.getMarketRegistries.mockResolvedValue({ ...added, status: { 'team-store': { state: 'needs_sign_in', account: '' } } })
    api.listMarketListings.mockResolvedValue({
      ok: true, registry: 'team-store', rows: [], truncated: false,
      status: { state: 'needs_sign_in', account: '' }, error: { code: 'market_signed_out' },
    })
    wrap(<MarketStoreSections source="registry:community" />)
    await waitFor(() => expect(api.listMarketListings).toHaveBeenCalled(), STORE_CHAIN)
    expect(screen.queryByTestId('market-store-errors-team-store')).toBeNull()
  })
})

describe('a store block that changes state', () => {
  const added = { offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } }

  it('shows no rows once the store reads as signed out', async () => {
    api.getMarketRegistries.mockResolvedValue({ ...added, status: { 'team-store': { state: 'needs_sign_in', account: '' } } })
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row()], truncated: false, status: { state: 'needs_sign_in', account: '' } })
    wrap(<MarketStoreSections />)
    expect(await screen.findByTestId('market-signin-card', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('market-row-acme/board')).toBeNull()
  })

  it('says so when the refresh button fails', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings
      .mockResolvedValueOnce({ ok: true, registry: 'team-store', rows: [row()], truncated: false, status: { state: 'signed_in', account: 'a' }, account: 'a' })
      .mockRejectedValueOnce(new Error('Gateway unreachable'))
    wrap(<MarketStoreSections />)
    await screen.findByTestId('market-row-acme/board', {}, STORE_CHAIN)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh' })) })
    expect(await screen.findByText('Gateway unreachable', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.getByTestId('market-row-acme/board')).toBeInTheDocument()
  })
})

describe('a store that cannot be read right now', () => {
  const added = { offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } }

  it('shows no rows and no Install under unknown, only the read error', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({
      ok: true, registry: 'team-store', rows: [row()], truncated: false,
      status: { state: 'unknown', account: '' }, error: { code: 'market_api_unreachable' },
    })
    wrap(<MarketStoreSections />)
    expect(await screen.findByText("Can't reach Kiro Space right now.", {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByTestId('market-row-acme/board')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
    expect(screen.queryByTestId('market-signin-card')).toBeNull()
  })

  it('names the status detail under unknown, not only the read error', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({
      ok: true, registry: 'team-store', rows: [], truncated: false,
      status: { state: 'unknown', account: '', detail: 'cli_outdated' }, error: { code: 'market_unavailable' },
    })
    wrap(<MarketStoreSections />)
    expect(await screen.findByText('The Kiro Space command line is too old. Update it.', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByText("Can't reach Kiro Space right now.")).toBeNull()
  })

  it('shows the read error, not "Checking sign-in…", for a store the status read finds unknown', async () => {
    api.getMarketRegistries.mockResolvedValue({ ...added, status: { 'team-store': { state: 'unknown', account: '' } } })
    api.listMarketListings.mockResolvedValue({
      ok: true, registry: 'team-store', rows: [], truncated: false,
      status: { state: 'unknown', account: '' }, error: { code: 'market_unavailable' },
    })
    wrap(<MarketStoreSections />)
    expect(await screen.findByText("Can't reach Kiro Space right now.", {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByText('Checking sign-in…')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
  })

  it('drops the rows when a refresh answers a status that is not signed_in', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValueOnce({
      ok: true, registry: 'team-store', rows: [row()], truncated: false,
      status: { state: 'signed_in', account: 'a' }, account: 'a',
    })
    wrap(<MarketStoreSections />)
    expect(await screen.findByTestId('market-row-acme/board', {}, STORE_CHAIN)).toBeInTheDocument()
    api.listMarketListings.mockResolvedValueOnce({
      ok: true, registry: 'team-store', rows: [], truncated: false,
      status: { state: 'unknown', account: '' }, error: { code: 'market_unavailable' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByTestId('market-row-acme/board')).toBeNull(), STORE_CHAIN)
    expect(screen.getByText("Can't reach Kiro Space right now.")).toBeInTheDocument()
  })
})

describe('the listing page', () => {
  it('shows one notice when the store is signed out', async () => {
    const { default: MarketListingPage } = await import('../pages/apps/MarketListingPage')
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'needs_sign_in', account: '' } } })
    api.getMarketListing.mockResolvedValue({
      ok: true, registry: 'team-store', listing: null,
      status: { state: 'needs_sign_in', account: '' }, error: { code: 'market_signed_out' },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/acme/board']}>
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Added · not signed in', {}, STORE_CHAIN)).toBeInTheDocument()
    // A signed-out store is a state, not a failure: no error notice.
    expect(screen.getByText('Added · not signed in')).toHaveAttribute('role', 'status')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('Sign in to Kiro Space first.')).toBeNull()
    expect(api.getMarketListing).toHaveBeenCalledWith('team-store', 'acme/board')
  })

  it('offers no Install unless the store is signed in', async () => {
    const { default: MarketListingPage } = await import('../pages/apps/MarketListingPage')
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'unknown', account: '' } } })
    api.getMarketListing.mockResolvedValue({
      ok: true, registry: 'team-store', listing: row(), status: { state: 'unknown', account: '' },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/acme/board']}>
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(api.getMarketListing).toHaveBeenCalled(), STORE_CHAIN)
    expect(await screen.findByText('Checking sign-in…', {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
  })

  it('shows the read error of a store whose status is unknown', async () => {
    const { default: MarketListingPage } = await import('../pages/apps/MarketListingPage')
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'unknown', account: '' } } })
    api.getMarketListing.mockResolvedValue({
      ok: true, registry: 'team-store', listing: null,
      status: { state: 'unknown', account: '' }, error: { code: 'market_unavailable' },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/acme/board']}>
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByText("Can't reach Kiro Space right now.", {}, STORE_CHAIN)).toBeInTheDocument()
    expect(screen.queryByText('Checking sign-in…')).toBeNull()
  })

  it('closes the install dialog when the route moves to another listing', async () => {
    const { default: MarketListingPage } = await import('../pages/apps/MarketListingPage')
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } })
    api.getMarketListing.mockImplementation(async (_r: string, ref: string) => ({
      ok: true, registry: 'team-store', account: 'a', listing: row({ ref, displayName: ref === 'acme/board' ? 'Board' : 'Notes' }),
      status: { state: 'signed_in', account: 'a' },
    }))
    api.prepareMarketInstall.mockReturnValueOnce(new Promise(() => {}))
    function GoToNotes() {
      const navigate = useNavigate()
      return <button type="button" onClick={() => navigate('/apps/-/market/team-store/acme/notes')}>go</button>
    }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/acme/board']}>
          <GoToNotes />
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await screen.findByRole('heading', { name: 'Board' }, STORE_CHAIN)
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    expect(await screen.findByRole('dialog', {}, STORE_CHAIN)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'go' }))
    await screen.findByRole('heading', { name: 'Notes' }, STORE_CHAIN)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps the title and Back in the page header, above a scrolling body', async () => {
    const { default: MarketListingPage } = await import('../pages/apps/MarketListingPage')
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } })
    api.getMarketListing.mockResolvedValue({
      ok: true, registry: 'team-store', account: 'a', listing: row(), status: { state: 'signed_in', account: 'a' },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/acme/board']}>
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const header = await screen.findByTestId('page-header', {}, STORE_CHAIN)
    expect(header).toContainElement(screen.getByRole('button', { name: /Back to the App Store/ }))
    expect(await screen.findByTestId('page-subtitle', {}, STORE_CHAIN)).toHaveTextContent('Team store')
    expect(screen.getByTestId('market-listing-page').className).toContain('overflow-y-auto flex-1 min-h-0')
  })

  it('lets a long unbroken title wrap inside a narrow header', async () => {
    const { default: MarketListingPage } = await import('../pages/apps/MarketListingPage')
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'signed_in', account: 'a' } } })
    api.getMarketListing.mockResolvedValue({
      ok: true, registry: 'team-store', account: 'a', listing: row({ displayName: 'CustomerRelationshipManagement' }),
      status: { state: 'signed_in', account: 'a' },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/apps/-/market/team-store/acme/board']}>
          <Routes>
            <Route path="/apps/-/market/:registry/:namespace/:name" element={<MarketListingPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const title = await screen.findByRole('heading', { name: 'CustomerRelationshipManagement' }, STORE_CHAIN)
    expect(title.className).toContain('[overflow-wrap:anywhere]')
    // Stacked below `sm`, side by side from `sm` up.
    expect(title.closest('.flex-col')?.className).toContain('sm:flex-row')
  })
})
